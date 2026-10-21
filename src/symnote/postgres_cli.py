"""Inspect, import, and export user data without replacing existing databases."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from symnote.core import db, postgres

_LOCAL_ONLY_TABLES = {"oauth_clients", "oauth_tokens", "llm_usage"}


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _source_connection(source: Path) -> sqlite3.Connection:
    source = source.expanduser().resolve(strict=True)
    conn = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    conn.execute("BEGIN")
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        conn.close()
        raise ValueError("Source SQLite integrity check failed.")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != db.SCHEMA_VERSION:
        conn.close()
        raise ValueError(
            f"Source must be SQLite schema version {db.SCHEMA_VERSION}; upgrade a backup first."
        )
    return conn


def _sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _pg_tables(conn: Any, schema: str) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = ? "
            "AND table_type = 'BASE TABLE' "
            "AND table_name NOT LIKE '_symnote_%' ORDER BY table_name",
            (schema,),
        ).fetchall()
    ]


def inspect_sqlite(source: Path) -> dict[str, Any]:
    with _source_connection(source) as conn:
        counts = {
            name: conn.execute(f"SELECT count(*) FROM {_identifier(name)}").fetchone()[0]
            for name in _sqlite_tables(conn)
            if name not in _LOCAL_ONLY_TABLES
        }
    conn.close()
    return {"schema_version": db.SCHEMA_VERSION, "tables": counts, "source_modified": False}


def import_sqlite(source: Path, user_id: str) -> dict[str, Any]:
    """Copy one consistent SQLite snapshot into an empty PostgreSQL tenant."""
    if not postgres.enabled():
        raise ValueError("Select SYMNOTE_STORAGE_BACKEND=postgres before importing.")
    src = _source_connection(source)
    try:
        with postgres.user_schema(user_id):
            db.init_db()
            with db.connection() as target, target:
                target.execute("BEGIN IMMEDIATE")
                target.execute("SET CONSTRAINTS ALL DEFERRED")
                tables = _pg_tables(target, postgres.current_schema())
                source_tables = set(_sqlite_tables(src)) - _LOCAL_ONLY_TABLES
                unknown = source_tables - set(tables)
                if unknown:
                    raise ValueError(
                        "Source contains unsupported data tables: " + ", ".join(sorted(unknown))
                    )
                for table in tables:
                    if target.execute(f"SELECT count(*) FROM {_identifier(table)}").fetchone()[0]:
                        raise ValueError(
                            "Target user storage must be empty; existing data was preserved."
                        )
                counts: dict[str, int] = {}
                for table in sorted(source_tables):
                    cursor = src.execute(f"SELECT * FROM {_identifier(table)}")
                    columns = [column[0] for column in cursor.description]
                    names = ", ".join(_identifier(column) for column in columns)
                    placeholders = ", ".join("?" for _ in columns)
                    count = 0
                    while rows := cursor.fetchmany(500):
                        target.executemany(
                            f"INSERT INTO {_identifier(table)} ({names}) VALUES ({placeholders})",
                            rows,
                        )
                        count += len(rows)
                    counts[table] = count
                for table in tables:
                    sequence = (
                        target.execute(
                            "SELECT pg_get_serial_sequence(?, 'id')",
                            (postgres.current_schema() + "." + table,),
                        ).fetchone()[0]
                        if _has_id(target, postgres.current_schema(), table)
                        else None
                    )
                    if sequence:
                        maximum = target.execute(
                            f"SELECT MAX(id) FROM {_identifier(table)}"
                        ).fetchone()[0]
                        target.execute(
                            "SELECT setval(?::regclass, ?, ?)",
                            (sequence, maximum or 1, maximum is not None),
                        )
                target.execute("SET CONSTRAINTS ALL IMMEDIATE")
        return {
            "user_id": user_id,
            "tables": counts,
            "source_modified": False,
            "excluded_local_credentials_and_usage": sorted(_LOCAL_ONLY_TABLES),
        }
    finally:
        src.close()


def _has_id(conn: Any, schema: str, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = ? "
            "AND table_name = ? AND column_name = 'id'",
            (schema, table),
        ).fetchone()
        is not None
    )


def export_sqlite(output: Path, user_id: str) -> dict[str, Any]:
    """Export to a new local SQLite file; never replace an existing file."""
    if not postgres.enabled():
        raise ValueError("Select SYMNOTE_STORAGE_BACKEND=postgres before exporting.")
    output = output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("Export target already exists; choose a new path.")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".symnote-export-", suffix=".db", dir=output.parent)
    os.close(fd)
    partial = Path(temporary)
    try:
        env = os.environ.copy()
        env.update(SYMNOTE_STORAGE_BACKEND="sqlite", DB_PATH=str(partial))
        subprocess.run(
            [sys.executable, "-c", "from symnote.core.db import init_db; init_db()"],
            env=env,
            check=True,
            capture_output=True,
        )
        local = sqlite3.connect(partial)
        local.execute(
            "CREATE TABLE IF NOT EXISTS gmail_import_processed "
            "(source_ref TEXT PRIMARY KEY, processed_at TEXT NOT NULL)"
        )
        local.commit()
        try:
            with postgres.user_schema(user_id), db.connection() as source, local:
                source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                allowed = set(_sqlite_tables(local))
                counts = {}
                tables = _pg_tables(source, postgres.current_schema())
                if "items" not in tables:
                    raise ValueError("No initialized storage exists for this user.")
                for table in tables:
                    if table not in allowed or table in _LOCAL_ONLY_TABLES:
                        continue
                    cursor = source.execute(f"SELECT * FROM {_identifier(table)}")
                    columns = [column[0] for column in cursor.description]
                    names = ", ".join(_identifier(column) for column in columns)
                    placeholders = ", ".join("?" for _ in columns)
                    count = 0
                    while rows := cursor.fetchmany(500):
                        local.executemany(
                            f"INSERT INTO {_identifier(table)} ({names}) VALUES ({placeholders})",
                            [tuple(row) for row in rows],
                        )
                        count += len(rows)
                    counts[table] = count
                if local.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("Export reference check failed.")
                if local.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Export integrity check failed.")
        finally:
            local.close()
        os.link(partial, output)
        return {"user_id": user_id, "tables": counts, "output": str(output)}
    finally:
        partial.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(partial) + suffix).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("source", type=Path)
    load = commands.add_parser("import")
    load.add_argument("source", type=Path)
    load.add_argument("--user-id", required=True)
    load.add_argument("--apply", action="store_true")
    export = commands.add_parser("export")
    export.add_argument("output", type=Path)
    export.add_argument("--user-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "inspect" or (args.command == "import" and not args.apply):
        result = inspect_sqlite(args.source)
        if args.command == "import":
            result["dry_run"] = True
    elif args.command == "import":
        result = import_sqlite(args.source, args.user_id)
    else:
        result = export_sqlite(args.output, args.user_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
