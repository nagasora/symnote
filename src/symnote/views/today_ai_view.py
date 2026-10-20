"""「今日」画面の AI 部品：自然文クイック入力、AI の今日の計画、タスクの分解。"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List

import streamlit as st

from symnote.config import load_config
from symnote.core.db import connection, delete_item, insert_item, update_item_fields
from symnote.core.task_ai import ParsedTask, breakdown_task, parse_quick_task, plan_day
from symnote.views.text import escape_markdown

PLAN_STATE_KEY = "ai_day_plan"
QUICK_PREVIEW_KEY = "quick_add_preview"


def save_parsed_task(parsed: ParsedTask, *, status: str = "inbox") -> int:
    """読み取ったタスクを保存し、ID を返す。"""
    return insert_item(
        kind="task",
        raw_text=parsed.details or parsed.title,
        tags=parsed.title,
        due_date=parsed.due_date,
        due_time=parsed.due_time,
        status=status,
        ai_category="task",
        importance=parsed.importance,
        urgency=4 if parsed.importance >= 5 else 3,
    )


def _describe(parsed: ParsedTask) -> str:
    when = parsed.due_date or "期限なし"
    if parsed.due_time:
        when += f" {parsed.due_time}"
    return f"**{escape_markdown(parsed.title)}** ・ {when} ・ 重要度 {parsed.importance}"


def render_quick_add() -> None:
    """1 行書くだけでタスクにする入力欄。日時は AI（使えなければルール）で読み取る。"""
    use_ai = bool(load_config().llm_api_key)
    with st.form("quick_add_form", clear_on_submit=True):
        text = st.text_input(
            "クイック追加",
            placeholder="例: 明日15時までに企画書を送る / 来週月曜 歯医者 / 至急 請求書チェック",
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button("＋ 追加", type="primary")
    if submitted and text.strip():
        with st.spinner("読み取り中…" if use_ai else None):
            parsed = parse_quick_task(text, use_ai=use_ai)
        task_id = save_parsed_task(parsed)
        st.session_state[QUICK_PREVIEW_KEY] = (task_id, parsed)
    preview = st.session_state.get(QUICK_PREVIEW_KEY)
    if preview:
        task_id, parsed = preview
        label = "AI" if parsed.source == "ai" else "ルール"
        st.success(f"追加しました（{label}で読み取り）: {_describe(parsed)}")
        if st.button("取り消す", key=f"undo_quick_{task_id}"):
            delete_item(task_id)
            st.session_state.pop(QUICK_PREVIEW_KEY, None)
            st.rerun()
    st.caption(
        "日時・重要度は文章から自動で読み取ります。"
        + ("" if use_ai else "（API キー未設定のためルールで読み取り）")
    )


def open_task_ids(task_ids: List[int]) -> List[int]:
    """指定 ID のうち、いま存在する未完了タスクの ID だけを元の順で返す。"""
    if not task_ids:
        return []
    placeholders = ",".join("?" for _ in task_ids)
    with connection() as conn:
        rows = conn.execute(
            f"SELECT id FROM items WHERE id IN ({placeholders}) "
            "AND kind = 'task' AND status != 'done'",
            list(task_ids),
        ).fetchall()
    alive = {int(row["id"]) for row in rows}
    return [task_id for task_id in task_ids if task_id in alive]


def apply_plan_to_today(task_ids: List[int]) -> int:
    """計画のタスクを「今日」にする。計画後に完了・削除されたものは触らない。"""
    targets = open_task_ids(task_ids)
    for task_id in targets:
        update_item_fields(task_id, status="today")
    return len(targets)


def today_events_for_plan() -> List[Dict[str, Any]]:
    """対象日でクリップした予定を、リスト互換の取得状態付きオブジェクトで返す。"""
    from symnote.core.calendar_events import fetch_events
    from symnote.core.calendar_settings import effective_config
    from symnote.core.google_calendar import is_connected, today_in_timezone
    from symnote.core.timeblock import CalendarPlanEvents, normalize_calendar_events

    try:
        config = effective_config()
        day = today_in_timezone(config.google_calendar_timezone)
        if not is_connected():
            return CalendarPlanEvents(
                notice="空き時間未確認：Googleカレンダーが未接続です。", day=day.isoformat()
            )
        result = fetch_events(day, day + dt.timedelta(days=1), config=config)
        events = normalize_calendar_events(result.events, day, config.google_calendar_timezone)
        confirmed = not result.offline and result.fetched_at is not None
        notice = (
            ""
            if confirmed
            else "空き時間未確認：予定を取得できませんでした。キャッシュがあれば参考表示します。"
        )
        return CalendarPlanEvents(events, confirmed=confirmed, notice=notice, day=day.isoformat())
    except Exception:
        return CalendarPlanEvents(notice="空き時間未確認：カレンダーの予定を確認できませんでした。")


def render_ai_plan(tasks: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> None:
    """ボタン 1 つで今日の計画を作り、計画のタスクをまとめて「今日」に入れられる。"""
    st.subheader("🧭 今日の計画")
    if not getattr(events, "confirmed", False):
        st.info(
            getattr(events, "notice", "")
            or "空き時間未確認：予定は未確認のため、計画は参考として扱ってください。"
        )
    make_plan = st.button("AI で計画を立てる", key="make_ai_plan", use_container_width=True)
    if make_plan:
        with st.spinner("AI が今日の計画を考えています…"):
            st.session_state[PLAN_STATE_KEY] = (
                dt.date.today().isoformat(),
                plan_day(tasks, events, use_ai=bool(load_config().llm_api_key)),
            )
    stored = st.session_state.get(PLAN_STATE_KEY)
    if not stored or stored[0] != dt.date.today().isoformat():
        st.caption("期限・重要度・今日の予定から、今日やることと順番を提案します。")
        return
    plan = stored[1]
    if plan.notice:
        st.info(plan.notice)
    st.write(escape_markdown(plan.summary))
    alive = set(open_task_ids([item.task_id for item in plan.items]))
    items = [item for item in plan.items if item.task_id in alive]
    if not items:
        return
    for index, item in enumerate(items, start=1):
        start = f"{item.start_time} " if item.start_time else ""
        reason = f" — {escape_markdown(item.reason)}" if item.reason else ""
        st.markdown(f"{index}. {start}**{escape_markdown(item.title)}**{reason}")
    if st.button("この計画のタスクを「今日」に入れる", key="apply_ai_plan"):
        moved = apply_plan_to_today([item.task_id for item in plan.items])
        st.success(f"{moved} 件を今日のタスクにしました。")
        st.rerun()


def render_breakdown_button(task: Dict[str, Any]) -> None:
    """タスクを AI で小さな作業に分け、選んだものをサブタスクとして追加する。"""
    key = f"breakdown_{task['id']}"
    if st.button("🪄 AI で小さく分ける", key=f"{key}_run"):
        try:
            with st.spinner("AI が作業を分解しています…"):
                st.session_state[key] = breakdown_task(
                    (task.get("tags") or "").strip() or task.get("raw_text", "")[:200],
                    task.get("raw_text", ""),
                )
        except Exception as exc:
            st.error(f"分解できませんでした: {exc}")
    steps = st.session_state.get(key)
    if not steps:
        return
    chosen = [
        step for i, step in enumerate(steps) if st.checkbox(step, value=True, key=f"{key}_{i}")
    ]
    if st.button("選んだ作業をタスクに追加", key=f"{key}_save", disabled=not chosen):
        parent = (task.get("tags") or "").strip() or f"#{task['id']}"
        for step in chosen:
            insert_item(
                kind="task",
                raw_text=f"「{parent}」の作業",
                tags=step,
                due_date=task.get("due_date"),
                due_time=task.get("due_time"),
                status=task.get("status") or "inbox",
                ai_category="task",
                importance=int(task.get("importance") or 3),
                urgency=int(task.get("urgency") or 3),
            )
        st.session_state.pop(key, None)
        st.success(f"{len(chosen)} 件のサブタスクを追加しました。")
        st.rerun()
