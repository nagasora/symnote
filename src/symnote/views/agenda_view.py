"""今日の Google 予定と期限タスクを時刻順に表示する。"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

import streamlit as st

from symnote.core.calendar_events import event_bounds, fetch_events, is_symnote_generated
from symnote.core.calendar_settings import effective_config
from symnote.core.db import connection
from symnote.core.google_calendar import is_connected, today_in_timezone
from symnote.views.text import escape_markdown


def build_today_agenda(
    events: List[Dict[str, Any]],
    tasks: List[Dict[str, Any]],
    day: dt.date,
    timezone: str,
) -> Tuple[List[Dict[str, Any]], int]:
    """一日のタイムラインと、重複を除いた Google 予定の占有時間から空き分数を返す。"""
    begin = dt.datetime.combine(day, dt.time.min, ZoneInfo(timezone))
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, ZoneInfo(timezone))
    rows: List[Dict[str, Any]] = []
    busy: List[Tuple[dt.datetime, dt.datetime]] = []
    for event in events:
        if event.get("status") == "cancelled" or is_symnote_generated(event):
            continue
        try:
            start, finish = event_bounds(event, timezone)
        except (KeyError, ValueError, TypeError):
            continue
        if finish <= begin or start >= end:
            continue
        rows.append(
            {
                "time": max(begin, start),
                "label": "終日" if "date" in event["start"] else start.strftime("%H:%M"),
                "kind": "Google 予定",
                "title": event.get("summary") or "（無題）",
            }
        )
        declined = any(
            person.get("self") and person.get("responseStatus") == "declined"
            for person in event.get("attendees", [])
        )
        if event.get("transparency") != "transparent" and not declined and finish > start:
            busy.append((max(start, begin), min(finish, end)))
    for task in tasks:
        if task.get("status") == "done" or task.get("due_date") != day.isoformat():
            continue
        try:
            time = dt.time.fromisoformat(task["due_time"]) if task.get("due_time") else None
        except (ValueError, TypeError):
            time = None
        rows.append(
            {
                "time": dt.datetime.combine(day, time or dt.time.max, ZoneInfo(timezone)),
                "label": time.strftime("%H:%M") if time else "時刻なし",
                "kind": "タスク期限",
                "title": task.get("tags") or task.get("raw_text") or "（無題）",
            }
        )
    occupied = 0.0
    cursor = begin
    for start, finish in sorted(busy):
        if finish > cursor:
            occupied += (finish - max(cursor, start)).total_seconds()
            cursor = finish
    free = max(0, int(((end - begin).total_seconds() - occupied) / 60))
    return sorted(rows, key=lambda row: row["time"]), free


def render_today_agenda() -> None:
    """今日のアジェンダを表示し、通信失敗時は最終取得時刻を示す。"""
    config = effective_config()
    day = today_in_timezone(config.google_calendar_timezone)
    st.subheader("今日の予定・期限")
    with connection() as conn:
        tasks = [
            dict(row)
            for row in conn.execute(
                "SELECT id, tags, raw_text, status, due_date, due_time FROM items WHERE kind = 'task' AND due_date = ?",
                (day.isoformat(),),
            )
        ]
    events: List[Dict[str, Any]] = []
    connected = is_connected()
    events_available = False
    if connected:
        result = fetch_events(day, day + dt.timedelta(days=1), config=config)
        events = result.events
        events_available = result.fetched_at is not None
        if result.fetched_at:
            st.caption(
                f"最終取得: {result.fetched_at}" + ("（保存済みの予定）" if result.cached else "")
            )
        if result.offline:
            st.warning(result.message)
    else:
        st.caption("Google Calendar は未接続です。ローカルタスクのみ表示します。")
    rows, free = build_today_agenda(events, tasks, day, config.google_calendar_timezone)
    if connected and not events_available:
        st.caption("Google 予定を取得できないため、空き時間は不明です。")
    if events_available:
        st.caption(
            f"取得済み予定による空き時間（00:00〜24:00、タスクは所要時間未指定のため除外）: {free // 60}時間 {free % 60}分"
        )
    for row in rows:
        st.markdown(f"**{row['label']}** [{row['kind']}] {escape_markdown(str(row['title']))}")
    if not rows:
        st.info("今日の予定・期限タスクはありません。")
