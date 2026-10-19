"""Gmail の独立した読み取り専用認証と、再試行可能な候補取り込みを扱う。"""

from __future__ import annotations

import base64
import datetime as dt
import json
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence
from urllib.parse import quote

from symnote.config import AppConfig
from symnote.core.db import connection
from symnote.core.runtime import is_test_mode
from symnote.core.task_extraction import ExtractionResult, ImportItem, extract_task_candidates

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
KEYRING_SERVICE = "symnote-gmail"
KEYRING_USERNAME = "default"
MAX_BODY_CHARACTERS = 12000


class GmailImportError(RuntimeError):
    """Gmail の設定・認証・読み取りの失敗を日本語で伝える。"""


class GmailConnectionBlocked(GmailImportError):
    """テスト環境で共有認証情報を変更・更新しようとしたことを表す。"""


def _guard_credentials() -> None:
    """テスト環境から共有資格情報へのアクセスを拒否する。"""
    if is_test_mode():
        raise GmailConnectionBlocked("テスト環境では本番と共有の Gmail 接続を変更・使用しません。")


def _keyring() -> Any:
    """OS の資格情報ストアを遅延ロードする。"""
    import keyring

    return keyring


def is_connected() -> bool:
    """Gmail 専用の資格情報が保存済みか返し、ストアの障害は未接続扱いにする。"""
    if is_test_mode():
        return False
    try:
        return bool(_keyring().get_password(KEYRING_SERVICE, KEYRING_USERNAME))
    except Exception:
        return False


def connect(config: AppConfig | None = None) -> None:
    """Desktop OAuth で gmail.readonly のみを承認し、専用 keyring へ保存する。"""
    _guard_credentials()
    from symnote.core.calendar_settings import effective_config

    client_file = Path(effective_config(config).google_calendar_client_secret_path).expanduser()
    if not client_file.is_file():
        raise GmailImportError("Desktop OAuth クライアント JSON を Google 接続設定で指定してください。")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow

        credentials = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES).run_local_server(
            port=0, open_browser=True
        )
        _keyring().set_password(KEYRING_SERVICE, KEYRING_USERNAME, credentials.to_json())
    except Exception:
        raise GmailImportError("Gmail に接続できませんでした。Gmail API と OAuth 同意画面の設定を確認してください。") from None


def disconnect() -> None:
    """ローカルの Gmail 専用資格情報だけを削除し、メールや Calendar 認証は変更しない。"""
    _guard_credentials()
    try:
        store = _keyring()
        if store.get_password(KEYRING_SERVICE, KEYRING_USERNAME):
            store.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except Exception:
        raise GmailImportError("Gmail の接続解除に失敗しました。資格情報ストアを確認してください。") from None


def _gmail_service() -> Any:
    """保存済みの読み取り専用トークンで Gmail サービスを構築する。"""
    _guard_credentials()
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        payload = _keyring().get_password(KEYRING_SERVICE, KEYRING_USERNAME)
        if not payload:
            raise GmailImportError("Gmail が未接続です。先に接続してください。")
        credentials = Credentials.from_authorized_user_info(json.loads(payload))
        if set(credentials.scopes or []) != set(SCOPES):
            raise GmailImportError("Gmail の認証スコープを確認できません。再接続してください。")
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            _keyring().set_password(KEYRING_SERVICE, KEYRING_USERNAME, credentials.to_json())
        if not credentials.valid:
            raise GmailImportError("Gmail の認証が期限切れです。再接続してください。")
        return build("gmail", "v1", credentials=credentials, cache_discovery=False)
    except GmailImportError:
        raise
    except Exception:
        raise GmailImportError("Gmail の認証を利用できません。接続設定と通信状況を確認してください。") from None


def _ensure_table() -> None:
    """既存のマイグレーションを変更せず、取り込み専用の追記テーブルを用意する。"""
    with connection() as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS gmail_import_processed ("
            "source_ref TEXT PRIMARY KEY, processed_at TEXT NOT NULL)"
        )


def mark_processed(source_refs: Sequence[str]) -> None:
    """AI 抽出と候補保存が完了した出典を記録する。失敗した出典は渡さない。"""
    _ensure_table()
    with connection() as conn, conn:
        conn.executemany(
            "INSERT OR IGNORE INTO gmail_import_processed(source_ref, processed_at) VALUES (?, ?)",
            [(ref, dt.datetime.now(dt.timezone.utc).isoformat()) for ref in source_refs],
        )


class _HTMLText(HTMLParser):
    """HTML 本文から表示用テキストを取り出し、スクリプト・スタイルは無視する。"""

    def __init__(self) -> None:
        """抽出用の断片と非表示要素の深さを初期化する。"""
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: List[tuple[str, str | None]]) -> None:
        """非表示要素と段落境界を追跡する。"""
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """非表示要素の終端と段落境界を追跡する。"""
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        """表示される文字列だけを保存する。"""
        if not self.hidden:
            self.parts.append(data)


def _body_parts(payload: Any) -> List[tuple[str, str]]:
    """添付ファイルを除外し、ネストした MIME からテキスト断片を集める。"""
    if not isinstance(payload, dict) or payload.get("filename"):
        return []
    headers = {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in payload.get("headers", []) if isinstance(h, dict)}
    if headers.get("content-disposition", "").lower().startswith("attachment"):
        return []
    parts: List[tuple[str, str]] = []
    mime = payload.get("mimeType", "")
    data = (payload.get("body") or {}).get("data")
    if mime in {"text/plain", "text/html"} and isinstance(data, str):
        try:
            encoded = data[:128000]
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            message = Message()
            message["content-type"] = headers.get("content-type", "text/plain; charset=utf-8")
            text = raw.decode(message.get_content_charset() or "utf-8", errors="replace")
            if mime == "text/html":
                parser = _HTMLText()
                parser.feed(text)
                text = "".join(parser.parts)
            parts.append((mime, text))
        except (ValueError, LookupError):
            pass
    for child in payload.get("parts", []) or []:
        parts.extend(_body_parts(child))
    return parts


def fetch_recent_messages(
    days: int = 7,
    max_messages: int = 20,
    service_factory: Callable[[], Any] | None = None,
) -> List[ImportItem]:
    """受信トレイの未処理メールを最大件数まで取得し、本文をプレーンテキストで返す。

    日数は1〜365、件数は1〜100。処理済み ID はアカウントのメールアドレスで
    名前空間を分ける。取得だけでは処理済みにせず、最大1000件の一覧を調べる。
    """
    if not 1 <= days <= 365 or not 1 <= max_messages <= 100:
        raise ValueError("日数は1〜365、メール件数は1〜100で指定してください。")
    _ensure_table()
    with connection() as conn:
        processed = {row[0] for row in conn.execute("SELECT source_ref FROM gmail_import_processed")}
    try:
        service = (service_factory or _gmail_service)()
        users = service.users()
        account = str(users.getProfile(userId="me").execute()["emailAddress"])
        messages = users.messages()
        results: List[ImportItem] = []
        token: str | None = None
        tokens: set[str] = set()
        seen: set[str] = set()
        scanned = 0
        while scanned < 1000:
            params: dict[str, Any] = {
                "userId": "me", "q": f"newer_than:{days}d -category:promotions -category:social in:inbox",
                "maxResults": min(100, 1000 - scanned),
            }
            if token:
                params["pageToken"] = token
            page = messages.list(**params).execute()
            entries = page.get("messages", [])
            scanned += max(1, len(entries))
            for entry in entries:
                message_id = entry.get("id")
                if not isinstance(message_id, str) or message_id in seen:
                    continue
                seen.add(message_id)
                ref = f"gmail:{account}:{message_id}"
                if ref in processed:
                    continue
                message = messages.get(userId="me", id=message_id, format="full").execute()
                payload = message.get("payload") or {}
                headers = {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in payload.get("headers", []) if isinstance(h, dict)}
                parts = _body_parts(payload)
                plain = [text for mime, text in parts if mime == "text/plain"]
                body = "\n".join(plain or [text for _, text in parts])[:MAX_BODY_CHARACTERS]
                thread_id = message.get("threadId") if isinstance(message.get("threadId"), str) else message_id
                results.append(ImportItem(
                    source_ref=ref, text=body, title=headers.get("subject", ""),
                    sender=headers.get("from", ""), date=headers.get("date", ""),
                    source_url=f"https://mail.google.com/mail/u/{quote(account)}/#all/{quote(thread_id)}",
                ))
                if len(results) >= max_messages:
                    return results
            token = page.get("nextPageToken")
            if not token or token in tokens:
                break
            tokens.add(token)
        return results
    except GmailImportError:
        raise
    except Exception:
        raise GmailImportError("Gmail を取得できませんでした。通信状況・Gmail API・接続権限を確認してください。") from None


def import_recent_messages(
    days: int = 7,
    max_messages: int = 20,
    service_factory: Callable[[], Any] | None = None,
    generate_json: Optional[Callable[..., Any]] = None,
) -> ExtractionResult:
    """未処理メールから候補を保存し、成功したメールだけを処理済みにする。

    AI が使えない・応答が崩れた回のメールは処理済みにせず、次回に再試行する。
    """
    items = fetch_recent_messages(days, max_messages, service_factory)
    result = extract_task_candidates("gmail", items, generate_json)
    if result.processed_refs:
        mark_processed(result.processed_refs)
    return result
