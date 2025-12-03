from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Dict, List, Optional

import streamlit as st

from symnote.core.db import (
    fetch_counts_by_date,
    fetch_due_date_counts_by_date,
    fetch_items_by_date,
    fetch_tasks_due_on,
    update_item_fields,
)


def _get_month_range(year: int, month: int) -> tuple[date, date]:
    """その月の開始日と終了日を返す"""
    first_day = date(year, month, 1)
    # 翌月の1日の前日が月末
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    last_day = next_month - timedelta(days=1)
    return first_day, last_day


def render_calendar_tab() -> None:
    st.subheader("🗓️ カレンダー")

    # Session State for Calendar Navigation
    if "cal_year" not in st.session_state:
        st.session_state.cal_year = date.today().year
    if "cal_month" not in st.session_state:
        st.session_state.cal_month = date.today().month

    # Navigation
    col_prev, col_curr, col_next = st.columns([1, 3, 1])
    with col_prev:
        if st.button("← 前月"):
            if st.session_state.cal_month == 1:
                st.session_state.cal_month = 12
                st.session_state.cal_year -= 1
            else:
                st.session_state.cal_month -= 1
            st.rerun()
    with col_curr:
        st.markdown(f"<h3 style='text-align: center;'>{st.session_state.cal_year}年 {st.session_state.cal_month}月</h3>", unsafe_allow_html=True)
    with col_next:
        if st.button("翌月 →"):
            if st.session_state.cal_month == 12:
                st.session_state.cal_month = 1
                st.session_state.cal_year += 1
            else:
                st.session_state.cal_month += 1
            st.rerun()

    # Data Fetching
    year = st.session_state.cal_year
    month = st.session_state.cal_month
    start_date, end_date = _get_month_range(year, month)
    
    # Fetch counts
    counts = {}
    # Initialize for all days in month
    curr = start_date
    while curr <= end_date:
        counts[curr.isoformat()] = {"task": 0, "memo": 0, "due": 0}
        curr += timedelta(days=1)

    for d_str, task_count, memo_count in fetch_counts_by_date(start_date.isoformat(), end_date.isoformat()):
        if d_str in counts:
            counts[d_str]["task"] = task_count
            counts[d_str]["memo"] = memo_count
    
    for d_str, due_count in fetch_due_date_counts_by_date(start_date.isoformat(), end_date.isoformat()):
        if d_str in counts:
            counts[d_str]["due"] = due_count

    # Calendar Grid
    # Header
    cols = st.columns(7)
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    for i, w in enumerate(weekdays):
        cols[i].markdown(f"**{w}**")

    # Weeks
    month_matrix = calendar.monthcalendar(year, month)
    for week in month_matrix:
        cols = st.columns(7)
        for i, day in enumerate(week):
            with cols[i]:
                if day == 0:
                    st.write("") # Empty cell for other month
                else:
                    d_obj = date(year, month, day)
                    d_str = d_obj.isoformat()
                    
                    # Style
                    is_today = d_obj == date.today()
                    day_style = "**" if is_today else ""
                    
                    st.markdown(f"{day_style}{day}{day_style}")
                    
                    c = counts.get(d_str, {"task": 0, "memo": 0, "due": 0})
                    if c["task"] > 0 or c["memo"] > 0 or c["due"] > 0:
                        info = []
                        if c["due"] > 0:
                            info.append(f"🚨{c['due']}")
                        if c["task"] > 0:
                            info.append(f"T:{c['task']}")
                        if c["memo"] > 0:
                            info.append(f"M:{c['memo']}")
                        st.caption(" ".join(info))
                    
                    if st.button("詳細", key=f"view_{d_str}"):
                        st.session_state["selected_calendar_date"] = d_str

    st.divider()

    # Details Section
    target_date_str = st.session_state.get("selected_calendar_date", date.today().isoformat())
    st.markdown(f"### {target_date_str} の詳細")
    
    created_items = fetch_items_by_date(target_date_str)
    due_tasks = fetch_tasks_due_on(target_date_str)

    combined_items = {item['id']: item for item in created_items}
    for task in due_tasks:
        if task['id'] not in combined_items:
            combined_items[task['id']] = task

    if not combined_items:
        st.info("この日付のタスク・メモはありません。")
        return
    
    all_items = list(combined_items.values())

    tasks = [i for i in all_items if i.get("kind") == "task"]
    memos = [i for i in all_items if i.get("kind") == "memo"]

    uncompleted_tasks = [t for t in tasks if t.get("status") != "done"]
    completed_tasks = [t for t in tasks if t.get("status") == "done"]

    tab_uncompleted, tab_completed, tab_memos = st.tabs(["未完了タスク", "完了済みタスク", "メモ"])

    with tab_uncompleted:
        if not uncompleted_tasks:
            st.info("未完了タスクはありません。")
        else:
            for t in uncompleted_tasks:
                is_due = t.get("due_date") == target_date_str
                is_created = t.get("date") == target_date_str
                label = ""
                if is_due and is_created:
                    label = " (作成 & 期限)"
                elif is_due:
                    label = " (期限)"
                elif is_created:
                    label = " (作成)"

                display_title = t.get("tags") or t.get("raw_text", "")[:20]
                with st.expander(f"[{t.get('status', '')}] {display_title}{label}"):
                    st.write(t.get("raw_text", ""))
                    st.caption(f"優先度: {t.get('importance')} × {t.get('urgency')} / effort {t.get('effort')} / energy {t.get('energy')}")
                    if st.button("✔ 完了", key=f"done_calendar_{t['id']}"):
                        update_item_fields(t["id"], status="done")
                        st.success("完了に更新しました。")
                        st.rerun()

    with tab_completed:
        if not completed_tasks:
            st.info("完了済みタスクはありません。")
        else:
            for t in completed_tasks:
                display_title = t.get("tags") or t.get("raw_text", "")[:20]
                with st.expander(f"✅ {display_title}"):
                    st.write(t.get("raw_text", ""))
                    st.caption(f"完了済み (ID: {t['id']})")
                    if st.button("未完了に戻す", key=f"revert_calendar_{t['id']}"):
                        update_item_fields(t["id"], status="inbox")
                        st.success("未完了に戻しました。")
                        st.rerun()

    with tab_memos:
        if not memos:
            st.info("メモはありません。")
        else:
            for m in memos:
                display_title = m.get("tags") or m.get("raw_text", "")[:20]
                with st.expander(f"メモ: {display_title}"):
                    st.write(m.get("raw_text", ""))
                    st.caption(f"タグ: {m.get('tags') or '-'}")
