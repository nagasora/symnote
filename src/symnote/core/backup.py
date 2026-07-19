"""Local, consistent SQLite backup helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3
import os
import tempfile


def create_backup(database_path: str | Path, backup_directory: str | Path) -> Path:
    """Back up a SQLite database without replacing any existing backup.

    SQLite's backup API includes committed WAL data, so it is safe while SymNote
    is running. Restoration is deliberately left to an explicit user action.
    """
    source_path = Path(database_path).expanduser()
    if not source_path.is_file():
        raise FileNotFoundError(f"SymNote database does not exist: {source_path}")
    destination_dir = Path(backup_directory).expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = destination_dir / f"symnote-{timestamp}.db"
    fd, temporary_name = tempfile.mkstemp(prefix=".symnote-", suffix=".db", dir=destination_dir)
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        source = sqlite3.connect(source_path)
        target = sqlite3.connect(temporary_path)
        try:
            source.backup(target)
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise sqlite3.DatabaseError(f"Backup integrity check failed: {integrity}")
        finally:
            target.close()
            source.close()
        temporary_path.replace(destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination
