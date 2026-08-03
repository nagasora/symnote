from __future__ import annotations

import sqlite3

from symnote.config import load_config
from symnote.core.backup import create_backup
from symnote.core.db import (
    delete_item,
    complete_task,
    create_recurring_task,
    fetch_inbox,
    fetch_tasks,
    fetch_tasks_for_morning_digest,
    fetch_tasks_for_today_view,
    fetch_weekly_review_sources,
    get_connection,
    init_db,
    insert_task,
)


class _TrackedConnection:
    """Delegate to SQLite while recording whether the connection was closed."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.closed = False

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

    def __enter__(self) -> "_TrackedConnection":
        self._connection.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        self._connection.__exit__(*args)

    def close(self) -> None:
        self.closed = True
        self._connection.close()


def test_database_helpers_close_connections(monkeypatch, tmp_path) -> None:
    """Read and write helpers leave no SQLite handle open."""
    database = tmp_path / "close.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    task_id = insert_task("Close the connection")

    import symnote.core.db as db

    tracked: list[_TrackedConnection] = []

    def tracked_connection() -> _TrackedConnection:
        raw_connection = sqlite3.connect(database)
        raw_connection.row_factory = sqlite3.Row
        item = _TrackedConnection(raw_connection)
        tracked.append(item)
        return item

    monkeypatch.setattr(db, "get_connection", tracked_connection)

    assert [item["raw_text"] for item in fetch_inbox()] == ["Close the connection"]
    delete_item(task_id)
    assert tracked and all(item.closed for item in tracked)


def test_default_database_path_is_user_data_directory(monkeypatch) -> None:
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/test/AppData/Local")
    assert load_config().db_path.endswith("SymNote\\symnote.db") or load_config().db_path.endswith(
        "SymNote/symnote.db"
    )


def test_init_migrates_legacy_task_dates(monkeypatch, tmp_path) -> None:
    database = tmp_path / "legacy.db"
    legacy = sqlite3.connect(database)
    legacy.execute(
        """CREATE TABLE items (
            id INTEGER PRIMARY KEY, created_at TEXT, date TEXT, kind TEXT,
            raw_text TEXT, ai_category TEXT, importance INTEGER, urgency INTEGER,
            effort TEXT, energy TEXT, status TEXT, tags TEXT, embedding BLOB
        )"""
    )
    legacy.execute("INSERT INTO items (date, kind, raw_text) VALUES ('2026-07-20', 'task', 'old')")
    legacy.commit()
    legacy.close()
    monkeypatch.setenv("DB_PATH", str(database))

    init_db()

    with sqlite3.connect(database) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 8
        due_date, due_time = check.execute(
            "SELECT due_date, due_time FROM items WHERE id = 1"
        ).fetchone()
        assert due_date == "2026-07-20"
        assert due_time is None
        assert check.execute("SELECT operation FROM calendar_sync_outbox").fetchone()[0] == "upsert"
        assert check.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'calendar_morning_digest_sync'"
        ).fetchone()[0] == "calendar_morning_digest_sync"


def test_connection_configuration_and_backup(monkeypatch, tmp_path) -> None:
    database = tmp_path / "symnote.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    insert_task("Back up the notes", due_date="2026-07-20")

    conn = get_connection()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()

    assert [task["raw_text"] for task in fetch_tasks_for_today_view("2026-07-20")] == [
        "Back up the notes"
    ]
    backup = create_backup(database, tmp_path / "backups")
    assert backup.is_file()
    with sqlite3.connect(backup) as check:
        assert check.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_tasks_without_deadline_are_not_treated_as_due(monkeypatch, tmp_path) -> None:
    database = tmp_path / "no-deadline.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    insert_task("No deadline", date_str="2026-07-01")
    assert fetch_tasks_for_today_view("2026-07-20") == []


def test_morning_digest_includes_unfinished_tasks_due_by_target(
    monkeypatch, tmp_path
) -> None:
    database = tmp_path / "morning-digest.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    overdue = insert_task("Overdue", due_date="2026-07-19")
    due_today = insert_task("Due today", due_date="2026-07-20")
    completed = insert_task("Completed", due_date="2026-07-18")
    insert_task("Future", due_date="2026-07-21")
    insert_task("No deadline", date_str="2026-07-01")
    complete_task(completed)

    tasks = fetch_tasks_for_morning_digest("2026-07-20")
    assert [(task["id"], task["raw_text"], task["due_date"]) for task in tasks] == [
        (overdue, "Overdue", "2026-07-19"),
        (due_today, "Due today", "2026-07-20"),
    ]


def test_weekly_sources_include_tasks_due_during_week(monkeypatch, tmp_path) -> None:
    database = tmp_path / "weekly.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    insert_task("Created earlier, due this week", date_str="2026-06-01", due_date="2026-07-20")
    assert [row["raw_text"] for row in fetch_weekly_review_sources("2026-07-20", "2026-07-26")["tasks"]] == [
        "Created earlier, due this week"
    ]


def test_task_changes_are_queued_for_calendar_sync(monkeypatch, tmp_path) -> None:
    database = tmp_path / "calendar-outbox.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    task_id = insert_task("Calendar task", due_date="2026-07-20")

    with sqlite3.connect(database) as check:
        assert check.execute(
            "SELECT operation FROM calendar_sync_outbox WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == "upsert"

    delete_item(task_id)
    with sqlite3.connect(database) as check:
        assert check.execute(
            "SELECT operation FROM calendar_sync_outbox WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == "delete"


def test_recurring_task_creates_the_next_occurrence_on_completion(monkeypatch, tmp_path) -> None:
    database = tmp_path / "recurrence.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    task_id = create_recurring_task(
        "Daily review", due_date="2026-07-20", frequency="daily", tags="review"
    )

    next_id = complete_task(task_id)
    assert next_id is not None
    with sqlite3.connect(database) as check:
        completed, next_task = check.execute(
            "SELECT status, due_date FROM items WHERE id IN (?, ?) ORDER BY id", (task_id, next_id)
        ).fetchall()
        assert completed == ("done", "2026-07-20")
        assert next_task == ("inbox", "2026-07-21")
        queued = check.execute(
            "SELECT COUNT(*) FROM calendar_sync_outbox"
        ).fetchone()[0]
        assert queued == 2
    assert complete_task(task_id) is None


def test_recurring_task_keeps_time_and_stops_at_end_date(monkeypatch, tmp_path) -> None:
    database = tmp_path / "recurrence-end.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    first_id = create_recurring_task(
        "Daily review",
        due_date="2026-07-20",
        due_time="18:30",
        frequency="daily",
        end_date="2026-07-21",
    )

    second_id = complete_task(first_id)
    assert second_id is not None
    assert complete_task(second_id) is None

    tasks = fetch_tasks(limit=10)
    assert {(task["due_date"], task["due_time"], task["status"]) for task in tasks} == {
        ("2026-07-20", "18:30", "done"),
        ("2026-07-21", "18:30", "done"),
    }
    with sqlite3.connect(database) as check:
        assert check.execute("SELECT active FROM task_recurrence_rules").fetchone()[0] == 0
