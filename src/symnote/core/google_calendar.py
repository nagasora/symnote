"""ローカルタスクを原本として Google Calendar の認証と冪等な書き出しを扱う。

予定の読み取りは短時間キャッシュを使い、ネットワーク障害時もローカルの
タスク作成・編集を妨げない。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from symnote.config import AppConfig
from symnote.core.db import connection, fetch_tasks_for_morning_digest

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
]
KEYRING_SERVICE = "symnote-google-calendar"
KEYRING_USERNAME = "default"


class CalendarSetupError(RuntimeError):
    """The optional Calendar integration cannot be configured on this device."""


@dataclass(frozen=True)
class SyncResult:
    """同期成功・失敗・再試行待ち件数を返す。"""

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
    """保存済みの承認 scope を維持して認証情報を復元する。"""
    payload = _keyring().get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    if not payload:
        return None
    from google.oauth2.credentials import Credentials

    return Credentials.from_authorized_user_info(json.loads(payload))


def is_connected() -> bool:
    """Whether this Windows/macOS/Linux user has saved Calendar credentials."""
    try:
        return bool(_keyring().get_password(KEYRING_SERVICE, KEYRING_USERNAME))
    except Exception:
        return False


def connect(config: AppConfig | None = None) -> None:
    """Desktop OAuth を実行し、更新トークンを OS の資格情報ストアに保存する。"""
    from symnote.core.runtime import CalendarWriteBlocked, is_test_mode

    if is_test_mode():
        raise CalendarWriteBlocked("テスト環境では本番と共有の Google 接続を変更しません。")
    from symnote.core.calendar_settings import effective_config

    config = effective_config(config)
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
    with connection() as conn, conn:
        conn.execute("DELETE FROM calendar_event_cache")


def disconnect() -> None:
    """Remove locally retained OAuth credentials; calendar events are untouched."""
    from symnote.core.runtime import CalendarWriteBlocked, is_test_mode

    if is_test_mode():
        raise CalendarWriteBlocked("テスト環境では本番と共有の Google 接続を変更しません。")
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
    service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    from symnote.core.runtime import ReadOnlyCalendarService, is_test_mode

    return ReadOnlyCalendarService(service) if is_test_mode() else service


def task_event_payload(
    task: dict[str, Any],
    reminder_minutes: int,
    deadline_hour: int = 9,
    timezone: str = "Asia/Tokyo",
) -> dict[str, Any] | None:
    """タスクの期限を予定にする。時刻があれば 30 分の予定、日付だけなら終日の予定。

    本文にローカルのタスク ID を持たせ、ローカル優先で安全に照合できるようにする。
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
    title = (task.get("tags") or task.get("raw_text") or "SymNote task").strip()
    description = task.get("raw_text", "").strip()
    if due_time is None:
        # 期限時刻の無いタスクを deadline_hour の時間指定予定にすると、空き時間を塞いで見える。
        timing: dict[str, Any] = {
            "start": {"date": due_day.isoformat()},
            "end": {"date": (due_day + dt.timedelta(days=1)).isoformat()},
            "reminders": {"useDefault": False, "overrides": []},
        }
    else:
        start = dt.datetime.combine(due_day, due_time)
        end = start + dt.timedelta(minutes=30)
        timing = {
            "start": {"dateTime": start.isoformat(timespec="seconds"), "timeZone": timezone},
            "end": {"dateTime": end.isoformat(timespec="seconds"), "timeZone": timezone},
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": max(0, reminder_minutes)}],
            },
        }
    return {
        "summary": title,
        "description": description,
        **timing,
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
    previous: dict[str, str] | None = None
    if existing and (existing.get("calendar_id") or calendar_id) != calendar_id:
        # 対象カレンダーが変わった: 旧カレンダーの予定を消し、新しい側へ作り直す。
        # 削除と作成を 1 操作にまとめ、削除に失敗したら記録を旧カレンダーのまま残して再試行させる。
        previous = {"calendar_id": existing["calendar_id"], "event_id": existing["event_id"]}
        if payload is None:
            operations.append({"operation": "delete", "digest_date": digest_date, **previous})
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
                    "previous": previous,
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
    """朝のまとめと任意の個別予定を同期し、失敗した操作は再試行できるようにする。"""
    from symnote.core.calendar_settings import effective_config
    from symnote.core.calendar_task_export import sync_task_events
    from symnote.core.runtime import is_test_mode

    if is_test_mode():
        return SyncResult(message="テスト環境のため、Google カレンダーへは同期しません。")
    config = effective_config(config)
    task_result = sync_task_events(config, service_factory or _calendar_service)
    digest_operations = _morning_digest_operations(
        config, today or today_in_timezone(config.google_calendar_timezone)
    )
    if not digest_operations:
        return task_result if task_result.synced or task_result.failed else SyncResult(message="同期する変更はありません。")
    try:
        service = (service_factory or _calendar_service)()
    except Exception as exc:
        pending = len(digest_operations) + task_result.failed
        return SyncResult(synced=task_result.synced, failed=pending, pending=pending, message=str(exc))

    synced, failed = task_result.synced, task_result.failed
    failure_message = task_result.message
    for operation in digest_operations:
        digest_date = operation["digest_date"]
        calendar_id = operation["calendar_id"]
        try:
            if operation["operation"] == "delete":
                _delete_event(service, calendar_id, operation["event_id"])
                _delete_morning_digest_mapping(digest_date)
            else:
                previous = operation.get("previous")
                if previous:
                    _delete_event(service, previous["calendar_id"], previous["event_id"])
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


from symnote.core.calendar_events import fetch_events, propose_prep_tasks_from_events  # noqa: E402,F401
