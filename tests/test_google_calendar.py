from __future__ import annotations

import sqlite3
from datetime import date

from symnote.config import load_config
from symnote.core.db import delete_item, init_db, insert_task
from symnote.core.google_calendar import (
    SyncResult,
    morning_digest_event_id,
    morning_digest_event_payload,
    sync_pending_tasks,
    task_event_payload,
)


class _Request:
    def __init__(self, result: dict | None = None) -> None:
        self.result = result or {}

    def execute(self) -> dict:
        return self.result


class _Events:
    def __init__(self) -> None:
        self.insert_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self.update_error: Exception | None = None
        self.insert_error: Exception | None = None

    def insert(self, **kwargs: object) -> _Request:
        self.insert_calls.append(kwargs)
        if self.insert_error:
            raise self.insert_error
        return _Request({"id": kwargs["body"].get("id", "event-1")})

    def update(self, **kwargs: object) -> _Request:
        self.update_calls.append(kwargs)
        if self.update_error:
            raise self.update_error
        return _Request()

    def delete(self, **kwargs: object) -> _Request:
        self.delete_calls.append(kwargs)
        return _Request()


class _Service:
    def __init__(self) -> None:
        self.events_api = _Events()

    def events(self) -> _Events:
        return self.events_api


def test_event_payload_is_all_day_and_has_popup_reminder() -> None:
    payload = task_event_payload(
        {"id": 3, "tags": "Ship", "raw_text": "Ship release", "due_date": "2026-07-20", "status": "week"},
        30,
    )
    assert payload is not None
    assert payload["start"] == {"dateTime": "2026-07-20T09:00:00", "timeZone": "Asia/Tokyo"}
    assert payload["end"] == {"dateTime": "2026-07-20T09:30:00", "timeZone": "Asia/Tokyo"}
    assert payload["reminders"]["overrides"] == [{"method": "popup", "minutes": 30}]


def test_event_payload_uses_the_task_due_time_when_present() -> None:
    payload = task_event_payload(
        {
            "id": 3,
            "tags": "Ship",
            "raw_text": "Ship release",
            "due_date": "2026-07-20",
            "due_time": "14:45",
            "status": "week",
        },
        30,
    )
    assert payload is not None
    assert payload["start"]["dateTime"] == "2026-07-20T14:45:00"
    assert payload["end"]["dateTime"] == "2026-07-20T15:15:00"


def test_morning_digest_payload_includes_due_today_and_overdue() -> None:
    payload = morning_digest_event_payload(
        "2026-07-20",
        [
            {"id": 1, "tags": "Today title", "raw_text": "Today details", "due_date": "2026-07-20"},
            {"id": 2, "tags": "Late title", "raw_text": "Late details", "due_date": "2026-07-19"},
        ],
    )

    assert payload is not None
    assert payload["summary"] == "SymNote: 今日の課題 1件（期限切れ 1件）"
    assert "今日が期限（1件）\n- Today title" in payload["description"]
    assert "期限切れ（1件）\n- Late title" in payload["description"]
    assert payload["start"]["dateTime"] == "2026-07-20T08:00:00"
    assert payload["reminders"]["overrides"] == [{"method": "popup", "minutes": 0}]


class _HttpError(Exception):
    """Google API のエラーを模した例外。"""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.resp = type("resp", (), {"status": status})()


def _sync(service: _Service, today: date = date(2026, 7, 20)):
    """テスト用の同期呼び出し。"""
    return sync_pending_tasks(load_config(), service_factory=lambda: service, today=today)


def _mapping(database) -> tuple | None:
    """朝のまとめの記録（event_id, calendar_id）を返す。"""
    with sqlite3.connect(database) as check:
        return check.execute(
            "SELECT event_id, calendar_id FROM calendar_morning_digest_sync"
        ).fetchone()


def test_sync_creates_digest_with_fixed_id_then_deletes_it(monkeypatch, tmp_path) -> None:
    """期限タスクがあれば固定 ID で朝のまとめを作り、無くなれば削除する。"""
    database = tmp_path / "calendar.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    task_id = insert_task("Ship release", due_date="2026-07-20")
    service = _Service()

    assert _sync(service) == SyncResult(synced=1, failed=0, pending=0)
    inserted = service.events_api.insert_calls[0]["body"]
    assert inserted["id"] == morning_digest_event_id("2026-07-20") == "sndigest20260720"
    assert inserted["summary"] == "SymNote: 今日の課題 1件（期限切れ 0件）"
    assert _mapping(database) == ("sndigest20260720", "primary")

    delete_item(task_id)
    assert _sync(service).synced == 1
    assert service.events_api.delete_calls == [
        {"calendarId": "primary", "eventId": "sndigest20260720"}
    ]
    with sqlite3.connect(database) as check:
        assert check.execute("SELECT COUNT(*) FROM calendar_sync_outbox").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM calendar_morning_digest_sync").fetchone()[0] == 0


def test_task_changes_alone_do_not_contact_calendar(monkeypatch, tmp_path) -> None:
    """まとめの内容が変わらない変更では Google に接続しない。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "calendar-noop.db"))
    init_db()
    insert_task("期限なし")
    calls: list[int] = []

    result = sync_pending_tasks(
        load_config(), service_factory=lambda: calls.append(1), today=date(2026, 7, 20)
    )

    assert result.synced == 0 and calls == []


def test_retry_after_lost_insert_response_updates_instead_of_duplicating(
    monkeypatch, tmp_path
) -> None:
    """前回の挿入が届いていて 409 になった場合は、同じ ID の予定を更新する。"""
    database = tmp_path / "calendar-409.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    insert_task("Ship release", due_date="2026-07-20")
    service = _Service()
    service.events_api.insert_error = _HttpError(409)

    assert _sync(service) == SyncResult(synced=1, failed=0, pending=0)
    assert service.events_api.update_calls[0]["eventId"] == "sndigest20260720"
    assert service.events_api.update_calls[0]["body"]["status"] == "confirmed"
    assert _mapping(database) == ("sndigest20260720", "primary")


def test_sync_recreates_legacy_digest_removed_from_google_calendar(monkeypatch, tmp_path) -> None:
    """固定 ID 導入前の予定が消されていたら、固定 ID で作り直す。"""
    database = tmp_path / "calendar.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    insert_task("Ship release", due_date="2026-07-20")
    with sqlite3.connect(database) as check:
        check.execute(
            "INSERT INTO calendar_morning_digest_sync "
            "(digest_date, event_id, content_hash, last_synced_at) "
            "VALUES ('2026-07-20', 'random-old-id', 'old', '2026-07-20T07:00:00')"
        )
    service = _Service()
    service.events_api.update_error = _HttpError(404)

    assert _sync(service) == SyncResult(synced=1, failed=0, pending=0)
    assert service.events_api.update_calls[0]["eventId"] == "random-old-id"
    assert service.events_api.insert_calls[0]["body"]["id"] == "sndigest20260720"
    assert _mapping(database) == ("sndigest20260720", "primary")


def test_changing_calendar_moves_digest_to_new_calendar(monkeypatch, tmp_path) -> None:
    """対象カレンダーを変えると、旧カレンダーの予定を消して新しい側に作る。"""
    database = tmp_path / "calendar-move.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    insert_task("Ship release", due_date="2026-07-20")
    service = _Service()
    _sync(service)

    monkeypatch.setenv("GOOGLE_CALENDAR_ID", "work@example.com")
    assert _sync(service) == SyncResult(synced=2, failed=0, pending=0)

    assert service.events_api.delete_calls == [
        {"calendarId": "primary", "eventId": "sndigest20260720"}
    ]
    assert service.events_api.insert_calls[-1]["calendarId"] == "work@example.com"
    assert _mapping(database) == ("sndigest20260720", "work@example.com")


def test_today_follows_configured_timezone(monkeypatch) -> None:
    """「今日」は端末ではなく設定タイムゾーンの日付で決まる。"""
    import datetime as dt

    import symnote.core.google_calendar as calendar

    class _FixedDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 7, 19, 16, 0, tzinfo=dt.UTC).astimezone(tz)

    monkeypatch.setattr(calendar.dt, "datetime", _FixedDatetime)
    assert calendar.today_in_timezone("Asia/Tokyo") == date(2026, 7, 20)
    assert calendar.today_in_timezone("America/New_York") == date(2026, 7, 19)
    assert isinstance(calendar.today_in_timezone("Not/AZone"), date)


def test_morning_digest_failure_keeps_delivery_state_unmapped(monkeypatch, tmp_path) -> None:
    """送信に失敗したら記録を残さず、次回に再試行される。"""
    database = tmp_path / "calendar-digest-failure.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    insert_task("Today", due_date="2026-07-20")
    service = _Service()
    service.events_api.insert_error = RuntimeError("offline")

    assert _sync(service) == SyncResult(synced=0, failed=1, pending=1, message="offline")
    assert _mapping(database) is None


def test_sync_removes_future_digest_events_from_legacy_lookahead(monkeypatch, tmp_path) -> None:
    database = tmp_path / "calendar-legacy-future.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    with sqlite3.connect(database) as check:
        check.execute(
            """
            INSERT INTO calendar_morning_digest_sync
              (digest_date, event_id, content_hash, last_synced_at)
            VALUES ('2026-07-21', 'future-event', 'old-hash', '2026-07-20T08:00:00')
            """
        )
        check.execute(
            """
            INSERT INTO calendar_task_sync (task_id, event_id, last_synced_at)
            VALUES (99, 'legacy-task-event', '2026-07-20T08:00:00')
            """
        )
        check.commit()

    service = _Service()
    result = sync_pending_tasks(
        load_config(), service_factory=lambda: service, today=date(2026, 7, 20)
    )

    assert result == SyncResult(synced=2, failed=0, pending=0)
    assert service.events_api.delete_calls == [
        {"calendarId": "primary", "eventId": "legacy-task-event"},
        {"calendarId": "primary", "eventId": "future-event"}
    ]
    with sqlite3.connect(database) as check:
        assert check.execute("SELECT COUNT(*) FROM calendar_morning_digest_sync").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM calendar_task_sync").fetchone()[0] == 0


def test_calendar_sync_command_exit_codes(monkeypatch, tmp_path) -> None:
    """定期実行コマンドは相対パスで 2、未接続で 0、同期失敗で 1 を返す。"""
    import symnote.calendar_sync as command

    monkeypatch.setenv("DB_PATH", "./symnote.db")
    assert command.run() == 2

    monkeypatch.setenv("DB_PATH", str(tmp_path / "command.db"))
    monkeypatch.setattr(command, "is_connected", lambda: False)
    assert command.run() == 0

    monkeypatch.setattr(command, "is_connected", lambda: True)
    monkeypatch.setattr(
        command, "sync_pending_tasks", lambda config: SyncResult(failed=1, pending=1, message="x")
    )
    assert command.run() == 1
