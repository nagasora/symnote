"""他の情報を描かず、一つのタスクだけに集中するStreamlitビュー。"""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st
import streamlit.components.v1 as components

from symnote.core.focus import (
    ALLOWED_MINUTES,
    FocusResult,
    FocusSession,
    active_session,
    finish_session,
    start_session,
    stop_session,
    today_focus_summary,
)
from symnote.views.quest_view import render_celebration
from symnote.views.text import escape_markdown

_COMPONENT_PATH = Path(__file__).resolve().parents[1] / "components" / "focus_timer" / "index.html"
_NOTICE_KEY = "focus_finished_notice"


def _render_notice() -> None:
    """終了通知と追加報酬を一回だけ表示する。通常報酬は親の既存経路に任せる。"""
    result: Optional[FocusResult] = st.session_state.pop(_NOTICE_KEY, None)
    if result is None:
        return
    if result.session.outcome == "stopped":
        st.info("ここまでの集中も大切な一歩。好きなときに、また始められます。")
    else:
        st.success("集中おつかれさまでした。ひと息つきましょう。")
        if result.bonus_reward is not None:
            render_celebration([result.bonus_reward])


def _render_summary() -> None:
    """今日の集中記録を短いテキストで伝える。"""
    summary = today_focus_summary(date.today())
    st.caption(
        f"今日の集中 {summary.total_minutes}分 ・ 完了 {summary.completed_sessions}回 "
        f"・ 連続 {summary.consecutive_sessions}セッション"
    )


def render_focus_launcher(tasks: List[Dict[str, Any]]) -> None:
    """ホームに未完了タスク・15/25/50分の選択と明示的な開始操作を表示する。"""
    _render_notice()
    with st.container(border=True):
        st.subheader("集中する")
        _render_summary()
        if active_session() is not None:
            st.info("進行中の集中があります。画面を更新すると戻れます。")
            return
        available = {
            int(task["id"]): str(task.get("raw_text") or "タスク")
            for task in tasks
            if task.get("kind", "task") == "task" and task.get("status") != "done"
        }
        if not available:
            st.caption("未完了のタスクを用意したら、ここから集中を始められます。")
            return
        with st.form("focus_launcher"):
            selected = st.selectbox(
                "今これだけ", options=list(available), format_func=available.__getitem__
            )
            minutes = st.selectbox("集中する時間（分）", options=ALLOWED_MINUTES, index=1)
            submitted = st.form_submit_button("集中を始める", use_container_width=True)
        if submitted:
            try:
                start_session(selected, minutes)
            except (ValueError, sqlite3.Error) as exc:
                st.warning(f"集中を開始できませんでした。{exc}")
            else:
                st.rerun()


def _render_timer(session: FocusSession) -> None:
    """開始日時を安全に埋め込み、ブラウザで継続する円形タイマーを表示する。"""
    payload = {
        "startedAt": session.started_at.astimezone().isoformat(),
        "plannedMinutes": session.planned_minutes,
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    document = _COMPONENT_PATH.read_text(encoding="utf-8").replace(
        "__FOCUS_PAYLOAD_BASE64__", encoded
    )
    components.html(document, height=400, scrolling=False)


def render_focus_mode() -> bool:
    """集中中なら専用画面を描いてTrueを返す。親はTrueのとき他のUIを描かない。"""
    _render_notice()
    session = active_session()
    if session is None:
        return False
    st.markdown(
        """<style>
    [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
    [data-testid="stHeader"], [data-testid="stToolbar"], #MainMenu, footer {display:none!important}
    .block-container {max-width:760px;padding-top:2rem}
    </style>""",
        unsafe_allow_html=True,
    )
    st.caption("今は、これだけ。")
    st.markdown(f"## {escape_markdown(session.title)}")
    _render_timer(session)
    done_column, stop_column = st.columns(2)
    done = done_column.button(
        "完了した！", key=f"focus_done_{session.id}", type="primary", use_container_width=True
    )
    stopped = stop_column.button(
        "中断する", key=f"focus_stop_{session.id}", use_container_width=True
    )
    if done or stopped:
        try:
            result = (
                finish_session(session.id, complete_task=True) if done else stop_session(session.id)
            )
        except (ValueError, sqlite3.Error) as exc:
            st.warning(f"集中の記録を更新できませんでした。{exc}")
        else:
            st.session_state[_NOTICE_KEY] = result
            st.rerun()
    st.caption("早めに終わっても、中断しても大丈夫。記録はそのまま残ります。")
    _render_summary()
    return True
