"""Google 予定の期間取得、オフラインキャッシュ、準備候補を扱う。"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Tuple
from zoneinfo import ZoneInfo

from symnote.config import AppConfig
from symnote.core.calendar_settings import calendar_error_message, effective_config
from symnote.core.candidates import (
    CandidateInput,
    ProposalResult,
    propose_candidates,
    validate_candidate,
)
from symnote.core.db import connection


@dataclass(frozen=True)
class EventFetchResult:
    """予定と最終取得時刻、キャッシュ・オフライン状態を表す。"""

    events: List[Dict[str, Any]]
    fetched_at: str | None = None
    cached: bool = False
    offline: bool = False
    message: str = ""


SYMNOTE_EVENT_PREFIX = "SymNote:"


def is_symnote_generated(event: Dict[str, Any]) -> bool:
    """SymNote 自身が書き出した予定（朝のまとめ・タスクの期限など）なら True。予定の集計から除く。"""
    private = (event.get("extendedProperties") or {}).get("private") or {}
    if private.get("symnote_task_id"):
        return True
    return str(event.get("summary") or "").startswith(SYMNOTE_EVENT_PREFIX)


def event_bounds(event: Dict[str, Any], timezone: str) -> Tuple[dt.datetime, dt.datetime]:
    """終日予定の排他的終了日も含め、予定の開始・終了を設定時刻帯で返す。"""
    zone = ZoneInfo(timezone)
    values: List[dt.datetime] = []
    for key in ("start", "end"):
        field = event[key]
        value = dt.datetime.fromisoformat(field.get("dateTime") or field["date"])
        values.append(
            value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)
        )
    return values[0], values[1]


def _boundary(value: dt.date | dt.datetime, timezone: str) -> str:
    """API の期間境界をタイムゾーン付き ISO 日時へ変換する。"""
    moment = value if isinstance(value, dt.datetime) else dt.datetime.combine(value, dt.time.min)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(timezone))
    return moment.isoformat()


def fetch_events(
    start: dt.date | dt.datetime,
    end: dt.date | dt.datetime,
    *,
    config: AppConfig | None = None,
    service_factory: Callable[[], Any] | None = None,
    force_refresh: bool = False,
    now: dt.datetime | None = None,
    cache_seconds: int = 300,
) -> EventFetchResult:
    """終了境界を含まない期間の全ページを取得し、失敗時は同期間の最終成功結果を返す。"""
    from symnote.core.google_calendar import _calendar_service

    config = effective_config(config)
    begin, finish = (
        _boundary(start, config.google_calendar_timezone),
        _boundary(end, config.google_calendar_timezone),
    )
    if dt.datetime.fromisoformat(begin) >= dt.datetime.fromisoformat(finish):
        raise ValueError("終了日時は開始日時より後にしてください。")
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    with connection() as conn:
        row = conn.execute(
            "SELECT payload, fetched_at FROM calendar_event_cache "
            "WHERE calendar_id = ? AND range_start = ? AND range_end = ?",
            (config.google_calendar_id, begin, finish),
        ).fetchone()
    if (
        row
        and not force_refresh
        and 0
        <= (now - dt.datetime.fromisoformat(row["fetched_at"])).total_seconds()
        < cache_seconds
    ):
        return EventFetchResult(json.loads(row["payload"]), row["fetched_at"], cached=True)
    try:
        service = (service_factory or _calendar_service)()
        events: List[Dict[str, Any]] = []
        token: str | None = None
        while True:
            kwargs: Dict[str, Any] = dict(
                calendarId=config.google_calendar_id,
                timeMin=begin,
                timeMax=finish,
                singleEvents=True,
                orderBy="startTime",
                maxResults=2500,
            )
            if token:
                kwargs["pageToken"] = token
            response = service.events().list(**kwargs).execute()
            events.extend(
                event for event in response.get("items", []) if event.get("status") != "cancelled"
            )
            token = response.get("nextPageToken")
            if not token:
                break
    except Exception as exc:
        return EventFetchResult(
            json.loads(row["payload"]) if row else [],
            row["fetched_at"] if row else None,
            cached=bool(row),
            offline=True,
            message=calendar_error_message(exc),
        )
    timestamp = now.isoformat(timespec="seconds")
    with connection() as conn, conn:
        conn.execute(
            "INSERT INTO calendar_event_cache VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(calendar_id, range_start, range_end) DO UPDATE SET "
            "fetched_at = excluded.fetched_at, payload = excluded.payload",
            (
                config.google_calendar_id,
                begin,
                finish,
                timestamp,
                json.dumps(events, ensure_ascii=False),
            ),
        )
        conn.execute(
            "DELETE FROM calendar_event_cache WHERE fetched_at < ?",
            ((now - dt.timedelta(days=90)).isoformat(),),
        )
    return EventFetchResult(events, timestamp)


class CalendarListPermissionError(RuntimeError):
    """予定の読み書きは可能だが、カレンダー一覧の追加権限が不足していることを表す。"""


def list_calendars(service_factory: Callable[[], Any] | None = None) -> List[Dict[str, Any]]:
    """書き出し可能なカレンダーを全ページ取得する。"""
    from symnote.core.google_calendar import _calendar_service, _http_status

    service = (service_factory or _calendar_service)()
    result: List[Dict[str, Any]] = []
    token: str | None = None
    while True:
        try:
            response = (
                service.calendarList().list(**({"pageToken": token} if token else {})).execute()
            )
        except Exception as exc:
            reason = str(exc).lower()
            if _http_status(exc) == 403 and ("insufficient" in reason or "scope" in reason):
                raise CalendarListPermissionError(
                    "カレンダー一覧を取得する追加権限がありません。"
                    "「再接続（権限の更新）」で許可してください。"
                    "現在の対象カレンダーの予定取得・書き出しはそのまま利用できます。"
                ) from exc
            raise
        result.extend(
            row for row in response.get("items", []) if row.get("accessRole") in ("owner", "writer")
        )
        token = response.get("nextPageToken")
        if not token:
            return result


def propose_prep_tasks_from_events(
    *,
    config: AppConfig | None = None,
    service_factory: Callable[[], Any] | None = None,
    today: dt.date | None = None,
) -> ProposalResult:
    """今日から七日間の会議・締切などを承認待ちに提案し、予定 ID の再提案を防ぐ。"""
    from symnote.core.google_calendar import today_in_timezone

    config = effective_config(config)
    today = today or today_in_timezone(config.google_calendar_timezone)
    result = fetch_events(
        today, today + dt.timedelta(days=7), config=config, service_factory=service_factory
    )
    if result.offline and result.fetched_at is None:
        raise RuntimeError(result.message)
    candidates: List[CandidateInput] = []
    duplicates = 0
    with connection() as conn:
        known = {
            row[0]
            for row in conn.execute(
                "SELECT source_ref FROM task_candidates WHERE source = 'calendar'"
            )
        }
    for event in result.events:
        title = " ".join(str(event.get("summary", "")).split())
        reference = str(event.get("id", ""))
        if not reference or not any(
            word in title.lower()
            for word in ("会議", "締切", "提出", "発表", "meeting", "deadline", "presentation")
        ):
            continue
        if event.get("extendedProperties", {}).get("private", {}).get("symnote_task_id"):
            continue
        if reference in known:
            duplicates += 1
            continue
        try:
            begin, _ = event_bounds(event, config.google_calendar_timezone)
            candidate = validate_candidate(
                source="calendar",
                source_ref=reference,
                title=f"{title[:190]}の準備をする",
                details="予定に向けた準備内容を確認してください。",
                excerpt=title[:1000],
                due_date=begin.date().isoformat(),
                due_time=begin.strftime("%H:%M") if "dateTime" in event.get("start", {}) else None,
            )
        except (KeyError, ValueError):
            continue
        candidates.append(candidate)
        known.add(reference)
    created: List[int] = []
    for offset in range(0, len(candidates), 20):
        proposed = propose_candidates(candidates[offset : offset + 20], deduplicate_source_ref=True)
        created.extend(proposed.created_ids)
        duplicates += proposed.duplicate_count
    return ProposalResult(created, duplicates)
