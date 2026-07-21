"""One-way, retryable Google Calendar delivery for local SymNote tasks.

The SQLite database remains authoritative.  This module only sends tasks that
were queued in the local outbox, so losing network access never prevents task
creation or editing.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from symnote.config import AppConfig, load_config
from symnote.core.db import connection

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
KEYRING_SERVICE = "symnote-google-calendar"
KEYRING_USERNAME = "default"


class CalendarSetupError(RuntimeError):
    """The optional Calendar integration cannot be configured on this device."""


@dataclass(frozen=True)
class SyncResult:
    synced: int = 0
    failed: int = 0
    pending: int = 0
    message: str = ""


def _keyring() -> Any:
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - package dependency guard
        raise CalendarSetupError("keyring が利用できません。依存関係をインストールしてください。") from exc
    return keyring


def _load_credentials() -> Any | None:
    payload = _keyring().get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    if not payload:
        return None
    from google.oauth2.credentials import Credentials

    return Credentials.from_authorized_user_info(json.loads(payload), SCOPES)


def is_connected() -> bool:
    """Whether this Windows/macOS/Linux user has saved Calendar credentials."""
    try:
        return bool(_keyring().get_password(KEYRING_SERVICE, KEYRING_USERNAME))
    except Exception:
        return False


def connect(config: AppConfig | None = None) -> None:
    """Run Desktop OAuth and retain the refresh token in the OS credential vault."""
    config = config or load_config()
    client_file = Path(config.google_calendar_client_secret_path).expanduser()
    if not client_file.is_file():
        raise CalendarSetupError(
            "Google Cloud で作成した Desktop OAuth クライアント JSON のパスを "
            "GOOGLE_CALENDAR_CLIENT_SECRET_PATH に設定してください。"
        )
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover - package dependency guard
        raise CalendarSetupError("Google Calendar の依存関係をインストールしてください。") from exc

    credentials = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES).run_local_server(
        port=0, open_browser=True
    )
    _keyring().set_password(KEYRING_SERVICE, KEYRING_USERNAME, credentials.to_json())


def disconnect() -> None:
    """Remove locally retained OAuth credentials; calendar events are untouched."""
    _keyring().delete_password(KEYRING_SERVICE, KEYRING_USERNAME)


def _calendar_service() -> Any:
    credentials = _load_credentials()
    if credentials is None:
        raise CalendarSetupError("Google Calendar が未接続です。")
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        _keyring().set_password(KEYRING_SERVICE, KEYRING_USERNAME, credentials.to_json())
    if not credentials.valid:
        raise CalendarSetupError("Google Calendar の認証が期限切れです。再接続してください。")
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def task_event_payload(
    task: dict[str, Any],
    reminder_minutes: int,
    deadline_hour: int = 9,
    timezone: str = "Asia/Tokyo",
) -> dict[str, Any] | None:
    """Represent a task deadline as a timed Calendar reminder.

    Calendar applies the popup reminder to this all-day event.  The event body
    deliberately contains the local task ID for safe, local-wins reconciliation.
    """
    due = task.get("due_date")
    if not due or task.get("status") == "done":
        return None
    try:
        due_day = dt.date.fromisoformat(due)
    except (TypeError, ValueError):
        return None
    try:
        due_time = dt.time.fromisoformat(task["due_time"]) if task.get("due_time") else None
    except (TypeError, ValueError):
        due_time = None
    start = dt.datetime.combine(
        due_day,
        due_time or dt.time(hour=max(0, min(23, deadline_hour))),
    )
    end = start + dt.timedelta(minutes=30)
    title = (task.get("tags") or task.get("raw_text") or "SymNote task").strip()
    description = task.get("raw_text", "").strip()
    return {
        "summary": title,
        "description": description,
        "start": {
            "dateTime": start.isoformat(timespec="seconds"),
            "timeZone": timezone,
        },
        "end": {
            "dateTime": end.isoformat(timespec="seconds"),
            "timeZone": timezone,
        },
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": max(0, reminder_minutes)}],
        },
        "extendedProperties": {"private": {"symnote_task_id": str(task["id"])}},
    }


def _pending_rows() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT o.task_id, o.operation, s.event_id, i.id, i.raw_text, i.tags,
                   i.due_date, i.due_time, i.status
            FROM calendar_sync_outbox AS o
            LEFT JOIN calendar_task_sync AS s ON s.task_id = o.task_id
            LEFT JOIN items AS i ON i.id = o.task_id
            ORDER BY o.changed_at, o.task_id
            """
        ).fetchall()
        return [dict(row) for row in rows]


def _mark_complete(task_id: int, event_id: str | None, operation: str) -> None:
    with connection() as conn, conn:
        if operation == "delete":
            conn.execute("DELETE FROM calendar_task_sync WHERE task_id = ?", (task_id,))
        elif event_id:
            conn.execute(
                """
                INSERT INTO calendar_task_sync (task_id, event_id, last_synced_at)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                  event_id = excluded.event_id, last_synced_at = excluded.last_synced_at
                """,
                (task_id, event_id, dt.datetime.now().isoformat(timespec="seconds")),
            )
        conn.execute("DELETE FROM calendar_sync_outbox WHERE task_id = ?", (task_id,))


def _is_not_found(error: Exception) -> bool:
    """Return whether a Google API error means its remote event is gone."""
    return getattr(getattr(error, "resp", None), "status", None) == 404


def sync_pending_tasks(
    config: AppConfig | None = None,
    service_factory: Callable[[], Any] | None = None,
) -> SyncResult:
    """Deliver all queued changes. Failures stay queued and are retried later."""
    config = config or load_config()
    rows = _pending_rows()
    if not rows:
        return SyncResult(message="同期する変更はありません。")
    try:
        service = (service_factory or _calendar_service)()
    except Exception as exc:
        return SyncResult(failed=len(rows), pending=len(rows), message=str(exc))

    synced = failed = 0
    for row in rows:
        task_id, event_id, operation = row["task_id"], row["event_id"], row["operation"]
        try:
            payload = task_event_payload(
                row,
                config.google_calendar_reminder_minutes,
                config.google_calendar_deadline_hour,
                config.google_calendar_timezone,
            )
            should_delete = operation == "delete" or payload is None
            if should_delete:
                if event_id:
                    try:
                        service.events().delete(calendarId=config.google_calendar_id, eventId=event_id).execute()
                    except Exception as exc:
                        # Deleting an event already removed in Calendar is success.
                        if getattr(getattr(exc, "resp", None), "status", None) != 404:
                            raise
                _mark_complete(task_id, None, "delete")
            elif event_id:
                try:
                    service.events().update(
                        calendarId=config.google_calendar_id, eventId=event_id, body=payload
                    ).execute()
                except Exception as exc:
                    # A Calendar event may have been removed outside SymNote.
                    # Recreate it and replace the stale local mapping instead
                    # of leaving this task permanently queued for retry.
                    if not _is_not_found(exc):
                        raise
                    event = service.events().insert(
                        calendarId=config.google_calendar_id, body=payload
                    ).execute()
                    event_id = event["id"]
                _mark_complete(task_id, event_id, "upsert")
            else:
                event = service.events().insert(calendarId=config.google_calendar_id, body=payload).execute()
                _mark_complete(task_id, event["id"], "upsert")
            synced += 1
        except Exception:
            failed += 1
    return SyncResult(synced=synced, failed=failed, pending=failed)
