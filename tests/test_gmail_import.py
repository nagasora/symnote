"""Gmail 取り込みがモックの service だけで動き、処理済み記録と再試行が正しいことを保証するテスト。"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

import pytest

from symnote.core import gmail_import
from symnote.core.candidates import list_candidates
from symnote.core.db import connection, init_db
from symnote.core.llm import LLMUnavailableError


@pytest.fixture(autouse=True)
def _db() -> None:
    """候補テーブルを持つ一時 DB を用意する。"""
    init_db()


def _b64(text: str) -> str:
    """Gmail API と同じ URL セーフ Base64（パディングなし）にする。"""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode().rstrip("=")


def _message(message_id: str, subject: str, plain: Optional[str] = None, html: Optional[str] = None) -> Dict[str, Any]:
    """multipart/alternative と添付ファイルを持つメールを作る。"""
    parts: List[Dict[str, Any]] = []
    if plain is not None:
        parts.append({"mimeType": "text/plain", "headers": [], "body": {"data": _b64(plain)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "headers": [], "body": {"data": _b64(html)}})
    parts.append({"mimeType": "text/plain", "filename": "secret.txt", "body": {"data": _b64("添付の中身")}})
    return {"id": message_id, "threadId": f"t-{message_id}", "payload": {
        "mimeType": "multipart/mixed",
        "headers": [{"name": "Subject", "value": subject}, {"name": "From", "value": "boss@example.com"},
                    {"name": "Date", "value": "Thu, 15 Oct 2026 09:00:00 +0900"}],
        "parts": parts,
    }}


class _Call:
    """execute() で値を返す API 呼び出しの代役。"""

    def __init__(self, value: Any) -> None:
        self.value = value

    def execute(self) -> Any:
        return self.value


class FakeGmail:
    """users().messages().list/get と getProfile だけを持つ Gmail service の代役。"""

    def __init__(self, messages: List[Dict[str, Any]], page_size: int = 100) -> None:
        self.store = {m["id"]: m for m in messages}
        self.order = [m["id"] for m in messages]
        self.page_size = page_size
        self.list_calls: List[Dict[str, Any]] = []
        self.get_calls: List[str] = []

    def users(self) -> "FakeGmail":
        return self

    def messages(self) -> "FakeGmail":
        return self

    def getProfile(self, userId: str) -> _Call:  # noqa: N802 (Google API の名前に合わせる)
        return _Call({"emailAddress": "me@example.com"})

    def list(self, **params: Any) -> _Call:
        self.list_calls.append(params)
        start = int(params.get("pageToken") or 0)
        ids = self.order[start:start + self.page_size]
        page: Dict[str, Any] = {"messages": [{"id": i} for i in ids]}
        if start + self.page_size < len(self.order):
            page["nextPageToken"] = str(start + self.page_size)
        return _Call(page)

    def get(self, userId: str, id: str, format: str) -> _Call:  # noqa: A002
        self.get_calls.append(id)
        return _Call(self.store[id])


class FakeAI:
    """呼び出し回数を数え、決められた応答を返す generate_json の代役。"""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.prompts: List[str] = []

    def __call__(self, prompt: str, *args: Any, **kwargs: Any) -> Any:
        self.prompts.append(prompt)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_fetch_reads_inbox_without_promotions_and_prefers_plain_text() -> None:
    """受信トレイ（プロモーション・ソーシャル除外）から件名・送信者・日付・プレーン本文を取る。"""
    service = FakeGmail([_message("m1", "見積の件", plain="見積書を送ってください", html="<p>HTML版</p>")])

    [item] = gmail_import.fetch_recent_messages(days=3, max_messages=5, service_factory=lambda: service)

    query = service.list_calls[0]["q"]
    assert "newer_than:3d" in query and "-category:promotions" in query
    assert "-category:social" in query and "in:inbox" in query
    assert (item.title, item.sender, item.text) == ("見積の件", "boss@example.com", "見積書を送ってください")
    assert item.date.startswith("Thu, 15 Oct 2026")
    assert item.source_ref == "gmail:me@example.com:m1"
    assert item.source_url == "https://mail.google.com/mail/u/me%40example.com/#all/t-m1"
    assert "添付の中身" not in item.text


def test_html_only_mail_is_converted_to_text_and_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTML だけのメールはスクリプトを除いたテキストにし、上限文字数で切る。"""
    monkeypatch.setattr(gmail_import, "MAX_BODY_CHARACTERS", 20)
    html = "<style>.x{}</style><script>alert(1)</script><p>会議資料を準備する</p>" + "<p>長文</p>" * 50
    service = FakeGmail([_message("m1", "件名", html=html)])

    [item] = gmail_import.fetch_recent_messages(service_factory=lambda: service)

    assert "alert" not in item.text and ".x" not in item.text
    assert "会議資料を準備する" in item.text and len(item.text) <= 20


def test_max_messages_and_pagination() -> None:
    """ページをまたいで取得し、最大件数で止める。"""
    service = FakeGmail([_message(f"m{i}", f"件名{i}", plain="本文") for i in range(7)], page_size=3)

    items = gmail_import.fetch_recent_messages(max_messages=5, service_factory=lambda: service)

    assert [item.source_ref.rsplit(":", 1)[1] for item in items] == ["m0", "m1", "m2", "m3", "m4"]
    assert len(service.list_calls) == 2


def test_invalid_ranges_are_rejected() -> None:
    """日数・件数の範囲外指定は ValueError にする。"""
    with pytest.raises(ValueError):
        gmail_import.fetch_recent_messages(days=0, service_factory=lambda: FakeGmail([]))
    with pytest.raises(ValueError):
        gmail_import.fetch_recent_messages(max_messages=101, service_factory=lambda: FakeGmail([]))


def test_service_failure_becomes_japanese_error() -> None:
    """API の失敗は日本語の GmailImportError になる。"""
    def broken() -> Any:
        raise OSError("network down")

    with pytest.raises(gmail_import.GmailImportError, match="Gmail を取得できませんでした"):
        gmail_import.fetch_recent_messages(service_factory=broken)


def test_processed_messages_are_skipped_on_next_import() -> None:
    """候補保存に成功したメールは処理済みになり、次回は取得も AI 呼び出しもしない。"""
    service = FakeGmail([_message("m1", "見積の件", plain="見積書を送ってください")])
    ai = FakeAI({"candidates": [{
        "source_ref": "gmail:me@example.com:m1", "title": "見積書を送る", "details": "",
        "excerpt": "見積書を送って", "due_date": None, "due_time": None, "confidence": 0.8,
    }]})

    first = gmail_import.import_recent_messages(service_factory=lambda: service, generate_json=ai)
    second = gmail_import.import_recent_messages(service_factory=lambda: service, generate_json=ai)

    assert first.created_count == 1 and first.processed_refs == ["gmail:me@example.com:m1"]
    assert second.processed_refs == [] and second.created_count == 0
    assert len(ai.prompts) == 1 and service.get_calls == ["m1"]
    assert [row["source_url"] for row in list_candidates("pending")] == [
        "https://mail.google.com/mail/u/me%40example.com/#all/t-m1"
    ]


def test_failed_extraction_is_retried_next_time() -> None:
    """AI が使えなかったメールは処理済みにせず、次回に再試行する。"""
    service = FakeGmail([_message("m1", "件名", plain="本文")])
    failing = FakeAI(LLMUnavailableError("Gemini の利用上限に達しています。"))

    result = gmail_import.import_recent_messages(service_factory=lambda: service, generate_json=failing)

    assert result.processed_refs == [] and "利用上限" in result.message
    with connection() as conn:
        assert conn.execute("SELECT count(*) FROM gmail_import_processed").fetchone()[0] == 0

    retry = gmail_import.import_recent_messages(service_factory=lambda: service, generate_json=FakeAI([]))

    assert retry.processed_refs == ["gmail:me@example.com:m1"]
    assert service.get_calls == ["m1", "m1"]


def test_test_mode_refuses_connect_disconnect_and_real_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """テスト環境では接続・解除・保存済み資格情報の利用を拒否し、未接続扱いにする。"""
    monkeypatch.setenv("SYMNOTE_TEST_MODE", "1")

    def keyring_must_not_be_used() -> Any:
        raise AssertionError("テスト環境で keyring に触れてはいけない")

    monkeypatch.setattr(gmail_import, "_keyring", keyring_must_not_be_used)

    assert gmail_import.is_connected() is False
    with pytest.raises(gmail_import.GmailConnectionBlocked):
        gmail_import.connect()
    with pytest.raises(gmail_import.GmailConnectionBlocked):
        gmail_import.disconnect()
    with pytest.raises(gmail_import.GmailConnectionBlocked):
        gmail_import.fetch_recent_messages()


def test_gmail_uses_its_own_keyring_entry_and_readonly_scope() -> None:
    """Gmail は Calendar と別の keyring エントリを使い、スコープは gmail.readonly だけ。"""
    from symnote.core import google_calendar

    assert gmail_import.SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]
    calendar_service = getattr(google_calendar, "KEYRING_SERVICE", None)
    assert gmail_import.KEYRING_SERVICE != calendar_service
