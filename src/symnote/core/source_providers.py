"""GmailとSlackユーザートークンによる読み取り専用通信。"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit

from symnote.core import source_connections as store
from symnote.core.gmail_import import _body_parts
from symnote.core.task_extraction import ImportItem

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SLACK_SCOPES = {"channels:read", "channels:history"}
MAX_MESSAGES = 20
MAX_PAGES = 5
MAX_RESPONSE = 2 * 1024 * 1024
SLACK_METHODS = {
    "oauth.v2.access",
    "auth.test",
    "conversations.list",
    "conversations.info",
    "conversations.history",
}

_before_request = contextvars.ContextVar("symnote_source_before_request", default=None)


@contextmanager
def authorized_run(check):
    token = _before_request.set(check)
    try:
        yield
    finally:
        _before_request.reset(token)


class ProviderError(ValueError):
    def __init__(self, code: str, message: str, retry_after: int = 0):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError(
            "redirect_rejected", "情報源が想定外の場所へ移動しました。接続設定を確認してください。"
        )


def request_json(url: str, *, token: str = "", form: dict | None = None) -> dict:
    parsed = urlsplit(url)
    allowed = (
        (
            parsed.scheme == "https"
            and parsed.hostname == "slack.com"
            and parsed.path.removeprefix("/api/") in SLACK_METHODS
        )
        or (
            parsed.scheme == "https"
            and parsed.hostname == "gmail.googleapis.com"
            and parsed.path.startswith("/gmail/v1/users/me/")
        )
        or url == "https://oauth2.googleapis.com/token"
    )
    if form is not None and url not in {
        "https://oauth2.googleapis.com/token",
        "https://slack.com/api/oauth.v2.access",
    }:
        raise ValueError("情報源への書き込みは許可していません。")
    if not allowed or parsed.username or parsed.port not in (None, 443):
        raise ValueError("許可していない情報源への通信です。")
    check = _before_request.get()
    if check is not None:
        check()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(
        url, data=urlencode(form).encode() if form is not None else None, headers=headers
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ProviderError("response_too_large", "情報源の応答が大きすぎます。")
            result = json.loads(raw)
    except urllib.error.HTTPError as exc:
        if (
            exc.code == 400
            and urlsplit(url).hostname == "gmail.googleapis.com"
            and "pageToken=" in url
        ):
            raise ProviderError("invalid_cursor", "Gmailの続きの位置を更新してください。") from None
        if exc.code == 429:
            value = exc.headers.get("Retry-After", "60")
            retry = min(3600, max(1, int(value))) if value.isdigit() else 60
            raise ProviderError(
                "rate_limited",
                f"情報源の利用制限です。{retry}秒以上待って再試行してください。",
                retry,
            ) from None
        if exc.code in {400, 401, 403}:
            raise ProviderError(
                "authorization_failed",
                "情報源の承認を確認できません。権限と接続設定を確認してください。",
            ) from None
        raise ProviderError(
            "provider_unavailable", "情報源に接続できませんでした。時間をおいて再試行してください。"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise ProviderError(
            "provider_unavailable", "情報源の応答を確認できませんでした。"
        ) from None
    if not isinstance(result, dict):
        raise ProviderError("invalid_response", "情報源の応答形式を確認できませんでした。")
    if parsed.hostname == "slack.com" and not result.get("ok"):
        code = result.get("error", "")
        if code == "ratelimited":
            raise ProviderError(
                "rate_limited", "Slackの利用制限です。時間をおいて再試行してください。", 60
            )
        raise ProviderError(
            "authorization_failed"
            if code in {"invalid_auth", "token_revoked", "token_expired", "missing_scope"}
            else "provider_unavailable",
            "Slackの権限または接続状態を確認してください。",
        )
    return result


def provider_config(provider: str) -> tuple[str, str]:
    store.provider_name(provider)
    prefix = "GOOGLE" if provider == "gmail" else "SLACK"
    return tuple(
        os.getenv(f"SYMNOTE_{prefix}_{part}", "").strip() for part in ("CLIENT_ID", "CLIENT_SECRET")
    )


def callback_url(provider: str) -> str:
    origin = os.getenv("SYMNOTE_PUBLIC_ORIGIN", "").strip().rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ValueError("連携には固定HTTPS公開URLの設定が必要です。")
    return f"{origin}/api/integrations/{store.provider_name(provider)}/callback"


def ready(provider: str) -> bool:
    try:
        callback_url(provider)
        return all(provider_config(provider)) and store.encryption_ready()
    except ValueError:
        return False


def scopes(provider: str, grant: dict, previous: dict | None = None) -> dict:
    if (
        not isinstance(grant, dict)
        or not isinstance(grant.get("access_token"), str)
        or not grant["access_token"]
    ):
        raise ProviderError("invalid_token", "情報源の接続情報が不足しています。")
    scope = grant.get("scope", (previous or {}).get("scope", ""))
    granted = set(re.split(r"[ ,]+", scope.strip())) if isinstance(scope, str) else set()
    expected = {GMAIL_SCOPE} if provider == "gmail" else SLACK_SCOPES
    if granted != expected:
        raise ProviderError(
            "unexpected_scope",
            "読み取り専用の必要最小限の権限を確認できません。追加権限のないアプリで再接続してください。",
        )
    if provider == "slack" and grant.get("token_type") != "user":
        raise ProviderError(
            "bot_rejected",
            "Botトークンは使用しません。ユーザーの読み取り専用権限で接続してください。",
        )
    try:
        expires = max(0, min(int(grant.get("expires_in", 0)), 365 * 86400))
    except (ValueError, TypeError):
        raise ProviderError("invalid_token", "情報源の有効期限を確認できません。") from None
    return {
        "access_token": grant["access_token"],
        "refresh_token": grant.get("refresh_token") or (previous or {}).get("refresh_token", ""),
        "scope": " ".join(sorted(granted)),
        "token_type": "user" if provider == "slack" else "Bearer",
        "expires_at": time.time() + expires if expires else 0,
    }


def identity(provider: str, token: str) -> tuple[str, str]:
    if provider == "gmail":
        profile = request_json(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile", token=token
        )
        if not isinstance(profile.get("emailAddress"), str) or not profile["emailAddress"]:
            raise ProviderError("invalid_identity", "Gmailアカウントを確認できません。")
        return profile["emailAddress"], ""
    data = request_json("https://slack.com/api/auth.test", token=token)
    if data.get("bot_id") or not data.get("user_id") or not data.get("team_id"):
        raise ProviderError(
            "bot_rejected", "Slackの本人用接続を確認できません。Botは使用しません。"
        )
    return str(data["user_id"]), str(data["team_id"])


def exchange(provider: str, code: str, payload: dict) -> tuple[dict, str, str]:
    client_id, secret = provider_config(provider)
    form = {
        "code": code,
        "client_id": client_id,
        "client_secret": secret,
        "redirect_uri": payload["redirect_uri"],
    }
    if provider == "gmail":
        form.update(grant_type="authorization_code", code_verifier=payload["verifier"])
        result = request_json("https://oauth2.googleapis.com/token", form=form)
    else:
        result = request_json("https://slack.com/api/oauth.v2.access", form=form)
        if result.get("bot_user_id") or result.get("access_token") or result.get("scope"):
            raise ProviderError("bot_rejected", "Bot権限を含むSlack接続は受け付けません。")
        result = result.get("authed_user", {})
    credentials = scopes(provider, result)
    account, workspace = identity(provider, credentials["access_token"])
    return credentials, account, workspace


def access(provider: str, row: dict, run: str) -> str:
    value = store.unseal(provider, row["credentials"])
    if value["expires_at"] and value["expires_at"] <= time.time() + 60:
        if not value.get("refresh_token"):
            raise ProviderError(
                "reauthorization_required", "接続の有効期限が切れました。再接続してください。"
            )
        client_id, secret = provider_config(provider)
        form = {
            "grant_type": "refresh_token",
            "refresh_token": value["refresh_token"],
            "client_id": client_id,
            "client_secret": secret,
        }
        url = (
            "https://oauth2.googleapis.com/token"
            if provider == "gmail"
            else "https://slack.com/api/oauth.v2.access"
        )
        result = request_json(url, form=form)
        if provider == "slack" and "authed_user" in result:
            result = result["authed_user"]
        value = scopes(provider, result, value)
        account, workspace = identity(provider, value["access_token"])
        if (account, workspace) != (row["account"], row["workspace"]):
            raise ProviderError(
                "identity_changed", "更新した接続の利用者が一致しません。再接続してください。"
            )
        store.update_credentials(provider, row, run, value)
    return value["access_token"]


def slack_call(method: str, token: str, **params) -> dict:
    if method not in {"conversations.list", "conversations.info", "conversations.history"}:
        raise ValueError("Slackは履歴の読み取りに限定しています。")
    return request_json(f"https://slack.com/api/{method}?{urlencode(params)}", token=token)


def public_channel(token: str, channel_id: str) -> dict:
    if not re.fullmatch(r"C[A-Z0-9]{1,30}", channel_id):
        raise ValueError("公開チャンネルのIDを指定してください。")
    data = slack_call("conversations.info", token, channel=channel_id).get("channel", {})
    if (
        data.get("id") != channel_id
        or data.get("is_private")
        or data.get("is_im")
        or data.get("is_mpim")
        or not data.get("is_channel")
    ):
        raise PermissionError("選択できるのは公開チャンネルだけです。")
    return {"id": channel_id, "name": str(data.get("name", channel_id))}


def channels(token: str, cursor: str = "") -> dict:
    if len(cursor) > 1000:
        raise ValueError("チャンネル一覧の続きが不正です。")
    result = slack_call(
        "conversations.list",
        token,
        types="public_channel",
        exclude_archived="true",
        limit=100,
        cursor=cursor,
    )
    return {
        "channels": [
            {"id": c["id"], "name": c.get("name", c["id"])}
            for c in result.get("channels", [])
            if c.get("is_channel")
            and not c.get("is_private")
            and not c.get("is_im")
            and not c.get("is_mpim")
        ],
        "next_cursor": result.get("response_metadata", {}).get("next_cursor", ""),
    }


@dataclass
class FetchResult:
    items: list[ImportItem]
    limited: bool = False
    checkpoint: dict | None = None


def gmail_messages(
    token: str, account: str, processed: set[str], checkpoint: dict | None = None
) -> FetchResult:
    items: list[ImportItem] = []
    checkpoint = checkpoint or {}
    cursor = checkpoint.get("cursor", "")
    seen: set[str] = set()
    oldest = time.time() - 7 * 86400
    query_oldest = checkpoint.get("oldest", int(oldest))
    for _ in range(MAX_PAGES):
        params = {
            "q": f"in:inbox category:primary after:{query_oldest} -in:spam -in:trash",
            "maxResults": 100,
        }
        if cursor:
            params["pageToken"] = cursor
        try:
            result = request_json(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages?" + urlencode(params),
                token=token,
            )
        except ProviderError as exc:
            if cursor and exc.code == "invalid_cursor":
                return FetchResult(items, True, {})
            raise
        for entry in result.get("messages", []):
            message_id = str(entry.get("id", ""))
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", message_id):
                continue
            ref = f"gmail:{account}:{message_id}"
            if ref in processed:
                continue
            if len(items) >= MAX_MESSAGES:
                return FetchResult(items, True, {"cursor": cursor, "oldest": query_oldest})
            message = request_json(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
                f"{quote(message_id, safe='')}?format=full",
                token=token,
            )
            timestamp = int(message.get("internalDate", "0")) / 1000
            if timestamp < oldest or not {"INBOX", "CATEGORY_PERSONAL"}.issubset(
                set(message.get("labelIds", []))
            ):
                continue
            payload = message.get("payload", {})
            headers = {
                str(h.get("name", "")).lower(): str(h.get("value", ""))
                for h in payload.get("headers", [])
            }
            parts = _body_parts(payload)
            plain = [text for mime, text in parts if mime == "text/plain"]
            body = "\n".join(plain or [text for _, text in parts])[:12000]
            items.append(
                ImportItem(
                    ref,
                    body,
                    title=headers.get("subject", ""),
                    sender=headers.get("from", ""),
                    owner=account,
                    date=dt.datetime.fromtimestamp(timestamp, dt.UTC).isoformat(),
                    source_url=(
                        f"https://mail.google.com/mail/u/{quote(account, safe='')}/"
                        f"#all/{quote(message_id)}"
                    ),
                )
            )
            processed.add(ref)
        next_cursor = result.get("nextPageToken", "")
        if not next_cursor:
            return FetchResult(items, False, {})
        if next_cursor in seen:
            return FetchResult(items, True, {"cursor": cursor, "oldest": query_oldest})
        seen.add(next_cursor)
        cursor = next_cursor
    return FetchResult(items, True, {"cursor": cursor, "oldest": query_oldest})


def slack_messages(token: str, row: dict, processed: set[str]) -> FetchResult:
    selected = json.loads(row["channels"])
    if not selected:
        raise ValueError("先に取り込み対象の公開チャンネルを選んでください。")
    checkpoint = json.loads(row["checkpoint"])
    index = int(checkpoint.get("index", 0)) % len(selected)
    channel_id = selected[index]
    channel = public_channel(token, channel_id)
    scans = checkpoint.get("scans", {})
    scan = scans.get(channel_id, {})
    oldest = time.time() - 7 * 86400
    latest = scan.get("latest", time.time())
    # Slackの非Marketplaceアプリでは履歴取得が1分1回になるため、同一実行で次ページを連打しない。
    result = slack_call(
        "conversations.history",
        token,
        channel=channel_id,
        oldest=str(oldest),
        latest=str(latest),
        limit=15,
    )
    if len(result.get("messages", [])) > 15:
        raise ProviderError("invalid_response", "Slackの取得件数が上限を超えています。")
    items: list[ImportItem] = []
    for message in result.get("messages", []):
        ts = str(message.get("ts", ""))
        if (
            not re.fullmatch(r"\d+\.\d+", ts)
            or not oldest <= float(ts) <= float(latest)
            or message.get("subtype")
            or message.get("bot_id")
        ):
            continue
        ref = f"slack:{row['workspace']}:{channel_id}:{ts}"
        if ref in processed:
            continue
        items.append(
            ImportItem(
                ref,
                str(message.get("text", ""))[:12000],
                title=channel["name"],
                sender=str(message.get("user", "")),
                owner=row["account"],
                date=dt.datetime.fromtimestamp(float(ts), dt.UTC).isoformat(),
                source_url=(f"https://app.slack.com/archives/{channel_id}/p{ts.replace('.', '')}"),
            )
        )
    more = bool(result.get("has_more"))
    timestamps = [
        str(message.get("ts", ""))
        for message in result.get("messages", [])
        if re.fullmatch(r"\d+\.\d+", str(message.get("ts", "")))
        and oldest <= float(message["ts"]) < float(latest)
    ]
    if more and not timestamps:
        raise ProviderError("invalid_response", "Slackの続きの位置を確認できません。")
    covered = set(checkpoint.get("covered", [])) & set(selected)
    if more:
        scans[channel_id] = {"latest": min(timestamps, key=float)}
    else:
        scans.pop(channel_id, None)
        covered.add(channel_id)
    limited = bool(scans) or covered != set(selected)
    if not limited:
        covered.clear()
    return FetchResult(
        items,
        limited,
        {"index": (index + 1) % len(selected), "scans": scans, "covered": sorted(covered)},
    )
