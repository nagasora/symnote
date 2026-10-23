from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from symnote.core import db, postgres

TEST_URL_ENV = "SYMNOTE_TEST_POSTGRES_DSN"


@pytest.fixture
def pg_url(monkeypatch: pytest.MonkeyPatch) -> str:
    url = os.getenv(TEST_URL_ENV)
    if not url:
        pytest.skip(f"{TEST_URL_ENV} is required for PostgreSQL integration tests")
    monkeypatch.setenv("SYMNOTE_STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", url)
    return url


@pytest.fixture
def tenant(pg_url: str):
    user_id = f"pytest-{uuid.uuid4()}"
    with postgres.user_schema(user_id) as schema:
        db.init_db()
        yield schema


def test_postgres_is_strictly_opt_in_and_requires_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYMNOTE_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert postgres.enabled() is False
    monkeypatch.setenv("SYMNOTE_STORAGE_BACKEND", "postgres")
    assert postgres.enabled() is True
    with pytest.raises(postgres.ConfigurationError, match="requires DATABASE_URL"):
        postgres.connect_data()


def test_schema_name_is_safe_stable_and_scoped() -> None:
    first = postgres.schema_for_user("user@example.com")
    assert first == postgres.schema_for_user("user@example.com")
    assert first.startswith("symnote_user_")
    assert "@" not in first
    assert postgres.current_schema() == postgres.OWNER_SCHEMA
    with postgres.user_schema("user@example.com"):
        assert postgres.current_schema() == first
    assert postgres.current_schema() == postgres.OWNER_SCHEMA


def test_postgres_snapshot_matches_sqlite_v18_columns(
    pg_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from symnote.config import use_db_path

    sqlite_path = tmp_path / "snapshot.db"
    monkeypatch.setenv("SYMNOTE_STORAGE_BACKEND", "sqlite")
    with use_db_path(str(sqlite_path)):
        db.init_db()
        with db.connection() as conn, conn:
            conn.execute(
                "CREATE TABLE gmail_import_processed ("
                "source_ref TEXT PRIMARY KEY, processed_at TEXT NOT NULL)"
            )
    with sqlite3.connect(sqlite_path) as conn:
        sqlite_tables = {
            row[0]: [column[1] for column in conn.execute(f'PRAGMA table_info("{row[0]}")')]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    monkeypatch.setenv("SYMNOTE_STORAGE_BACKEND", "postgres")
    user_id = f"pytest-snapshot-{uuid.uuid4()}"
    with postgres.user_schema(user_id):
        db.init_db()
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = ? AND table_name NOT LIKE '_symnote_%'
                ORDER BY table_name, ordinal_position
                """,
                (postgres.current_schema(),),
            ).fetchall()
    postgres_tables: dict[str, list[str]] = {}
    for row in rows:
        postgres_tables.setdefault(row["table_name"], []).append(row["column_name"])
    assert postgres_tables == sqlite_tables


def test_qmark_lexer_preserves_literals_and_comments() -> None:
    sql = "SELECT '?', ? -- ?\n, \"?\", /* ? */ ?"
    assert postgres._convert_qmark(sql) == "SELECT '?', %s -- ?\n, \"?\", /* ? */ %s"
    assert postgres._convert_qmark("SELECT '100%', 5 % 2, ?") == (
        "SELECT '100%%', 5 %% 2, %s"
    )


def test_connection_timeout_defaults_and_preserves_explicit_value() -> None:
    assert postgres._connect_options("postgresql://example.test/symnote") == {
        "application_name": "symnote",
        "connect_timeout": 10,
    }
    assert postgres._connect_options(
        "postgresql://example.test/symnote?connect_timeout=3"
    ) == {"application_name": "symnote"}


def test_schema_crud_rows_lastrowid_and_known_sql_adaptations(tenant: str) -> None:
    item_id = db.insert_task("PostgreSQL task", due_date="2026-10-22", tags="PG")
    assert item_id > 0
    with db.connection() as conn, conn:
        row = conn.execute("SELECT id, raw_text FROM items WHERE id = ?", (item_id,)).fetchone()
        assert row is not None
        assert row[0] == row["id"] == item_id
        assert row.keys() == ["id", "raw_text"]
        assert dict(row)["raw_text"] == "PostgreSQL task"

        first = conn.execute(
            "INSERT OR IGNORE INTO achievements(code, unlocked_at) VALUES (?, ?)",
            ("pg-test", "2026-10-21T00:00:00"),
        )
        duplicate = conn.execute(
            "INSERT OR IGNORE INTO achievements(code, unlocked_at) VALUES (?, ?)",
            ("pg-test", "2026-10-21T00:00:01"),
        )
        assert first.rowcount == 1
        assert duplicate.rowcount == 0
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE items SET importance = MAX(COALESCE(importance, 0), 5) WHERE id = ?",
            (item_id,),
        )
    assert db.fetch_item(item_id)["importance"] == 5


def test_runtime_sql_bridges_execute_on_postgres(tenant: str) -> None:
    from symnote.core import calendar_settings, gmail_import, oauth_store

    task_id = db.insert_task("MixedCase searchable task")
    assert [row["id"] for row in db.search_items("mixedcase")] == [task_id]
    calendar_settings.set_setting("export_tasks", "on")
    with db.connection() as conn:
        changed_at = conn.execute(
            "SELECT changed_at FROM calendar_sync_outbox WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        values = conn.execute(
            "SELECT '100%' AS literal, 5 % 2 AS remainder, ? AS value", (7,)
        ).fetchone()
    assert "T" in changed_at
    assert tuple(values) == ("100%", 1, 7)

    oauth_store.save_client("pg-client-b", "{}")
    oauth_store.save_client("pg-client-a", "{}")
    assert oauth_store.evict_unused_clients(1) == 2

    gmail_import.mark_processed(["gmail:pg:first", "gmail:pg:first"])
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM gmail_import_processed").fetchone()[0] == 1


def test_tenant_schemas_are_isolated(pg_url: str) -> None:
    first_user = f"pytest-a-{uuid.uuid4()}"
    second_user = f"pytest-b-{uuid.uuid4()}"
    with postgres.user_schema(first_user):
        db.init_db()
        first_id = db.insert_memo("first tenant")
        assert db.fetch_item(first_id)["raw_text"] == "first tenant"
    with postgres.user_schema(second_user):
        db.init_db()
        assert db.fetch_memos() == []


def test_transaction_rolls_back_without_closing_connection(tenant: str) -> None:
    conn = db.get_connection()
    try:
        with pytest.raises(RuntimeError, match="rollback"), conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO app_settings(key, value) VALUES (?, ?)", ("rollback", "yes")
            )
            raise RuntimeError("rollback")
        assert conn.closed is False
        assert conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", ("rollback",)
        ).fetchone() is None
    finally:
        conn.close()


def test_implicit_writes_are_serialized_before_relation_locks(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = f"pytest-write-lock-{uuid.uuid4()}"
    with postgres.user_schema(user_id):
        db.init_db()
        item_id = db.insert_task("Concurrent task")

    complete_has_written = threading.Event()
    delete_attempted_write = threading.Event()
    release_complete = threading.Event()
    original_execute = postgres.Connection.execute

    def gated_execute(
        self: postgres.Connection, query: str, parameters=None
    ) -> postgres.Cursor:
        thread_name = threading.current_thread().name
        if "review-delete" in thread_name and query.strip().upper() == "BEGIN IMMEDIATE":
            delete_attempted_write.set()
        result = original_execute(self, query, parameters)
        if (
            "review-complete" in thread_name
            and "UPDATE items SET status = 'done', completed_at = ?" in query
        ):
            complete_has_written.set()
            if not release_complete.wait(5):
                raise TimeoutError("test did not release the completing transaction")
        return result

    monkeypatch.setattr(postgres.Connection, "execute", gated_execute)

    def complete() -> None:
        with postgres.user_schema(user_id):
            db.complete_task(item_id)

    def delete() -> None:
        with postgres.user_schema(user_id):
            db.delete_item(item_id)

    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-complete") as complete_pool,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-delete") as delete_pool,
    ):
        complete_future = complete_pool.submit(complete)
        try:
            assert complete_has_written.wait(5)
            delete_future = delete_pool.submit(delete)
            assert delete_attempted_write.wait(5)
            time.sleep(0.2)
            assert not delete_future.done()
        finally:
            release_complete.set()
        complete_future.result(timeout=5)
        delete_future.result(timeout=5)

    with postgres.user_schema(user_id):
        assert db.fetch_item(item_id) is None


def test_executemany_acquires_write_lock_after_an_implicit_read(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = f"pytest-executemany-lock-{uuid.uuid4()}"
    with postgres.user_schema(user_id):
        db.init_db()
        holder = db.get_connection()
    holder.execute("BEGIN IMMEDIATE")

    write_lock_attempted = threading.Event()
    original_acquire = postgres.Connection._acquire_write_lock

    def observed_acquire(self: postgres.Connection) -> None:
        if "review-executemany" in threading.current_thread().name:
            write_lock_attempted.set()
        original_acquire(self)

    monkeypatch.setattr(postgres.Connection, "_acquire_write_lock", observed_acquire)

    def insert_many() -> None:
        with postgres.user_schema(user_id), db.connection() as conn, conn:
            conn.execute("SELECT 1")
            conn.executemany(
                "INSERT INTO gmail_import_processed(source_ref, processed_at) VALUES (?, ?)",
                [("gmail:first", "2026-10-21T00:00:00"), ("gmail:second", "2026-10-21T00:00:01")],
            )

    try:
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="review-executemany"
        ) as pool:
            future = pool.submit(insert_many)
            assert write_lock_attempted.wait(5)
            time.sleep(0.2)
            assert not future.done()
            holder.commit()
            future.result(timeout=5)
    finally:
        if not holder.closed:
            holder.rollback()
            holder.close()

    with postgres.user_schema(user_id), db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM gmail_import_processed").fetchone()[0] == 2


def test_recursive_cte_delete_waits_for_tenant_write_lock(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from symnote.core import mindmap

    user_id = f"pytest-cte-lock-{uuid.uuid4()}"
    with postgres.user_schema(user_id):
        db.init_db()
        node_id = mindmap.create_node(1, "root", "Concurrent mind map")
        holder = db.get_connection()
    holder.execute("BEGIN IMMEDIATE")

    write_lock_attempted = threading.Event()
    original_acquire = postgres.Connection._acquire_write_lock

    def observed_acquire(self: postgres.Connection) -> None:
        if "review-cte" in threading.current_thread().name:
            write_lock_attempted.set()
        original_acquire(self)

    monkeypatch.setattr(postgres.Connection, "_acquire_write_lock", observed_acquire)

    def delete_subtree() -> None:
        with postgres.user_schema(user_id):
            mindmap.delete_node_and_descendants(node_id)

    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-cte") as pool:
            future = pool.submit(delete_subtree)
            assert write_lock_attempted.wait(5)
            time.sleep(0.2)
            assert not future.done()
            holder.commit()
            future.result(timeout=5)
    finally:
        if not holder.closed:
            holder.rollback()
            holder.close()

    with postgres.user_schema(user_id), db.connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM mindmap_nodes WHERE id = ?", (node_id,)
        ).fetchone() is None


def test_newer_schema_version_is_rejected(pg_url: str) -> None:
    user_id = f"pytest-newer-{uuid.uuid4()}"
    with postgres.user_schema(user_id):
        db.init_db()
        with db.connection() as conn, conn:
            conn.execute(
                "INSERT INTO _symnote_schema_migrations(version) VALUES (?)",
                (postgres.POSTGRES_SCHEMA_VERSION + 1,),
            )
        with pytest.raises(RuntimeError, match="newer version"):
            db.init_db()


def test_v17_to_v18_bonus_migration_is_additive(pg_url: str) -> None:
    user_id = f"pytest-v17-v18-{uuid.uuid4()}"
    with postgres.user_schema(user_id):
        db.init_db()
        item_id = db.insert_task("Preserved")
        with db.connection() as conn, conn:
            conn.execute("DROP TABLE quest_bonus_log")
            conn.execute("DELETE FROM _symnote_schema_migrations WHERE version >= 18")

        db.init_db()
        db.init_db()

        assert db.fetch_item(item_id) is not None
        with db.connection() as conn:
            version = conn.execute(
                "SELECT MAX(version) FROM _symnote_schema_migrations"
            ).fetchone()[0]
            table = conn.execute(
                "SELECT to_regclass('quest_bonus_log')"
            ).fetchone()[0]
        assert version == postgres.POSTGRES_SCHEMA_VERSION == 19
        assert table == "quest_bonus_log"


def test_system_connection_creates_only_shared_schema(pg_url: str) -> None:
    with postgres.system_connection() as conn, conn:
        row = conn.execute("SELECT current_schema()").fetchone()
        assert row is not None and row[0] == postgres.SYSTEM_SCHEMA
        conn.execute(
            "CREATE TABLE IF NOT EXISTS postgres_storage_probe (key TEXT PRIMARY KEY)"
        )


def test_snapshot_foreign_keys_can_be_deferred_for_import(tenant: str) -> None:
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT condeferrable
            FROM pg_constraint
            WHERE contype = 'f' AND connamespace = ?::regnamespace
            """,
            (tenant,),
        ).fetchall()
    assert rows
    assert all(row["condeferrable"] for row in rows)


def test_integrity_errors_keep_sqlite_domain_contract(tenant: str) -> None:
    with db.connection() as conn, conn:
        conn.execute(
            "INSERT INTO achievements(code, unlocked_at) VALUES (?, ?)",
            ("unique", "2026-10-21T00:00:00"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO achievements(code, unlocked_at) VALUES (?, ?)",
                ("unique", "2026-10-21T00:00:01"),
            )


def test_v18_source_migration_preserves_existing_items(pg_url):
    user_id = f"pytest-v18-v19-{uuid.uuid4()}"
    with postgres.user_schema(user_id):
        db.init_db()
        item_id = db.insert_task("Keep existing data")
        tables = ("source_connections", "source_oauth_states", "source_processed")
        with db.connection() as conn, conn:
            for table in tables:
                conn.execute(f"DROP TABLE {table}")
            conn.execute("DELETE FROM _symnote_schema_migrations WHERE version >= 19")
        db.init_db()
        db.init_db()
        assert db.fetch_item(item_id) is not None
        with db.connection() as conn:
            assert conn.execute("SELECT MAX(version) FROM _symnote_schema_migrations").fetchone()[0] == 19
            for table in tables:
                assert conn.execute("SELECT to_regclass(?)", (table,)).fetchone()[0] == table
