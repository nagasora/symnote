"""外部 AI が提案した ToDo 候補を承認・却下する画面。"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Optional

import streamlit as st

from symnote.core.candidates import approve_candidate, list_candidates, reject_candidate

SOURCE_LABELS: Dict[str, str] = {
    "gmail": "📧 Gmail",
    "slack": "💬 Slack",
    "notion": "📓 Notion",
    "chat": "🤖 AI との会話",
    "calendar": "📆 カレンダー",
    "other": "🔗 その他",
}


def _parse_optional_date(value: Optional[str]) -> Optional[dt.date]:
    """候補の期限日を date に変換する。不正・空なら None。"""
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _render_candidate(candidate: Dict[str, Any]) -> None:
    """候補 1 件を、手直しできる承認フォームとして描画する。"""
    candidate_id = int(candidate["id"])
    with st.container(border=True):
        label = SOURCE_LABELS.get(candidate["source"], candidate["source"])
        confidence = candidate.get("confidence")
        meta = f"{label}・{candidate['created_at']}"
        if confidence is not None:
            meta += f"・確度 {float(confidence):.0%}"
        # 候補の文字列は外部由来なので、Markdown として解釈させず text で表示する
        # （画像や偽リンクの埋め込みを防ぐため）。
        st.text(meta)
        with st.form(f"candidate_form_{candidate_id}"):
            title = st.text_input("タイトル", value=candidate["title"])
            details = st.text_area("詳細", value=candidate.get("details") or "", height=80)
            date_column, time_column = st.columns(2)
            with date_column:
                due_date = st.date_input(
                    "期限日（空欄で未設定）",
                    value=_parse_optional_date(candidate.get("due_date")),
                )
            with time_column:
                due_time_text = st.text_input(
                    "期限時刻（HH:MM・任意）", value=candidate.get("due_time") or ""
                )
            if candidate.get("excerpt"):
                st.caption("根拠となる原文")
                st.text(candidate["excerpt"])
            approve_column, reject_column = st.columns(2)
            approved = approve_column.form_submit_button(
                "承認してタスクに追加", type="primary", use_container_width=True
            )
            rejected = reject_column.form_submit_button("却下", use_container_width=True)
        if candidate.get("source_url"):
            st.link_button("原文を開く", candidate["source_url"])

    if approved:
        try:
            approve_candidate(
                candidate_id,
                title=title,
                details=details,
                due_date=due_date.isoformat() if due_date else "",
                due_time=due_time_text,
            )
        except (ValueError, LookupError) as exc:
            st.error(str(exc))
            return
        st.toast(f"「{title}」をタスクに追加しました。")
        st.rerun()
    if rejected:
        try:
            reject_candidate(candidate_id)
        except LookupError as exc:
            st.error(str(exc))
            return
        st.rerun()


def render_candidates_tab() -> None:
    """承認待ちの候補一覧と、最近の判断履歴を表示する。"""
    pending = list_candidates("pending", limit=100)
    if not pending:
        st.info(
            "承認待ちの候補はありません。MCP 連携した AI に"
            "「Gmail から ToDo を探して SymNote に提案して」のように依頼すると、ここに届きます。"
        )
    for candidate in pending:
        _render_candidate(candidate)

    with st.expander("最近の承認・却下"):
        history = list_candidates("approved", limit=20) + list_candidates("rejected", limit=20)
        history.sort(key=lambda row: row.get("decided_at") or "", reverse=True)
        if not history:
            st.caption("まだ履歴はありません。")
        for row in history[:20]:
            mark = "✅" if row["status"] == "approved" else "🚫"
            st.text(f"{mark} {row['title']}（{SOURCE_LABELS.get(row['source'], row['source'])}）")
