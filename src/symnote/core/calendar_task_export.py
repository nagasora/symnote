"""任意設定のタスク個別予定を既存 outbox と対応表で冪等に同期する。"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Tuple

from symnote.config import AppConfig
from symnote.core.calendar_settings import get_setting
from symnote.core.db import connection

if TYPE_CHECKING:
    from symnote.core.google_calendar import SyncResult


DEFAULT_EXPORT_TASKS = "1"


def sync_task_events(config: AppConfig, service_factory: Callable[[], Any]) -> SyncResult:
    """期限日のあるタスクを予定と照合し、完了・削除・設定 OFF の予定を削除する。"""
    from symnote.core.google_calendar import (
        SyncResult,
        _delete_event,
        _http_status,
        _payload_hash,
        task_event_payload,
    )

    enabled = get_setting("export_tasks", DEFAULT_EXPORT_TASKS) == "1"
    with connection() as conn:
        tasks = {
            row["id"]: dict(row) for row in conn.execute("SELECT * FROM items WHERE kind = 'task'")
        }
        mappings = {
            row["task_id"]: dict(row) for row in conn.execute("SELECT * FROM calendar_task_sync")
        }
        queued = {
            row["task_id"]: dict(row) for row in conn.execute("SELECT * FROM calendar_sync_outbox")
        }
    operations: Dict[int, Tuple[Dict[str, Any] | None, str | None, Dict[str, Any] | None]] = {}
    for task_id in tasks.keys() | mappings.keys() | queued.keys():
        task = tasks.get(task_id)
        payload = None
        if enabled and task and task.get("due_date"):
            try:
                payload = task_event_payload(
                    task,
                    config.google_calendar_reminder_minutes,
                    config.google_calendar_deadline_hour,
                    config.google_calendar_timezone,
                )
            except (ValueError, TypeError):
                pass
        previous = mappings.get(task_id)
        content_hash = _payload_hash(payload) if payload else None
        if (
            payload
            and (
                not previous
                or previous.get("content_hash") != content_hash
                or previous.get("calendar_id") != config.google_calendar_id
            )
        ) or (not payload and previous):
            operations[task_id] = (payload, content_hash, previous)
        elif task_id in queued:
            _acknowledge(task_id, queued[task_id])
    if not operations:
        return SyncResult()
    try:
        service = service_factory()
    except Exception as exc:
        return SyncResult(failed=len(operations), pending=len(operations), message=str(exc))
    synced = failed = 0
    message = ""
    for task_id, (payload, content_hash, previous) in operations.items():
        try:
            old_calendar = (
                (previous.get("calendar_id") or config.google_calendar_id) if previous else None
            )
            if (
                previous
                and previous["event_id"]
                and (not payload or old_calendar != config.google_calendar_id)
            ):
                _delete_event(service, old_calendar, previous["event_id"])
            if payload:
                identity = f"{Path(config.db_path).expanduser().resolve()}:{task_id}"
                event_id = "sntask" + hashlib.sha256(identity.encode()).hexdigest()[:40]
                if previous and old_calendar == config.google_calendar_id:
                    event_id = previous["event_id"] or event_id
                if (
                    not previous
                    or previous["event_id"] != event_id
                    or old_calendar != config.google_calendar_id
                ):
                    _remember_pending_event(task_id, event_id, config.google_calendar_id)
                body = {**payload, "id": event_id, "status": "confirmed"}
                needs_insert = True
                if (
                    previous
                    and previous["event_id"]
                    and previous.get("last_synced_at") is not None
                    and old_calendar == config.google_calendar_id
                ):
                    try:
                        service.events().update(
                            calendarId=config.google_calendar_id, eventId=event_id, body=body
                        ).execute()
                        needs_insert = False
                    except Exception as exc:
                        if _http_status(exc) not in (404, 410):
                            raise
                if needs_insert:
                    try:
                        service.events().insert(
                            calendarId=config.google_calendar_id, body=body
                        ).execute()
                    except Exception as exc:
                        if _http_status(exc) != 409:
                            raise
                        service.events().update(
                            calendarId=config.google_calendar_id, eventId=event_id, body=body
                        ).execute()
                with connection() as conn, conn:
                    conn.execute(
                        "INSERT INTO calendar_task_sync(task_id, event_id, last_synced_at, calendar_id, content_hash) "
                        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET "
                        "event_id=excluded.event_id, last_synced_at=excluded.last_synced_at, "
                        "calendar_id=excluded.calendar_id, content_hash=excluded.content_hash",
                        (
                            task_id,
                            event_id,
                            dt.datetime.now().isoformat(),
                            config.google_calendar_id,
                            content_hash,
                        ),
                    )
            else:
                with connection() as conn, conn:
                    conn.execute("DELETE FROM calendar_task_sync WHERE task_id = ?", (task_id,))
            if task_id in queued:
                _acknowledge(task_id, queued[task_id])
            synced += 1
        except Exception as exc:
            failed += 1
            message = message or str(exc)
    return SyncResult(synced=synced, failed=failed, pending=failed, message=message)


def _remember_pending_event(task_id: int, event_id: str, calendar_id: str) -> None:
    """送信前に予定の所在を永続化し、応答消失後の完了・削除でも削除対象を残す。

    last_synced_at と content_hash が NULL の行は、作成結果が未確定の予定を表す。
    対応表の保存後にだけ送信し、通信失敗時はこの行を残して照合・削除を可能にする。
    """
    with connection() as conn, conn:
        conn.execute(
            "INSERT INTO calendar_task_sync "
            "(task_id, event_id, calendar_id, last_synced_at, content_hash) "
            "VALUES (?, ?, ?, NULL, NULL) ON CONFLICT(task_id) DO UPDATE SET "
            "event_id = excluded.event_id, calendar_id = excluded.calendar_id, "
            "last_synced_at = NULL, content_hash = NULL",
            (task_id, event_id, calendar_id),
        )


def _acknowledge(task_id: int, queued: Dict[str, Any]) -> None:
    """読み取った送信待ちだけを削除し、同期中の更新は次回に残す。"""
    with connection() as conn, conn:
        conn.execute(
            "DELETE FROM calendar_sync_outbox WHERE task_id = ? AND operation = ? AND changed_at = ?",
            (task_id, queued["operation"], queued["changed_at"]),
        )
