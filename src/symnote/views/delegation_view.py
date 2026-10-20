"""タスクの AI 委任と、届いた進捗報告の承認画面を表示する。"""

from __future__ import annotations

import html
from typing import Any, Dict, cast

import streamlit as st

from symnote.core import delegation

AGENT_LABELS: Dict[str, str] = {
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "claude_code": "Claude Code",
    "codex": "Codex",
    "other": "その他",
}
STATUS_LABELS: Dict[str, str] = {"progress": "進捗", "done": "完了", "blocked": "ブロック"}


def render_delegate_button(task: Dict[str, Any]) -> None:
    """担当選択とコピー用指示書、委任の保存・解除ボタンを表示する。"""
    task_id = int(task["id"])
    # Why not st.expander: タスク編集欄自体が expander の中にあり、Streamlit は入れ子を許さない。
    current = task.get("delegated_to")
    # Why not 担当名をラベルに入れる: ラベルが変わるとウィジェットが作り直され、保存直後に閉じてしまう。
    opened = st.toggle("🤖 AI に任せる", key=f"delegate_open_{task_id}")
    if opened:
        current = task.get("delegated_to")
        if current:
            st.caption(f"担当: {AGENT_LABELS.get(current, current)}")
        options = list(AGENT_LABELS)
        agent = st.selectbox(
            "担当 AI",
            options,
            index=options.index(current) if current in options else 0,
            format_func=_agent_label,
            key=f"delegate_agent_{task_id}",
        )
        try:
            handoff = delegation.generate_handoff(task_id, cast(delegation.Delegate, agent))
        except ValueError as exc:
            st.error(str(exc))
            return
        st.code(handoff, language="markdown")
        st.caption(
            "指示書をコピーして担当 AI に貼り付けてください。保存だけでは AI は起動しません。"
        )
        if st.button(
            "任せた", key=f"delegate_save_{task_id}", disabled=task.get("status") == "done"
        ):
            try:
                delegation.set_delegation(task_id, cast(delegation.Delegate, agent))
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.rerun()
        if current and st.button("任せるのをやめる", key=f"delegate_clear_{task_id}"):
            try:
                delegation.set_delegation(task_id, None)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.rerun()


def _agent_label(agent: str) -> str:
    """選択欄で表示する担当名を返す。"""
    return AGENT_LABELS[agent]


def pending_report_count() -> int:
    """受信箱のバッジに使う承認待ち報告件数を返す。"""
    return delegation.pending_report_count()


def render_agent_reports() -> None:
    """受信箱に報告を表示し、明示承認または却下を受け付ける。"""
    reports = delegation.list_pending_reports()
    if not reports:
        return
    st.subheader("AI からの進捗報告")
    st.caption(
        "承認した報告だけを反映します。ブロックは重要度を最低5にし、ゴールのみの報告は進捗メモとして保存します。"
    )
    for report in reports:
        with st.container(border=True):
            _render_report(report)


def _render_report(report: Dict[str, Any]) -> None:
    """報告データをエスケープして表示し、二重反映しない審査操作へ渡す。"""
    task_id, goal_id = report["task_id"], report["goal_id"]
    targets = []
    if task_id is not None:
        title = report["task_title"] or report["task_text"] or "削除済み"
        targets.append(f"タスク #{task_id}: {title}")
    if goal_id is not None:
        targets.append(f"ゴール #{goal_id}: {report['goal_title'] or '削除済み'}")
    heading = f"{report['agent'] or 'AI'} / {STATUS_LABELS[report['suggested_status']]}"
    content = [heading, *targets, report["summary"], report["details"]]
    st.markdown(
        "<div style='white-space:pre-wrap;overflow-wrap:anywhere'>"
        + "\n\n".join(html.escape(str(value)) for value in content if value)
        + "</div>",
        unsafe_allow_html=True,
    )
    if st.button("承認", key=f"report_approve_{report['id']}"):
        try:
            delegation.approve_report(report["id"])
        except ValueError as exc:
            st.error(str(exc) + " 内容を確認して却下してください。")
        else:
            st.rerun()
    if st.button("却下", key=f"report_reject_{report['id']}"):
        delegation.reject_report(report["id"])
        st.rerun()
