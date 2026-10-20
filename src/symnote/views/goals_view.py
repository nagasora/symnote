"""理由と成功の定義を持つゴールツリーの操作画面。"""

from __future__ import annotations

import sqlite3
from datetime import date
from functools import partial
from typing import Any, Callable, List, Optional

import streamlit as st

from symnote.core import goals, llm
from symnote.views.text import escape_markdown


def _action(action: Callable[[], Any]) -> bool:
    """操作エラーを画面に閉じ込め、手動操作を継続可能にする。"""
    try:
        action()
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        st.error(escape_markdown(str(error)))
        return False
    return True


def _goal_form(parent_id: Optional[int] = None) -> None:
    """新規または子ゴールを作るフォームを表示する。"""
    with st.form(f"create_goal_{parent_id}", clear_on_submit=True):
        title = st.text_input("タイトル", max_chars=200)
        criteria = st.text_area("成功の定義1文", max_chars=2000)
        why = st.text_area("なぜ達成したいか", max_chars=2000)
        target = st.date_input("目標日（任意）", value=None)
        submitted = st.form_submit_button("ゴールを作成")
    if submitted and _action(
        lambda: goals.create_goal(
            title, why, criteria, target.isoformat() if target else None, parent_id
        )
    ):
        st.rerun()


def _edit_goal(goal: goals.GoalProgress) -> None:
    """成功の定義、理由と期限の編集フォームを表示する。"""
    context = goals.get_goal_context(goal.id)
    with st.form(f"edit_goal_{goal.id}"):
        title = st.text_input("タイトル", value=goal.title, max_chars=200)
        why = st.text_area("なぜ", value=goal.why, max_chars=2000)
        criteria = st.text_area("成功の定義1文", value=context["success_criteria"], max_chars=2000)
        target = st.date_input(
            "目標日", value=date.fromisoformat(goal.target_date) if goal.target_date else None
        )
        save = st.form_submit_button("変更を保存")
    if save and _action(
        lambda: goals.update_goal(
            goal.id,
            title=title,
            why=why,
            success_criteria=criteria,
            target_date=target.isoformat() if target else None,
        )
    ):
        st.rerun()
    if st.button(
        "達成にする" if goal.status != "done" else "進行中に戻す", key=f"goal_done_{goal.id}"
    ):
        if _action(
            lambda: goals.update_goal(goal.id, status="active" if goal.status == "done" else "done")
        ):
            st.rerun()
    if st.button(
        "アーカイブ解除" if goal.status == "archived" else "アーカイブ",
        key=f"goal_archive_{goal.id}",
    ):
        if _action(
            lambda: goals.update_goal(
                goal.id, status="active" if goal.status == "archived" else "archived"
            )
        ):
            st.rerun()


def _add_task(goal_id: int) -> None:
    """手動のタスク追加とゴールへの割当を表示する。"""
    with st.form(f"goal_task_{goal_id}", clear_on_submit=True):
        title = st.text_input("タスクのタイトル", max_chars=200)
        why = st.text_area("このタスクを行う理由", max_chars=2000)
        due = st.date_input("タスクの期限", value=None)
        save = st.form_submit_button("タスクを追加")
    if save and _action(
        lambda: goals.create_goal_task(goal_id, title, why, due.isoformat() if due else None)
    ):
        st.rerun()


def _apply_breakdown(
    goal_id: int, proposal: goals.GoalBreakdown, subgoals: List[int], tasks: List[int]
) -> None:
    """次の描画より前に確認済み提案を保存して確認状態を破棄する。"""
    if _action(partial(goals.apply_goal_breakdown, goal_id, proposal, subgoals, tasks)):
        st.session_state.pop(f"goal_breakdown_{goal_id}", None)


def _apply_assignments(proposals: List[goals.TaskGoalProposal]) -> None:
    """再描画前に確認済み割当だけを適用する。"""
    if _action(partial(goals.apply_task_goal_assignments, proposals)):
        st.session_state.pop("goal_assignment_proposals", None)


def _clear_selection(prefix: str) -> None:
    """再提案時に古いチェックを新しい内容へ引き継がない。"""
    for key in list(st.session_state):
        if isinstance(key, str) and key.startswith(prefix):
            del st.session_state[key]


def _breakdown(goal_id: int) -> None:
    """AI の提案を明示的にチェックしてから保存する。"""
    key = f"goal_breakdown_{goal_id}"
    if st.button("AIで今日のToDoまで分解", key=f"goal_ai_{goal_id}"):
        try:
            with st.spinner("成功の定義から最初の一歩を考えています"):
                st.session_state[key] = goals.propose_goal_breakdown(goal_id)
                _clear_selection(f"{key}_task_")
                _clear_selection(f"{key}_sub_")
        except (llm.LLMUnavailableError, ValueError) as error:
            st.warning(escape_markdown(str(error)))
    proposal = st.session_state.get(key)
    if not isinstance(proposal, goals.GoalBreakdown):
        return
    selected_goals: List[int] = []
    selected_tasks: List[int] = []
    st.caption("確認した提案だけチェックしてください。未選択のものは保存されません。")
    for index, item in enumerate(proposal.subgoals):
        if st.checkbox(
            escape_markdown(f"子ゴール: {item.title} — {item.why}"), key=f"{key}_sub_{index}"
        ):
            selected_goals.append(index)
    for index, item in enumerate(proposal.tasks):
        if st.checkbox(
            escape_markdown(
                f"{item.title} / {item.due_hint or '期限なし'} / {item.estimate_minutes or '未定'}分 — {item.why}"
            ),
            key=f"{key}_task_{index}",
        ):
            selected_tasks.append(index)
    st.button(
        "選んだ提案を追加",
        key=f"{key}_apply",
        disabled=not (selected_goals or selected_tasks),
        on_click=_apply_breakdown,
        args=(goal_id, proposal, selected_goals, selected_tasks),
    )


def _unclassified(all_goals: List[goals.GoalProgress]) -> None:
    """未分類タスクの手動割当と AI の一括提案を表示する。"""
    st.subheader("未分類タスク")
    tasks = goals.list_unclassified_tasks()
    active = {goal.id: goal for goal in all_goals if goal.status == "active"}
    if not tasks:
        st.caption("未分類の未完了タスクはありません。")
        return
    for task in tasks:
        st.write(escape_markdown(task["tags"] or task["raw_text"]))
        chosen = st.selectbox(
            "割当先のゴール",
            [None, *active],
            format_func=lambda value: (
                "選択してください" if value is None else escape_markdown(active[value].title)
            ),
            key=f"task_goal_{task['id']}",
        )
        if st.button("割り当てる", key=f"assign_goal_{task['id']}", disabled=chosen is None):
            if _action(partial(goals.set_task_goal, task["id"], chosen)):
                st.rerun()
    key = "goal_assignment_proposals"
    if st.button("AIでまとめて割当先を提案", disabled=not active):
        try:
            with st.spinner("割当先を考えています"):
                st.session_state[key] = goals.propose_task_goal_assignments()
                _clear_selection("assign_proposal_")
        except (llm.LLMUnavailableError, ValueError) as error:
            st.warning(escape_markdown(str(error)))
    titles = {task["id"]: task["tags"] or task["raw_text"] for task in tasks}
    selected: List[goals.TaskGoalProposal] = []
    for index, proposal in enumerate(st.session_state.get(key, [])):
        if proposal.task_id not in titles or proposal.goal_id not in active:
            continue
        label = f"{titles[proposal.task_id]} → {active[proposal.goal_id].title}: {proposal.why}"
        if st.checkbox(
            escape_markdown(label),
            key=f"assign_proposal_{index}_{proposal.task_id}_{proposal.goal_id}",
        ):
            selected.append(proposal)
    st.button(
        "確認した割当を適用", disabled=not selected, on_click=_apply_assignments, args=(selected,)
    )


def render_goals_page() -> None:
    """階層、進捗、次の一手と放置状態を一つの画面で表示する。"""
    with st.expander("＋ 新しいゴール", expanded=not goals.list_goals_with_progress()):
        _goal_form()
    archived = st.checkbox("アーカイブも表示", key="show_archived_goals")
    all_goals = goals.list_goals_with_progress(include_archived=archived)
    if not all_goals:
        st.info("まずは達成したいゴールと、成功の定義を1文で書いてみましょう。")
    for goal in all_goals:
        with st.container(border=True):
            st.markdown(
                f"{'　' * min(goal.depth, 12)}{'└ ' if goal.depth else ''}**{escape_markdown(goal.title)}**"
            )
            st.caption(
                escape_markdown(
                    f"状態: {goal.status} / 目標日: {goal.target_date or '未設定'} / なぜ: {goal.why}"
                )
            )
            st.progress(
                goal.progress,
                text=f"完了 {goal.done_count} / {goal.done_count + goal.open_count} 件（{goal.progress:.0%}）",
            )
            if (
                goal.status == "active"
                and goal.open_count
                and goal.stale_days is not None
                and goal.stale_days >= 3
            ):
                st.warning(f"放置 {goal.stale_days} 日 — 次の一手を見直しましょう。", icon=None)
            if goal.next_task:
                st.write(
                    "次の一手: "
                    + escape_markdown(goal.next_task["tags"] or goal.next_task["raw_text"])
                )
            else:
                st.caption("次の一手はまだありません。")
            with st.expander("タスクを追加"):
                _add_task(goal.id)
            with st.expander("子ゴールを追加"):
                _goal_form(goal.id)
            with st.expander("編集・達成・アーカイブ"):
                _edit_goal(goal)
            _breakdown(goal.id)
    _unclassified(all_goals)
