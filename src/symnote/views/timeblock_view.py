"""今日の予定とタスク枠を縦の時間割として提案・承認・書き出しする。"""

from __future__ import annotations

import datetime as dt
from html import escape
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import streamlit as st

from symnote.config import load_config
from symnote.core.calendar_settings import effective_config
from symnote.core.google_calendar import _calendar_service, is_connected
from symnote.core.runtime import is_test_mode
from symnote.core.timeblock import (
    Block,
    CalendarPlanEvents,
    TimeblockPlan,
    cancel_block,
    export_blocks_to_calendar,
    flush_cancelled_blocks,
    list_blocks,
    list_pending_cancellations,
    propose_timeblocks,
    reconcile_task_blocks,
    save_blocks,
)

STATE_KEY = "timeblock_preview"


def _timeline_html(events: List[Dict[str, Any]], blocks: List[Dict[str, Any]]) -> str:
    """タイトルをエスケープし、色に依存しない時刻順の縦リストを構築する。"""
    rows: List[Dict[str, Any]] = []
    for event in events:
        if event.get("status") != "cancelled":
            rows.append(
                {
                    "start": event.get("start", ""),
                    "end": event.get("end", ""),
                    "title": event.get("summary", "予定"),
                    "label": "予定",
                }
            )
    for block in blocks:
        label = "書き出し済み" if block.get("status") == "exported" else "タスク"
        if block.get("estimated_default"):
            label += " · 見積もり 30 分で仮置き"
        rows.append(
            {
                "start": block["start_time"],
                "end": block["end_time"],
                "title": block["title"],
                "label": label,
            }
        )
    rows.sort(key=lambda row: "00:00" if row["start"] == "終日" else str(row["start"]))
    items: List[str] = []
    for row in rows:
        period = str(row["start"]) + (" – " + str(row["end"]) if row["end"] else "")
        items.append(
            '<li style="border-inline-start:2px solid currentColor;padding:0.6rem 1rem;'
            'margin:0.4rem 0;overflow-wrap:anywhere">'
            f"<div><strong>{escape(period)}</strong> · {escape(str(row['label']))}</div>"
            f'<div style="margin-top:0.25rem">{escape(str(row["title"]))}</div></li>'
        )
    return (
        '<ol aria-label="今日の時間割" style="list-style:none;padding:0;margin:0">'
        + "".join(items)
        + "</ol>"
    )


def _preview_rows(blocks: List[Block]) -> List[Dict[str, Any]]:
    """プレビューの枠を保存済みの枠と共通の表示形式にする。"""
    return [
        {
            "task_id": b.task_id,
            "title": b.title,
            "start_time": b.start_time,
            "end_time": b.end_time,
            "estimated_default": b.estimated_default,
        }
        for b in blocks
    ]


def render_timeblock(tasks: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> None:
    """時間割のプレビュー、明示承認による保存、任意のカレンダー出力を表示する。"""
    st.subheader("AI 時間割")
    try:
        config = effective_config()
        now = dt.datetime.now(ZoneInfo(config.google_calendar_timezone))
        day = now.date().isoformat()
        reconcile_task_blocks()
        all_blocks = list_blocks(day)
        saved = [b for b in all_blocks if b["status"] != "cancelled"]
        cancellation_pending = list_pending_cancellations()
    except Exception:
        st.error("時間割を読み込めませんでした。DBとタイムゾーン設定を確認してください。")
        return
    if not getattr(events, "confirmed", False):
        st.warning(
            getattr(events, "notice", "")
            or "空き時間未確認：予定を取得できていないため、仮の時間割として表示します。"
        )
    own_event_ids = {b["calendar_event_id"] for b in all_blocks if b["calendar_event_id"]}
    visible_events = [e for e in events if e.get("id") not in own_event_ids]
    uncertain = any(b["status"] == "planned" and b["calendar_event_id"] for b in saved)
    if uncertain:
        st.info("送信結果が未確定です。先にカレンダーへの書き出しを再試行してください。")
    if st.button("⏱ 時間割を組む", key="timeblock_make", disabled=uncertain):
        fixed = [b for b in saved if b["status"] == "exported"]
        reserved_ids = {b["task_id"] for b in fixed}
        candidates = [t for t in tasks if t.get("id") not in reserved_ids]
        occupied = CalendarPlanEvents(
            list(events)
            + [
                {"start": b["start_time"], "end": b["end_time"], "summary": b["title"]}
                for b in fixed
            ],
            confirmed=bool(getattr(events, "confirmed", False)),
            notice=getattr(events, "notice", ""),
            day=day,
        )
        try:
            with st.spinner("空き時間にタスクを配置しています…"):
                plan = propose_timeblocks(
                    candidates,
                    occupied,
                    now.date(),
                    now=now,
                    use_ai=bool(load_config().llm_api_key),
                )
            st.session_state[STATE_KEY] = (day, plan)
        except Exception:
            st.session_state.pop(STATE_KEY, None)
            st.error("時間割を作れませんでした。予定の開始・終了時刻を確認してください。")
    state = st.session_state.get(STATE_KEY)
    if state and state[0] == day:
        plan: TimeblockPlan = state[1]
        st.markdown("#### 提案のプレビュー")
        if plan.notice:
            st.info(plan.notice)
        fixed_rows = [b for b in saved if b["status"] == "exported"]
        st.markdown(
            _timeline_html(visible_events, fixed_rows + _preview_rows(plan.blocks)),
            unsafe_allow_html=True,
        )
        if plan.overflow:
            st.markdown("**今日は入りきらない**")
            for block in plan.overflow:
                note = " · 見積もり 30 分で仮置き" if block.estimated_default else ""
                st.text(f"{block.title}（{block.estimate_minutes} 分）{note}")
        if not plan.blocks:
            st.info(
                "配置できるタスクがありません。終日予定・空き時間・見積もりを確認してください。"
            )
        if st.button("この時間割で進める", key="timeblock_accept", disabled=not plan.blocks):
            try:
                save_blocks(day, plan.blocks)
                st.session_state.pop(STATE_KEY, None)
                st.rerun()
            except ValueError as exc:
                st.warning(str(exc))
            except Exception:
                st.error("保存できませんでした。時間割は変更されていません。")
    if not saved and not cancellation_pending:
        return
    st.markdown("#### 保存済みの時間割")
    default_ids = {t["id"] for t in tasks if not t.get("estimate_minutes")}
    for block in saved:
        block["estimated_default"] = block["task_id"] in default_ids
    st.markdown(_timeline_html(visible_events, saved), unsafe_allow_html=True)
    try:
        connected = is_connected()
    except Exception:
        connected = False
    writable = connected and not is_test_mode()
    if not connected:
        st.caption("Google カレンダーが未接続です。「接続」画面で連携すると書き出せます。")
    elif not writable:
        st.caption("テスト環境のため、カレンダーへの書き出し・取消は送信しません。")
    for block in saved:
        if st.button(
            "この枠を取り消す", key=f"timeblock_cancel_{block['id']}", help=block["title"]
        ):
            try:
                cancel_block(block["id"])
                st.session_state.pop(STATE_KEY, None)
                if writable:
                    result = flush_cancelled_blocks(_calendar_service)
                    if result.failed:
                        st.warning(result.message)
                st.rerun()
            except Exception:
                st.error("取消を送信できませんでした。再試行してください。")
    if cancellation_pending:
        st.warning(
            f"カレンダーの取消が未送信の枠が {len(cancellation_pending)} 件あります。"
            "接続後に再試行してください。"
        )
    pending = any(b["status"] == "planned" for b in saved) or bool(cancellation_pending)
    label = "カレンダーに書き出す" if saved else "カレンダーの取消を再試行"
    if st.button(label, key="timeblock_export", disabled=not writable or not pending):
        result = export_blocks_to_calendar(day, _calendar_service)
        if result.failed:
            st.warning(result.message)
        else:
            st.session_state.pop(STATE_KEY, None)
            st.success(f"{result.synced} 件の変更を反映しました。")
            st.rerun()
