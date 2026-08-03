from __future__ import annotations

import sqlite3
from datetime import date

from symnote.config import load_config
from symnote.core.db import delete_item, init_db, insert_task
from symnote.core.google_calendar import (
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
        return _Request({"id": "event-1"})

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


def test_sync_creates_then_deletes_mapped_event(monkeypatch, tmp_path) -> None:
    database = tmp_path / "calendar.db"
    monkeypatch.setenv("DB_PATH", str(database))
    monkeypatch.setenv("GOOGLE_CALENDAR_MORNING_DIGEST_LOOKAHEAD_DAYS", "1")
    init_db()
    task_id = insert_task("Ship release", due_date="2026-07-20")
    service = _Service()

    result = sync_pending_tasks(
        load_config(), service_factory=lambda: service, today=date(2026, 7, 20)
    )
    assert result == result.__class__(synced=2, failed=0, pending=0)
    assert service.events_api.insert_calls[0]["body"]["summary"] == "SymNote: 今日の課題 1件（期限切れ 0件）"

    delete_item(task_id)
    result = sync_pending_tasks(
        load_config(), service_factory=lambda: service, today=date(2026, 7, 20)
    )
    assert result.synced == 2
    assert service.events_api.delete_calls == [{"calendarId": "primary", "eventId": "event-1"}]
    with sqlite3.connect(database) as check:
        assert check.execute("SELECT COUNT(*) FROM calendar_sync_outbox").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM calendar_task_sync").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM calendar_morning_digest_sync").fetchone()[0] == 0


def test_sync_recreates_morning_digest_removed_from_google_calendar(monkeypatch, tmp_path) -> None:
    database = tmp_path / "calendar.db"
    monkeypatch.setenv("DB_PATH", str(database))
    monkeypatch.setenv("GOOGLE_CALENDAR_MORNING_DIGEST_LOOKAHEAD_DAYS", "1")
    init_db()
    task_id = insert_task("Ship release", due_date="2026-07-20")
    service = _Service()
    sync_pending_tasks(load_config(), service_factory=lambda: service, today=date(2026, 7, 20))

    class _NotFoundError(Exception):
        class resp:
            status = 404

    service.events_api.update_error = _NotFoundError()
    from symnote.core.db import update_item_fields

    update_item_fields(task_id, raw_text="Ship release v2")
    result = sync_pending_tasks(
        load_config(), service_factory=lambda: service, today=date(2026, 7, 20)
    )

    assert result == result.__class__(synced=2, failed=0, pending=0)
    assert len(service.events_api.update_calls) == 1
    assert len(service.events_api.insert_calls) == 2
    with sqlite3.connect(database) as check:
        assert check.execute(
            "SELECT event_id FROM calendar_morning_digest_sync WHERE digest_date = ?", ("2026-07-20",)
        ).fetchone()[0] == "event-1"


def test_morning_digest_failure_keeps_delivery_state_unmapped(monkeypatch, tmp_path) -> None:
    database = tmp_path / "calendar-digest-failure.db"
    monkeypatch.setenv("DB_PATH", str(database))
    monkeypatch.setenv("GOOGLE_CALENDAR_MORNING_DIGEST_LOOKAHEAD_DAYS", "1")
    init_db()
    insert_task("Today", due_date="2026-07-20")
    service = _Service()
    service.events_api.insert_error = RuntimeError("offline")

    result = sync_pending_tasks(
        load_config(), service_factory=lambda: service, today=date(2026, 7, 20)
    )

    assert result == result.__class__(synced=1, failed=1, pending=1, message="offline")
    with sqlite3.connect(database) as check:
        assert check.execute("SELECT COUNT(*) FROM calendar_morning_digest_sync").fetchone()[0] == 0


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

    assert result == result.__class__(synced=2, failed=0, pending=0)
    assert service.events_api.delete_calls == [
        {"calendarId": "primary", "eventId": "legacy-task-event"},
        {"calendarId": "primary", "eventId": "future-event"}
    ]
    with sqlite3.connect(database) as check:
        assert check.execute("SELECT COUNT(*) FROM calendar_morning_digest_sync").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM calendar_task_sync").fetchone()[0] == 0
