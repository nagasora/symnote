"""資料やテーマから手動でも育てられるアイデア整理画面。"""

from __future__ import annotations

import datetime as dt
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple, cast
from uuid import uuid4

import streamlit as st
import streamlit.components.v1 as components

from symnote.core import mindmap, nlp
from symnote.core.db import (
    create_idea_session,
    delete_idea_session,
    fetch_idea_sessions,
    get_idea_session,
)
from symnote.core.doc_loader import extract_text_from_file
from symnote.core.mindmap import MindmapNode, NodeKind
from symnote.views.text import escape_markdown

_MINDMAP_CANVAS = components.declare_component(
    "mindmap_canvas",
    path=str(Path(__file__).resolve().parents[1] / "components" / "mindmap_canvas"),
)

EXPANSION_MODES: Dict[str, Tuple[str, NodeKind]] = {
    "related_ideas": ("関連アイデア", "idea"),
    "related_tasks": ("関連タスク", "task"),
    "detail_steps": ("具体的な手順", "detail"),
    "questions": ("問いを立てる", "note"),
    "perspectives": ("別の視点", "note"),
    "combine": ("組み合わせる", "idea"),
}
NODE_KIND_LABELS: Dict[NodeKind, str] = {
    "root": "テーマ",
    "idea": "アイデア",
    "task": "タスク",
    "detail": "手順",
    "note": "メモ・問い",
    "link": "リンク",
}


def _reset_current_session() -> None:
    """現在開いているセッションの表示状態だけを解除する。"""
    for key in (
        "current_session_id",
        "idea_source_text",
        "idea_project_title",
        "idea_summary",
        "idea_generated_items",
        "idea_chat_history",
        "idea_creation_error",
        "idea_creation_result",
        "mindmap_action_result",
    ):
        st.session_state.pop(key, None)


def _set_current_session(session: Dict[str, Any]) -> None:
    """保存済みセッションを表示状態に反映する。"""
    _reset_current_session()
    st.session_state.update(
        {
            "current_session_id": session["id"],
            "idea_project_title": session["title"],
            "idea_summary": session.get("summary") or "",
            "idea_source_text": session.get("source_text") or "",
        }
    )


def _run_local_action(action: Callable[[], Any], message: str) -> bool:
    """ローカル操作の失敗を画面に通知し、成功時だけ結果を保存する。"""
    try:
        action()
    except Exception as exc:
        st.error(f"保存できませんでした: {escape_markdown(exc)}")
        return False
    st.session_state["mindmap_action_result"] = message
    return True


def _finish_mutation(project_id: int) -> None:
    """マップ変更後に古いアクションプランを破棄して再表示する。"""
    st.session_state.pop(f"idea_action_plan_{project_id}", None)
    st.rerun()


def _render_session_picker() -> None:
    """保存済みセッションの選択と確認付き削除を表示する。"""
    with st.expander("保存済みのアイデア整理", expanded=False):
        if st.button("新しく始める", key="new_session_btn"):
            _reset_current_session()
            st.rerun()
        for session in fetch_idea_sessions():
            session_id = session["id"]
            open_col, delete_col = st.columns([4, 1])
            with open_col:
                if st.button(escape_markdown(session["title"]), key=f"load_session_{session_id}"):
                    full_session = get_idea_session(session_id)
                    if full_session:
                        _set_current_session(full_session)
                        st.rerun()
                    else:
                        st.warning("セッションが見つかりません。")
            confirm_key = f"confirm_del_session_{session_id}"
            with delete_col:
                if st.button("削除", key=f"del_session_{session_id}"):
                    st.session_state[confirm_key] = True
            if st.session_state.get(confirm_key):
                st.warning("セッションとマップを削除します。登録済みタスク・メモは残ります。")
                yes_col, no_col = st.columns(2)
                with yes_col:
                    if st.button(
                        "削除を確定", key=f"yes_del_session_{session_id}"
                    ) and _run_local_action(
                        partial(delete_idea_session, session_id), "セッションを削除しました。"
                    ):
                        st.session_state.pop(confirm_key, None)
                        if st.session_state.get("current_session_id") == session_id:
                            _reset_current_session()
                        st.session_state.pop(f"idea_action_plan_{session_id}", None)
                        st.rerun()
                with no_col:
                    if st.button("キャンセル", key=f"no_del_session_{session_id}"):
                        st.session_state.pop(confirm_key, None)
                        st.rerun()


def _start_session(theme: str, source_text: str, use_ai: bool) -> None:
    """先にセッションを保存し、AI障害時にも手動用ルートを確保する。"""
    title = theme.strip() or next(
        (line.strip()[:200] for line in source_text.splitlines() if line.strip()),
        "新しいアイデア",
    )
    try:
        session_id = create_idea_session(title, "", source_text)
    except Exception as exc:
        st.error(f"セッションを保存できませんでした: {escape_markdown(exc)}")
        return
    _set_current_session({"id": session_id, "title": title, "source_text": source_text})
    if use_ai:
        try:
            with st.spinner("AIが最初のアイデアを整理しています…"):
                tree = nlp.generate_initial_mindmap(source_text, theme=theme.strip())
                mindmap.save_mindmap_tree(session_id, tree)
        except Exception as exc:
            st.session_state["idea_creation_error"] = (
                f"AI生成を完了できませんでした。手動で追加・編集できます: {exc}"
            )
        else:
            st.session_state["idea_creation_result"] = "最初のマップを保存しました。"
            st.rerun()
            return
    try:
        mindmap.create_node(project_id=session_id, kind="root", title=title)
    except Exception as exc:
        st.session_state["idea_creation_error"] = (
            f"セッションは保存済みです。マップの作成を再試行してください: {exc}"
        )
    st.rerun()


def _render_start_panel() -> None:
    """テーマのみ・資料・貼り付けの入力と手動開始を表示する。"""
    theme = st.text_input(
        "考えたいテーマ（資料なしでも始められます）", key="idea_theme", max_chars=200
    )
    with st.expander("資料やテキストを追加", expanded=False):
        uploaded = st.file_uploader(
            "資料をアップロード", type=["pdf", "docx", "txt", "md"], key="idea_uploader"
        )
        pasted = st.text_area("テキストを貼り付け", height=160, key="idea_text_input")
    ai_col, manual_col = st.columns(2)
    with ai_col:
        use_ai = st.button("AIで整理を始める", type="primary", key="idea_start_ai")
    with manual_col:
        manual = st.button("手動で始める", key="idea_start_manual")
    if not (use_ai or manual):
        return
    texts: List[str] = [pasted.strip()] if pasted.strip() else []
    if uploaded is not None:
        try:
            extracted = extract_text_from_file(
                uploaded.getvalue(), uploaded.name.rsplit(".", 1)[-1].lower()
            )
        except Exception as exc:
            st.error(
                "資料を読み込めませんでした。資料を外すとテーマだけで始められます: "
                f"{escape_markdown(exc)}"
            )
            return
        if extracted.strip():
            texts.insert(0, extracted.strip())
    source_text = "\n\n".join(texts)
    if not theme.strip() and not source_text:
        st.warning("テーマを入力するか、資料・テキストを追加してください。")
        return
    _start_session(theme, source_text, use_ai=use_ai)


def _render_empty_map(project_id: int) -> None:
    """未作成マップをAIまたは手動で再作成する。"""
    st.info("マップがまだありません。AIを使わずに作成することもできます。")
    title = st.session_state.get("idea_project_title", "新しいアイデア")
    ai_col, manual_col = st.columns(2)
    with ai_col:
        retry = st.button("AIでマップを生成", key="retry_mindmap_generation")
    with manual_col:
        manual = st.button("手動でマップを作成", key="init_mindmap_btn")
    if retry:
        try:
            with st.spinner("AIがマップを生成しています…"):
                tree = nlp.generate_initial_mindmap(
                    st.session_state.get("idea_source_text", ""), theme=title
                )
                mindmap.save_mindmap_tree(project_id, tree)
        except Exception as exc:
            st.error(f"AI生成に失敗しました。手動で作成できます: {escape_markdown(exc)}")
        else:
            _finish_mutation(project_id)
    if manual and _run_local_action(
        lambda: mindmap.create_node(project_id=project_id, kind="root", title=title),
        "手動編集用のマップを作成しました。",
    ):
        _finish_mutation(project_id)


def _render_initial_branches(root: MindmapNode) -> None:
    """既存ルートを保持したまま、資料から最初の枝のAI生成を再試行する。"""
    if not st.button("AIで最初の枝を生成", key=f"generate_initial_branches_{root.id}"):
        return
    try:
        with st.spinner("資料とテーマから最初の枝を生成しています…"):
            tree = nlp.generate_initial_mindmap(
                st.session_state.get("idea_source_text", ""), theme=root.title
            )
            inserted_ids = mindmap.append_initial_mindmap_children(root.id, tree)
    except Exception as exc:
        st.error(f"最初の枝を生成できませんでした。手動編集は続けられます: {escape_markdown(exc)}")
        return
    st.session_state.setdefault(f"collapsed_mindmap_node_ids_{root.project_id}", set()).discard(
        root.id
    )
    st.session_state["mindmap_action_result"] = (
        f"既存のテーマを保持して、{len(inserted_ids)} 個のノードを追加しました。"
    )
    _finish_mutation(root.project_id)


def render_idea_tab() -> None:
    """アイデア整理の開始・継続・ノード操作・出力を提供する。"""
    st.caption("テーマだけでも、資料からでも。AIで広げ、手動で直して、実行につなげます。")
    _render_session_picker()
    if error := st.session_state.pop("idea_creation_error", None):
        st.warning(escape_markdown(error))
    if message := st.session_state.pop("idea_creation_result", None):
        st.success(escape_markdown(message))
    if "current_session_id" not in st.session_state:
        _render_start_panel()
        return
    project_id = st.session_state["current_session_id"]
    st.write(escape_markdown(st.session_state.get("idea_project_title", "")))
    source_text = st.session_state.get("idea_source_text", "")
    if source_text:
        with st.expander("元の資料テキスト"):
            st.text(source_text)
    nodes = mindmap.get_nodes_by_project(project_id)
    if not nodes:
        _render_empty_map(project_id)
        return
    if len(nodes) == 1 and nodes[0].kind == "root" and not nodes[0].children:
        _render_initial_branches(nodes[0])
    _render_mindmap_workspace(nodes)
    st.divider()
    st.download_button(
        "Markdownでダウンロード",
        data=mindmap.export_markdown(nodes),
        file_name=f"ideas-{project_id}.md",
        mime="text/markdown",
        key=f"idea_export_{project_id}",
    )
    _render_action_plan(nodes)


def _run_node_expansion(
    node: MindmapNode,
    mode: str,
    expected_kind: NodeKind,
    progress_label: str,
    nodes: List[MindmapNode] | None = None,
) -> None:
    """経路・兄弟・既存の子を渡してAI展開し、成功時のみマップを更新する。"""
    try:
        if mode not in EXPANSION_MODES:
            raise ValueError("未対応の展開モードです。")
        with st.spinner(progress_label):
            context = mindmap.get_expansion_context(
                nodes if nodes is not None else mindmap.get_nodes_by_project(node.project_id),
                node.id,
            )
            new_items = nlp.expand_node(
                node.title,
                node.body,
                st.session_state.get("idea_summary", ""),
                mode,
                **context,
            )
            inserted_ids = mindmap.save_node_expansion(node.id, new_items, expected_kind)
    except Exception as exc:
        st.error(f"AI生成に失敗しました。手動で子ノードを追加できます: {escape_markdown(exc)}")
        return
    st.session_state.setdefault(f"collapsed_mindmap_node_ids_{node.project_id}", set()).discard(
        node.id
    )
    st.session_state["mindmap_action_result"] = f"{len(inserted_ids)} 個のノードを追加しました。"
    _finish_mutation(node.project_id)


def _render_node_editor(node: MindmapNode) -> None:
    """選択ノードの手動編集と子ノード追加フォームを表示する。"""
    with st.expander("編集・子ノードの追加"):
        with st.form(f"edit_mindmap_node_{node.id}"):
            title = st.text_input("タイトル", value=node.title, max_chars=200)
            body = st.text_area("内容", value=node.body, max_chars=4000)
            save = st.form_submit_button("変更を保存")
        if save and _run_local_action(
            lambda: mindmap.update_node(node.id, title=title.strip(), body=body),
            "ノードを更新しました。",
        ):
            _finish_mutation(node.project_id)
        with st.form(f"add_child_{node.id}", clear_on_submit=False):
            child_title = st.text_input("子ノードのタイトル", max_chars=200)
            child_body = st.text_area("子ノードの内容", max_chars=4000)
            kind = st.selectbox(
                "種類",
                options=["idea", "task", "detail", "note", "link"],
                format_func=lambda value: NODE_KIND_LABELS[cast(NodeKind, value)],
            )
            add = st.form_submit_button("子ノードを追加")
        if add and _run_local_action(
            lambda: mindmap.create_node(
                project_id=node.project_id,
                parent_id=node.id,
                kind=cast(NodeKind, kind),
                title=child_title.strip(),
                body=child_body,
            ),
            "子ノードを追加しました。",
        ):
            st.session_state.setdefault(
                f"collapsed_mindmap_node_ids_{node.project_id}", set()
            ).discard(node.id)
            _finish_mutation(node.project_id)


def _render_node_expansion_controls(
    node: MindmapNode, nodes: List[MindmapNode] | None = None
) -> None:
    """既存の展開モードを選択し、AI生成処理を実行する。"""
    mode = st.selectbox(
        "AIで広げる方法",
        options=list(EXPANSION_MODES),
        format_func=lambda value: EXPANSION_MODES[value][0],
        key=f"expansion_mode_{node.id}",
    )
    label, kind = EXPANSION_MODES[mode]
    if st.button("選んだ方法で広げる", key=f"expand_node_{node.id}"):
        _run_node_expansion(node, mode, kind, f"{label}を生成しています…", nodes)


def _render_add_child_panel(
    node: MindmapNode, nodes: List[MindmapNode] | None = None
) -> None:
    """選択ノードへの子追加をAI生成またはキー不要の手入力で行う。"""
    st.markdown("##### 子ノードを追加")
    method = st.radio(
        "追加方法",
        ["🤖 AI に考えてもらう", "✏️ 自分で入力する"],
        key=f"add_child_method_{node.id}",
    )
    if method == "🤖 AI に考えてもらう":
        st.caption("AIが使えない場合は「✏️ 自分で入力する」を選んで続けられます。")
        _render_node_expansion_controls(node, nodes)
        return
    with st.form(f"quick_add_child_{node.id}"):
        title = st.text_input("追加する子ノードのタイトル（必須）", max_chars=200)
        as_task = st.checkbox(
            "タスクにする",
            help="タスク種別のノードを作成します。タスク一覧への登録はノード選択後の「タスクに追加」で行えます。",
        )
        save = st.form_submit_button("子ノードを保存")
    if not save:
        return
    if not title.strip():
        st.warning("子ノードのタイトルを入力してください。")
        return
    if _run_local_action(
        lambda: mindmap.create_node(
            project_id=node.project_id,
            parent_id=node.id,
            kind="task" if as_task else "idea",
            title=title.strip(),
        ),
        "子ノードを追加しました。",
    ):
        st.session_state.setdefault(
            f"collapsed_mindmap_node_ids_{node.project_id}", set()
        ).discard(node.id)
        st.session_state.pop(f"add_child_mindmap_node_id_{node.project_id}", None)
        _finish_mutation(node.project_id)


def _render_node_actions(node: MindmapNode, nodes: List[MindmapNode] | None = None) -> None:
    """AI展開・編集・期限付きタスク化・メモ化・確認付き削除を表示する。"""
    if st.session_state.get(f"add_child_mindmap_node_id_{node.project_id}") == node.id:
        _render_add_child_panel(node, nodes)
    else:
        _render_node_expansion_controls(node, nodes)
    _render_node_editor(node)
    if st.button("選択ノードをゴールにする", key=f"node_goal_{node.id}"):
        from symnote.core.goals import create_goal

        _run_local_action(
            lambda: create_goal(title=node.title, why=node.body or ""),
            "ゴールを作成しました。ゴールツリーで成功の定義を設定できます。",
        )
    with st.expander("タスク・メモとして保存"):
        if node.linked_item_id:
            st.caption(f"タスクまたはメモに保存済み（ID: {node.linked_item_id}）")
        else:
            with st.form(f"node_task_{node.id}"):
                has_due = st.checkbox("期限を設定する")
                due = st.date_input("期限日", value=dt.date.today())
                task = st.form_submit_button("タスクに追加")
            if task and _run_local_action(
                lambda: mindmap.ensure_task_item_for_node(
                    node, due_date=due.isoformat() if has_due else None
                ),
                "タスクに追加しました。",
            ):
                _finish_mutation(node.project_id)
            if st.button("メモに追加", key=f"sync_memo_{node.id}") and _run_local_action(
                lambda: mindmap.ensure_memo_item_for_node(node), "メモに追加しました。"
            ):
                _finish_mutation(node.project_id)
    confirm_key = f"confirm_delete_mindmap_node_{node.id}"
    if st.button("ノードを削除", key=f"delete_node_{node.id}"):
        st.session_state[confirm_key] = True
    if st.session_state.get(confirm_key):
        st.warning("このノードと配下を削除します。登録済みのタスク・メモ本体は削除しません。")
        yes_col, no_col = st.columns(2)
        with yes_col:
            if st.button("削除を確定", key=f"confirm_delete_node_{node.id}") and _run_local_action(
                lambda: mindmap.delete_node_and_descendants(node.id), "ノードを削除しました。"
            ):
                st.session_state.pop(confirm_key, None)
                _finish_mutation(node.project_id)
        with no_col:
            if st.button("キャンセル", key=f"cancel_delete_node_{node.id}"):
                st.session_state.pop(confirm_key, None)
                st.rerun()


def _render_action_plan(nodes: List[MindmapNode]) -> None:
    """全体から生成したアクションを選択し、明示的にタスクとして保存する。"""
    project_id = nodes[0].project_id
    plan_key = f"idea_action_plan_{project_id}"
    st.markdown("### 全体のアクションプラン")
    st.caption("生成しただけではタスクは増えません。必要なものを選んで追加してください。")
    if st.button("全体からプランを生成", key=f"generate_plan_{project_id}"):
        try:
            with st.spinner("マップ全体から次の行動を整理しています…"):
                actions = nlp.generate_action_plan(nodes)
            if not isinstance(actions, list) or any(
                not isinstance(action, dict)
                or not isinstance(action.get("title"), str)
                or not action["title"].strip()
                for action in actions
            ):
                raise ValueError("アクションプランの形式が不正です。")
        except Exception as exc:
            st.error(
                "プラン生成に失敗しました。ノードから手動でタスクを追加できます: "
                f"{escape_markdown(exc)}"
            )
        else:
            st.session_state[plan_key] = {"actions": actions, "version": uuid4().hex}
    plan = st.session_state.get(plan_key)
    if not plan:
        return
    actions = plan["actions"]
    if not actions:
        st.info("追加できるアクションはありません。マップを編集して再生成できます。")
        return
    selected: List[Dict[str, Any]] = []
    selected_indices: set[int] = set()
    for index, action in enumerate(actions):
        if st.checkbox(
            escape_markdown(action["title"]),
            key=f"plan_pick_{project_id}_{plan['version']}_{index}",
        ):
            selected.append(action)
            selected_indices.add(index)
        if hint := action.get("due_hint"):
            st.caption(f"期限の目安: {escape_markdown(hint)}")
        if reason := action.get("reason"):
            st.write(escape_markdown(reason))
    if st.button(
        "選択した行動をタスクに追加", key=f"save_plan_{project_id}", disabled=not selected
    ):
        try:
            ids = mindmap.create_tasks_from_action_plan(project_id, selected)
        except Exception as exc:
            st.error(f"タスクを保存できませんでした: {escape_markdown(exc)}")
            return
        st.session_state[plan_key] = {
            "actions": [
                action for index, action in enumerate(actions) if index not in selected_indices
            ],
            "version": uuid4().hex,
        }
        st.session_state["mindmap_action_result"] = f"{len(ids)} 件のタスクを追加しました。"
        st.rerun()


def _mindmap_nodes_with_positions(
    nodes: List[MindmapNode], collapsed_node_ids: set[int] | None = None
) -> Tuple[List[MindmapNode], Dict[int, Tuple[int, int]]]:
    """折り畳みを反映し、親を子の中央に配置する座標を計算する。"""
    ordered: List[MindmapNode] = []
    positions: Dict[int, Tuple[int, int]] = {}
    collapsed_node_ids = collapsed_node_ids or set()
    leaf_index = 0

    def visit(node: MindmapNode, layout_depth: int) -> int:
        """子孫を順に配置して親の縦座標を返す。"""
        nonlocal leaf_index
        ordered.append(node)
        if not node.children or node.id in collapsed_node_ids:
            y = 58 + leaf_index * 82
            leaf_index += 1
        else:
            child_ys = [visit(child, layout_depth + 1) for child in node.children]
            y = sum(child_ys) // len(child_ys)
        positions[node.id] = (42 + layout_depth * 220, y)
        return y

    for root in nodes:
        visit(root, 0)
    return ordered, positions


def _render_interactive_mindmap_canvas(
    nodes: List[MindmapNode], collapsed_node_ids: set[int], selected_node_id: int | None
) -> Tuple[List[MindmapNode], Dict[str, Any] | None]:
    """既存コンポーネントAPIでキャンバスを表示し、最後のイベントを返す。"""
    ordered, positions = _mindmap_nodes_with_positions(nodes, collapsed_node_ids)
    if not ordered:
        return ordered, None
    scale = max(0.55, min(1.0, 1.0 - max(0, len(ordered) - 9) * 0.028))
    visible_ids = {node.id for node in ordered}
    visible_by_id = {node.id: node for node in ordered}
    focus_ids: set[int] = set()

    def collect_descendants(node: MindmapNode) -> None:
        """表示中の子孫をフォーカス対象へ追加する。"""
        for child in node.children:
            if child.id in visible_ids:
                focus_ids.add(child.id)
                collect_descendants(child)

    if selected_node_id in visible_by_id:
        collect_descendants(visible_by_id[selected_node_id])

    payload = []
    for node in ordered:
        x, y = positions[node.id]
        payload.append(
            {
                "id": node.id,
                "title": node.title,
                "body": node.body,
                "kind": node.kind,
                "x": int(x * scale),
                "y": int(y * scale),
                "children": [child.id for child in node.children if child.id in visible_ids],
                "has_children": bool(node.children),
                "collapsed": node.id in collapsed_node_ids,
            }
        )
    width = int((max(x for x, _ in positions.values()) + 230) * scale)
    height = int((max(y for _, y in positions.values()) + 62) * scale)
    event = _MINDMAP_CANVAS(
        nodes=payload,
        selected_id=selected_node_id,
        focus_ids=sorted(focus_ids),
        scale=scale,
        width=width,
        height=height,
        key=f"interactive_mindmap_canvas_{nodes[0].project_id}",
        default=None,
    )
    return ordered, event


def _parse_mindmap_event(event: Any, valid_node_ids: set[int]) -> Tuple[str, int, str] | None:
    """ブラウザーから受信したイベントの種類・ノードID・識別子を検証する。"""
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if not isinstance(event_type, str) or event_type not in {"select", "toggle", "add_child"}:
        return None
    raw_node_id = event.get("node_id")
    if isinstance(raw_node_id, bool) or not isinstance(raw_node_id, (int, str)):
        return None
    try:
        node_id = int(raw_node_id)
    except ValueError:
        return None
    event_id = event.get("event_id")
    if node_id not in valid_node_ids or not isinstance(event_id, str) or not event_id:
        return None
    return event_type, node_id, event_id


def _render_mindmap_workspace(nodes: List[MindmapNode]) -> None:
    """折り畳み・選択状態を保持し、キャンバスとノード操作を表示する。"""
    st.caption(
        "マインドマップの使い方: ＋で枝を追加（AIか手入力）・−で畳む・"
        "折り畳みボタンの＋で再展開・ノードを選ぶと編集/タスク化"
    )
    project_id = nodes[0].project_id
    collapsed_key = f"collapsed_mindmap_node_ids_{project_id}"
    collapsed_node_ids = st.session_state.setdefault(collapsed_key, set())
    selected_key = f"selected_mindmap_node_id_{project_id}"
    all_node_ids: set[int] = set()

    def collect_ids(node: MindmapNode) -> None:
        """非表示の子孫も含めて有効なノードIDを収集する。"""
        all_node_ids.add(node.id)
        for child in node.children:
            collect_ids(child)

    for root in nodes:
        collect_ids(root)

    if st.session_state.get(selected_key) not in all_node_ids:
        st.session_state[selected_key] = nodes[0].id
    flat_nodes, event = _render_interactive_mindmap_canvas(
        nodes, collapsed_node_ids, st.session_state[selected_key]
    )
    parsed_event = _parse_mindmap_event(event, all_node_ids)
    last_event_key = f"last_mindmap_component_event_{project_id}"
    if parsed_event is not None:
        event_type, event_node_id, event_id = parsed_event
        event_signature = f"{event_type}:{event_node_id}:{event_id}"
    else:
        event_signature = None
    if parsed_event is not None and event_signature != st.session_state.get(last_event_key):
        st.session_state[last_event_key] = event_signature
        clicked_node_id = event_node_id
        st.session_state[selected_key] = clicked_node_id
        add_child_key = f"add_child_mindmap_node_id_{project_id}"
        if event_type == "add_child":
            st.session_state[add_child_key] = clicked_node_id
        else:
            st.session_state.pop(add_child_key, None)
        if event_type == "toggle":
            if clicked_node_id in collapsed_node_ids:
                collapsed_node_ids.remove(clicked_node_id)
            else:
                collapsed_node_ids.add(clicked_node_id)
        st.rerun()
    node_by_id = {node.id: node for node in flat_nodes}
    if st.session_state[selected_key] not in node_by_id:
        st.session_state[selected_key] = flat_nodes[0].id

    node = node_by_id[st.session_state[selected_key]]
    st.markdown(f"#### {escape_markdown(node.title)}")
    st.caption(f"{node.kind.upper()} · レベル {node.depth + 1}")
    if node.body:
        st.write(escape_markdown(node.body))
    st.caption("削除すると、選択したノードとその配下をまとめて削除します。")
    if message := st.session_state.pop("mindmap_action_result", None):
        st.success(escape_markdown(message))
    _render_node_actions(node, nodes)
