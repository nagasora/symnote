from __future__ import annotations

import sqlite3

import pytest

from symnote.config import load_config
from symnote.core.backup import create_backup
from symnote.core.db import (
    SCHEMA_VERSION,
    delete_item,
    complete_task,
    create_recurring_task,
    fetch_inbox,
    fetch_item,
    fetch_seeds,
    fetch_tasks,
    fetch_tasks_for_morning_digest,
    fetch_tasks_for_today_view,
    fetch_weekly_review_sources,
    get_connection,
    init_db,
    insert_task,
    list_trashed_items,
    restore_trashed_item,
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
        assert check.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
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


def test_delete_archives_and_restore_uses_new_id_without_external_bindings(
    monkeypatch, tmp_path
) -> None:
    database = tmp_path / "trash.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    with sqlite3.connect(database) as setup:
        setup.execute(
            "INSERT INTO goals (title, created_at, updated_at) VALUES (?, ?, ?)",
            ("Goal", "2026-10-22T00:00:00", "2026-10-22T00:00:00"),
        )
    task_id = create_recurring_task(
        "Full task details\nsecond line",
        due_date="2026-10-23",
        frequency="daily",
        tags="Visible title",
        due_time="18:30",
        status="week",
        importance=5,
        urgency=4,
        effort="long",
        energy="high",
        goal_id=1,
        estimate_minutes=90,
    )
    with sqlite3.connect(database) as setup:
        setup.execute(
            "UPDATE items SET date = '2026-10-20', delegated_to = 'codex' WHERE id = ?",
            (task_id,),
        )

    delete_item(task_id)

    assert fetch_item(task_id) is None
    trashed = list_trashed_items()
    assert len(trashed) == 1
    assert trashed[0]["original_item_id"] == task_id
    assert trashed[0]["title"] == "Visible title"
    trash_id = trashed[0]["id"]

    restored_id = restore_trashed_item(trash_id)
    assert restored_id != task_id
    restored = fetch_item(restored_id)
    assert restored is not None
    assert {
        key: restored[key]
        for key in (
            "raw_text", "date", "due_date", "due_time", "status", "tags",
            "importance", "urgency", "effort", "energy", "goal_id",
            "estimate_minutes", "delegated_to",
        )
    } == {
        "raw_text": "Full task details\nsecond line",
        "date": "2026-10-20",
        "due_date": "2026-10-23",
        "due_time": "18:30",
        "status": "week",
        "tags": "Visible title",
        "importance": 5,
        "urgency": 4,
        "effort": "long",
        "energy": "high",
        "goal_id": 1,
        "estimate_minutes": 90,
        "delegated_to": "codex",
    }
    assert restored["recurrence_rule_id"] is None
    assert list_trashed_items() == []
    with pytest.raises(LookupError):
        restore_trashed_item(trash_id)
    with sqlite3.connect(database) as check:
        assert check.execute(
            "SELECT operation FROM calendar_sync_outbox WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == "delete"
        assert check.execute(
            "SELECT operation FROM calendar_sync_outbox WHERE task_id = ?", (restored_id,)
        ).fetchone()[0] == "upsert"


def test_v16_to_v18_migrations_are_additive_and_repeatable(monkeypatch, tmp_path) -> None:
    database = tmp_path / "v16-trash.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    item_id = insert_task("Keep me")
    with sqlite3.connect(database) as legacy:
        legacy.execute("DROP TABLE item_trash")
        legacy.execute("PRAGMA user_version = 16")

    init_db()
    init_db()

    assert fetch_item(item_id) is not None
    with sqlite3.connect(database) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 18
        columns = [row[1] for row in check.execute("PRAGMA table_info(item_trash)")]
        assert columns[:3] == ["id", "original_item_id", "created_at"]
        bonus_columns = [row[1] for row in check.execute("PRAGMA table_info(quest_bonus_log)")]
        assert bonus_columns == ["id", "kind", "ref", "xp", "gold", "created_at", "seen"]


def test_v17_to_v18_bonus_migration_preserves_items(monkeypatch, tmp_path) -> None:
    database = tmp_path / "v17-bonus.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    item_id = insert_task("Keep through v18")
    with sqlite3.connect(database) as legacy:
        legacy.execute("DROP TABLE quest_bonus_log")
        legacy.execute("PRAGMA user_version = 17")

    init_db()
    init_db()

    assert fetch_item(item_id) is not None
    with sqlite3.connect(database) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 18
        assert check.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='quest_bonus_log'"
        ).fetchone() == ("quest_bonus_log",)


def test_fetch_seeds_returns_only_unadopted_idea_and_someday_memos(
    monkeypatch, tmp_path
) -> None:
    database = tmp_path / "seeds.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    from symnote.core.db import insert_item

    idea = insert_item(kind="memo", raw_text="Idea", ai_category="idea")
    someday = insert_item(kind="memo", raw_text="Someday", ai_category="someday")
    insert_item(kind="memo", raw_text="Ordinary", ai_category="memo")
    insert_item(kind="task", raw_text="Task idea", ai_category="idea")
    with sqlite3.connect(database) as setup:
        setup.execute(
            "INSERT INTO goals(title, created_at, updated_at) VALUES ('Map','now','now')"
        )
        setup.execute("UPDATE items SET goal_id=1 WHERE id=?", (idea,))

    assert [row["id"] for row in fetch_seeds()] == [someday]


def test_restore_drops_goal_link_when_goal_no_longer_exists(monkeypatch, tmp_path) -> None:
    database = tmp_path / "trash-missing-goal.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    with sqlite3.connect(database) as setup:
        setup.execute(
            "INSERT INTO goals (title, created_at, updated_at) VALUES (?, ?, ?)",
            ("Temporary", "2026-10-22T00:00:00", "2026-10-22T00:00:00"),
        )
    task_id = insert_task("Task", goal_id=1)
    delete_item(task_id)
    trash_id = list_trashed_items()[0]["id"]
    with sqlite3.connect(database) as setup:
        setup.execute("DELETE FROM goals WHERE id = 1")

    restored = fetch_item(restore_trashed_item(trash_id))

    assert restored is not None
    assert restored["goal_id"] is None


def test_restore_does_not_reuse_another_deleted_tasks_calendar_id(monkeypatch, tmp_path) -> None:
    database = tmp_path / "trash-calendar-id.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    first = insert_task("First")
    second = insert_task("Second")
    with sqlite3.connect(database) as setup:
        setup.execute(
            "INSERT INTO calendar_task_sync (task_id, event_id) VALUES (?, ?)",
            (second, "event-b"),
        )

    delete_item(first)
    first_trash_id = next(
        row["id"] for row in list_trashed_items() if row["original_item_id"] == first
    )
    delete_item(second)

    restored = restore_trashed_item(first_trash_id)

    assert restored > second
    with sqlite3.connect(database) as check:
        assert check.execute(
            "SELECT event_id FROM calendar_task_sync WHERE task_id = ?", (second,)
        ).fetchone()[0] == "event-b"
        assert check.execute(
            "SELECT operation FROM calendar_sync_outbox WHERE task_id = ?", (second,)
        ).fetchone()[0] == "delete"
        assert check.execute(
            "SELECT operation FROM calendar_sync_outbox WHERE task_id = ?", (restored,)
        ).fetchone()[0] == "upsert"


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


def test_v15_recurrence_migration_preserves_rules(monkeypatch, tmp_path) -> None:
    """v15のID・間隔・終了日・停止状態を変えずにv16へ移行する。"""
    database = tmp_path / "v15-recurrence.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    first = create_recurring_task("毎日", "2026-07-20", "daily", end_date="2026-08-01")
    second = create_recurring_task("隔週", "2026-07-20", "weekly", interval=2)
    with sqlite3.connect(database) as legacy:
        legacy.execute("UPDATE task_recurrence_rules SET active=0 WHERE id=2")
        expected = legacy.execute(
            "SELECT id, frequency, interval_days, end_date, active, created_at FROM task_recurrence_rules ORDER BY id"
        ).fetchall()
        legacy.execute("ALTER TABLE task_recurrence_rules RENAME TO extended_rules")
        legacy.execute(
            "CREATE TABLE task_recurrence_rules ("
            "id INTEGER PRIMARY KEY, frequency TEXT NOT NULL CHECK(frequency IN ('daily', 'weekly')), "
            "interval_days INTEGER NOT NULL CHECK(interval_days > 0), end_date TEXT, "
            "active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)), created_at TEXT NOT NULL)"
        )
        legacy.execute(
            "INSERT INTO task_recurrence_rules SELECT id, frequency, interval_days, end_date, active, created_at FROM extended_rules"
        )
        legacy.execute("DROP TABLE extended_rules")
        legacy.execute("PRAGMA user_version = 15")
    init_db()
    init_db()
    with sqlite3.connect(database) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 18
        assert (
            check.execute(
                "SELECT id, frequency, interval_days, end_date, active, created_at FROM task_recurrence_rules ORDER BY id"
            ).fetchall()
            == expected
        )
        assert check.execute(
            "SELECT weekdays, month_day FROM task_recurrence_rules"
        ).fetchall() == [("", None), ("", None)]
        assert check.execute("SELECT recurrence_rule_id FROM items ORDER BY id").fetchall() == [
            (1,),
            (2,),
        ]
    next_id = complete_task(first)
    assert next_id is not None
    assert next(task for task in fetch_tasks() if task["id"] == next_id)["due_date"] == "2026-07-21"
    assert complete_task(second) is None
