"""One-way, retryable Google Calendar delivery for local SymNote tasks.

The SQLite database remains authoritative.  This module only sends tasks that
were queued in the local outbox, so losing network access never prevents task
creation or editing.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from symnote.config import AppConfig, load_config
from symnote.core.db import connection, fetch_tasks_for_morning_digest

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


def _task_title(task: dict[str, Any]) -> str:
    """Return the concise title users see in a Calendar digest."""
    value = (task.get("tags") or task.get("raw_text") or "").strip()
    if value:
        return value.splitlines()[0]
    return f"Task #{task.get('id', '?')}"


def morning_digest_event_payload(
    digest_date: str,
    tasks: list[dict[str, Any]],
    hour: int = 8,
    timezone: str = "Asia/Tokyo",
) -> dict[str, Any] | None:
    """Build one 08:00 Calendar event for due and overdue local tasks."""
    if not tasks:
        return None
    try:
        day = dt.date.fromisoformat(digest_date)
    except (TypeError, ValueError):
        return None

    due_today = [task for task in tasks if task.get("due_date") == digest_date]
    overdue = [task for task in tasks if task.get("due_date") < digest_date]
    lines = ["SymNote 朝の課題通知", ""]
    if due_today:
        lines.append(f"今日が期限（{len(due_today)}件）")
        lines.extend(f"- {_task_title(task)}" for task in due_today)
    if overdue:
        if due_today:
            lines.append("")
        lines.append(f"期限切れ（{len(overdue)}件）")
        lines.extend(f"- {_task_title(task)}" for task in overdue)
    start = dt.datetime.combine(day, dt.time(hour=max(0, min(23, hour))))
    return {
        "summary": f"SymNote: 今日の課題 {len(due_today)}件（期限切れ {len(overdue)}件）",
        "description": "\n".join(lines),
        "start": {"dateTime": start.isoformat(timespec="seconds"), "timeZone": timezone},
        "end": {
            "dateTime": (start + dt.timedelta(minutes=15)).isoformat(timespec="seconds"),
            "timeZone": timezone,
        },
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 0}]},
        "extendedProperties": {"private": {"symnote_morning_digest_date": digest_date}},
    }


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def morning_digest_event_id(digest_date: str) -> str:
    """朝のまとめ予定に使う、日付から決まる Google Calendar のイベント ID を返す。

    ID は base32hex（0-9, a-v）のみ使える。挿入の応答を受け取れずに再試行しても、
    同じ ID なら重複予定ではなく 409（既存）になるため、更新に切り替えられる。
    """
    return "sndigest" + digest_date.replace("-", "")


def today_in_timezone(timezone: str) -> dt.date:
    """設定タイムゾーンでの今日の日付を返す。不正なタイムゾーンなら端末の日付を使う。"""
    try:
        return dt.datetime.now(ZoneInfo(timezone)).date()
    except (ZoneInfoNotFoundError, ValueError):
        return dt.date.today()


def _morning_digest_mappings(today: dt.date) -> dict[str, dict[str, Any]]:
    """Remove expired local delivery state and return active event mappings."""
    with connection() as conn, conn:
        conn.execute(
            "DELETE FROM calendar_morning_digest_sync WHERE digest_date < ?", (today.isoformat(),)
        )
        rows = conn.execute(
            "SELECT digest_date, event_id, content_hash, calendar_id "
            "FROM calendar_morning_digest_sync"
        ).fetchall()
        return {row["digest_date"]: dict(row) for row in rows}


def _morning_digest_operations(config: AppConfig, today: dt.date) -> list[dict[str, Any]]:
    """今日の朝のまとめを最新にするための insert/update/delete 操作を組み立てる。"""
    mappings = _morning_digest_mappings(today)
    operations: list[dict[str, Any]] = []
    digest_date = today.isoformat()
    calendar_id = config.google_calendar_id
    payload = morning_digest_event_payload(
        digest_date,
        fetch_tasks_for_morning_digest(digest_date),
        config.google_calendar_morning_digest_hour,
        config.google_calendar_timezone,
    )
    existing = mappings.get(digest_date)
    if existing and (existing.get("calendar_id") or calendar_id) != calendar_id:
        # 対象カレンダーが変わった: 旧カレンダーの予定を消し、新しい側へ作り直す。
        operations.append(
            {
                "operation": "delete",
                "digest_date": digest_date,
                "event_id": existing["event_id"],
                "calendar_id": existing["calendar_id"],
            }
        )
        existing = None
    if payload is None:
        if existing:
            operations.append(
                {
                    "operation": "delete",
                    "digest_date": digest_date,
                    "event_id": existing["event_id"],
                    "calendar_id": calendar_id,
                }
            )
    else:
        content_hash = _payload_hash(payload)
        if existing is None:
            operations.append(
                {
                    "operation": "insert",
                    "digest_date": digest_date,
                    "payload": payload,
                    "content_hash": content_hash,
                    "calendar_id": calendar_id,
                }
            )
        elif existing["content_hash"] != content_hash or existing.get("calendar_id") is None:
            operations.append(
                {
                    "operation": "update",
                    "digest_date": digest_date,
                    "event_id": existing["event_id"],
                    "payload": payload,
                    "content_hash": content_hash,
                    "calendar_id": calendar_id,
                }
            )

    # Clean up future events created by the former rolling-lookahead sync.
    for mapped_date, mapping in mappings.items():
        if mapped_date > digest_date:
            operations.append(
                {
                    "operation": "delete",
                    "digest_date": mapped_date,
                    "event_id": mapping["event_id"],
                    "calendar_id": mapping.get("calendar_id") or calendar_id,
                }
            )
    return operations


def _save_morning_digest_mapping(
    digest_date: str, event_id: str, content_hash: str, calendar_id: str
) -> None:
    with connection() as conn, conn:
        conn.execute(
            """
            INSERT INTO calendar_morning_digest_sync (
              digest_date, event_id, content_hash, last_synced_at, calendar_id
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(digest_date) DO UPDATE SET
              event_id = excluded.event_id,
              content_hash = excluded.content_hash,
              last_synced_at = excluded.last_synced_at,
              calendar_id = excluded.calendar_id
            """,
            (
                digest_date,
                event_id,
                content_hash,
                dt.datetime.now().isoformat(timespec="seconds"),
                calendar_id,
            ),
        )


def _delete_morning_digest_mapping(digest_date: str) -> None:
    with connection() as conn, conn:
        conn.execute(
            "DELETE FROM calendar_morning_digest_sync WHERE digest_date = ?", (digest_date,)
        )


def _discard_task_outbox() -> None:
    """タスク単位の送信待ちを手元で破棄する。

    タスクごとの予定は作らない仕様になったため、outbox は送る先が無い。
    _queue_calendar_sync の書き込み自体を止めないのは、多数の更新経路に手を入れずに
    済ませるため（行はここで毎回片付くので肥大化しない）。
    """
    with connection() as conn, conn:
        conn.execute("DELETE FROM calendar_sync_outbox")


def _legacy_task_event_rows() -> list[dict[str, Any]]:
    """旧仕様で作られたタスク単位の予定（削除対象）を返す。"""
    with connection() as conn:
        rows = conn.execute(
            "SELECT task_id, event_id FROM calendar_task_sync ORDER BY task_id"
        ).fetchall()
        return [dict(row) for row in rows]


def _forget_legacy_task_event(task_id: int) -> None:
    with connection() as conn, conn:
        conn.execute("DELETE FROM calendar_task_sync WHERE task_id = ?", (task_id,))


def _http_status(error: Exception) -> int | None:
    """Google API エラーの HTTP ステータスを返す。取れなければ None。"""
    return getattr(getattr(error, "resp", None), "status", None)


def _is_not_found(error: Exception) -> bool:
    """Return whether a Google API error means its remote event is gone."""
    return _http_status(error) in (404, 410)


def _delete_event(service: Any, calendar_id: str, event_id: str) -> None:
    """予定を削除する。既に無い場合は成功とみなす。"""
    try:
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    except Exception as exc:
        if not _is_not_found(exc):
            raise


def _upsert_digest_event(
    service: Any, calendar_id: str, digest_date: str, payload: dict[str, Any]
) -> str:
    """固定 ID で朝のまとめ予定を作成し、既にあれば上書きする。"""
    event_id = morning_digest_event_id(digest_date)
    body = {**payload, "id": event_id, "status": "confirmed"}
    try:
        service.events().insert(calendarId=calendar_id, body=body).execute()
    except Exception as exc:
        # 409 は「同じ ID が既にある」（前回の応答を受け取れなかった、または削除済みの予定）。
        if _http_status(exc) != 409:
            raise
        service.events().update(calendarId=calendar_id, eventId=event_id, body=body).execute()
    return event_id


def sync_pending_tasks(
    config: AppConfig | None = None,
    service_factory: Callable[[], Any] | None = None,
    *,
    today: dt.date | None = None,
) -> SyncResult:
    """Deliver the same-day digest and remove legacy per-task Calendar events."""
    config = config or load_config()
    _discard_task_outbox()
    legacy_rows = _legacy_task_event_rows()
    digest_operations = _morning_digest_operations(
        config, today or today_in_timezone(config.google_calendar_timezone)
    )
    if not legacy_rows and not digest_operations:
        return SyncResult(message="同期する変更はありません。")
    try:
        service = (service_factory or _calendar_service)()
    except Exception as exc:
        pending = len(legacy_rows) + len(digest_operations)
        return SyncResult(failed=pending, pending=pending, message=str(exc))

    synced = failed = 0
    failure_message = ""
    for row in legacy_rows:
        try:
            if row["event_id"]:
                # 旧仕様の予定がどのカレンダーにあるかは記録が無いため、現在の設定先を対象にする。
                _delete_event(service, config.google_calendar_id, row["event_id"])
            _forget_legacy_task_event(row["task_id"])
            synced += 1
        except Exception as exc:
            failed += 1
            if not failure_message:
                failure_message = str(exc)
    for operation in digest_operations:
        digest_date = operation["digest_date"]
        calendar_id = operation["calendar_id"]
        try:
            if operation["operation"] == "delete":
                _delete_event(service, calendar_id, operation["event_id"])
                _delete_morning_digest_mapping(digest_date)
            else:
                payload = operation["payload"]
                event_id = operation.get("event_id")
                if event_id and event_id != morning_digest_event_id(digest_date):
                    # 固定 ID 導入前に作られた予定は更新を試み、無ければ固定 ID で作り直す。
                    try:
                        service.events().update(
                            calendarId=calendar_id, eventId=event_id, body=payload
                        ).execute()
                    except Exception as exc:
                        if not _is_not_found(exc):
                            raise
                        event_id = _upsert_digest_event(service, calendar_id, digest_date, payload)
                else:
                    event_id = _upsert_digest_event(service, calendar_id, digest_date, payload)
                _save_morning_digest_mapping(
                    digest_date, event_id, operation["content_hash"], calendar_id
                )
            synced += 1
        except Exception as exc:
            failed += 1
            if not failure_message:
                failure_message = str(exc)
    return SyncResult(
        synced=synced,
        failed=failed,
        pending=failed,
        message=failure_message,
    )
