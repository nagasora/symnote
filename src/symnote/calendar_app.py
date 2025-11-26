from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List

import streamlit as st

from symnote.core.db import fetch_counts_by_date, fetch_items_by_date
from symnote.core.db import update_item_fields


def _week_dates(ref: date | None = None) -> List[date]:
    today = ref or date.today()
    monday = today - timedelta(days=today.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def render_calendar_tab() -> None:
    st.subheader("🗓️ カレンダー（週ビュー）")
    today = date.today()
    default_week = _week_dates(today)

    selected = st.date_input("週の開始日（任意で変更）", default_week[0])
    week = _week_dates(selected)
    start, end = week[0], week[-1]

    counts = {d.isoformat(): {"task": 0, "memo": 0} for d in week}
    for d_str, task_count, memo_count in fetch_counts_by_date(start.isoformat(), end.isoformat()):
        if d_str in counts:
            counts[d_str]["task"] = task_count
            counts[d_str]["memo"] = memo_count

    cols = st.columns(7)
    for col, d in zip(cols, week):
        d_str = d.isoformat()
        with col:
            st.markdown(f"**{d.strftime('%a %m/%d')}**")
            st.caption(f"タスク {counts[d_str]['task']} / メモ {counts[d_str]['memo']}")
            if st.button("見る", key=f"view_{d_str}"):
                st.session_state["selected_calendar_date"] = d_str

    target = st.session_state.get("selected_calendar_date", today.isoformat())
    st.markdown(f"### {target} の詳細")
    items = fetch_items_by_date(target)
    if not items:
        st.info("この日付のタスク・メモはありません。")
        return

    tasks = [i for i in items if i.get("kind") == "task"]
    memos = [i for i in items if i.get("kind") == "memo"]

    if tasks:
        st.write("#### タスク")
        for t in tasks:
            with st.expander(f"[{t.get('status', '')}] {t.get('raw_text', '')[:40]}..."):
                st.write(t.get("raw_text", ""))
                st.caption(f"優先度: {t.get('importance')} × {t.get('urgency')} / effort {t.get('effort')} / energy {t.get('energy')}")
                if st.button("✔ 完了", key=f"done_calendar_{t['id']}"):
                    update_item_fields(t["id"], status="done")
                    st.success("完了に更新しました。")
                    st.rerun()

    if memos:
        st.write("#### メモ")
        for m in memos:
            with st.expander(f"メモ ID {m['id']}"):
                st.write(m.get("raw_text", ""))
                st.caption(f"タグ: {m.get('tags') or '-'}")
