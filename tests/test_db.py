from __future__ import annotations

import sqlite3

from symnote.config import load_config
from symnote.core.backup import create_backup
from symnote.core.db import (
    delete_item,
    fetch_inbox,
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
        assert check.execute("PRAGMA user_version").fetchone()[0] == 3
        due_date = check.execute("SELECT due_date FROM items WHERE id = 1").fetchone()[0]
        assert due_date == "2026-07-20"


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


def test_weekly_sources_include_tasks_due_during_week(monkeypatch, tmp_path) -> None:
    database = tmp_path / "weekly.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    insert_task("Created earlier, due this week", date_str="2026-06-01", due_date="2026-07-20")
    assert [row["raw_text"] for row in fetch_weekly_review_sources("2026-07-20", "2026-07-26")["tasks"]] == [
        "Created earlier, due this week"
    ]
