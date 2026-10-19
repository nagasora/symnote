"""一時DB上でアイデア画面の手動操作とAI障害時の継続性を保証する。"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

with patch("dotenv.load_dotenv", return_value=False):
    from symnote.core import db, mindmap, nlp
    from symnote.views import ideas_view


APP_SOURCE = """
from symnote.views.ideas_view import render_idea_tab
render_idea_tab()
"""


def _deny_network(*args: Any, **kwargs: Any) -> Any:
    """外部通信や未指定の生成呼び出しを失敗させる。"""
    raise AssertionError("テストでネットワーク・実LLMを使用してはいけません。")


def _canvas_event(**kwargs: Any) -> Any:
    """テストから注入した単発イベントのみをキャンバス応答として返す。"""
    return ideas_view.st.session_state.pop("test_canvas_event", None)


@pytest.fixture(autouse=True)
def isolated_ui(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """すべてのDB操作を一時ファイルに限定し実キーと環境ファイルを使わない。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "ideas-ui.db"))
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(nlp.llm, "generate_json", _deny_network)
    monkeypatch.setattr(ideas_view, "_MINDMAP_CANVAS", _canvas_event)
    db.init_db()


def _app() -> AppTest:
    """本体アプリを読み込まず対象画面だけを起動する。"""
    app = AppTest.from_string(APP_SOURCE, default_timeout=10).run()
    assert not app.exception
    return app


def _button(app: AppTest, label: str) -> Any:
    """表示名が一致するボタンを取得する。"""
    return next(button for button in app.button if button.label == label)


def _input(app: AppTest, label: str) -> Any:
    """表示名が一致する入力欄を取得する。"""
    return next(field for field in app.text_input if field.label == label)


def _textarea(app: AppTest, label: str) -> Any:
    """表示名が一致する複数行入力欄を取得する。"""
    return next(field for field in app.text_area if field.label == label)


def _start_manual(app: AppTest, theme: str = "暮らしの改善") -> Tuple[int, int]:
    """テーマを入力して手動開始し、セッションとルートのIDを返す。"""
    app.text_input(key="idea_theme").set_value(theme)
    app.button(key="idea_start_manual").click().run()
    assert not app.exception
    project_id = int(app.session_state["current_session_id"])
    return project_id, mindmap.get_nodes_by_project(project_id)[0].id


def _tree() -> List[mindmap.MindmapNode]:
    """二つの枝と片側に孫を持つレイアウト検証用の木を返す。"""
    leaf = mindmap.MindmapNode(4, 1, "note", "問い", "", parent_id=2, depth=2)
    left = mindmap.MindmapNode(2, 1, "idea", "左", "", parent_id=1, depth=1,
                               children=[leaf])
    right = mindmap.MindmapNode(3, 1, "task", "右", "", parent_id=1, depth=1)
    return [mindmap.MindmapNode(1, 1, "root", "全体", "", children=[left, right])]


def test_layout_places_children_by_depth_and_centers_parent() -> None:
    """深さごとの横位置と葉の間隔を固定し親を子の縦座標中央に置く。"""
    nodes = _tree()
    ordered, positions = ideas_view._mindmap_nodes_with_positions(nodes)
    assert [node.id for node in ordered] == [1, 2, 4, 3]
    assert positions == {1: (42, 99), 2: (262, 58), 4: (482, 58), 3: (262, 140)}
    assert len(nodes[0].children[0].children) == 1


def test_collapsed_layout_hides_descendants_without_mutating_tree() -> None:
    """折り畳んだ枝を葉として配置し、元の木から子孫を削除しない。"""
    nodes = _tree()
    ordered, positions = ideas_view._mindmap_nodes_with_positions(nodes, {2})
    assert [node.id for node in ordered] == [1, 2, 3]
    assert set(positions) == {1, 2, 3}
    assert nodes[0].children[0].children[0].id == 4
    folded, folded_positions = ideas_view._mindmap_nodes_with_positions(nodes, {1})
    assert [node.id for node in folded] == [1]
    assert folded_positions == {1: (42, 58)}
    assert ideas_view._mindmap_nodes_with_positions([]) == ([], {})


@pytest.mark.parametrize("event_type", ["select", "toggle", "add_child"])
@pytest.mark.parametrize("node_id", [2, "2"])
def test_event_parser_accepts_only_supported_valid_node_events(
    event_type: str, node_id: Any,
) -> None:
    """有効なノードの選択・折り畳みイベントは整数IDへ正規化する。"""
    assert ideas_view._parse_mindmap_event(
        {"type": event_type, "node_id": node_id, "event_id": "nonce"}, {1, 2}
    ) == (event_type, 2, "nonce")


@pytest.mark.parametrize(
    "event",
    [None, [], "select", {}, {"type": []},
     {"type": "delete", "node_id": 1, "event_id": "x"},
     {"type": "select", "node_id": True, "event_id": "x"},
     {"type": "select", "node_id": 1.0, "event_id": "x"},
     {"type": "select", "node_id": "invalid", "event_id": "x"},
     {"type": "select", "node_id": 999, "event_id": "x"},
     {"type": "select", "node_id": 1, "event_id": ""},
     {"type": "select", "node_id": 1, "event_id": 1}],
)
def test_event_parser_ignores_malformed_or_stale_events(event: Any) -> None:
    """不正な型・未知操作・削除済みノード・識別子欠落を安全に無視する。"""
    assert ideas_view._parse_mindmap_event(event, {1, 2}) is None


def test_blank_start_does_not_create_session() -> None:
    """入力が空白だけなら手動開始でもセッションとノードを作成しない。"""
    app = _app()
    app.text_input(key="idea_theme").set_value(" \t ")
    app.button(key="idea_start_manual").click().run()
    assert not app.exception
    assert any("テーマを入力" in warning.value for warning in app.warning)
    assert db.fetch_idea_sessions() == []


def test_manual_start_edit_add_child_and_reopen_session() -> None:
    """キーなしで開始・編集・子追加・保存済みセッション再選択が完了する。"""
    app = _app()
    project_id, root_id = _start_manual(app, "  暮らしの改善  ")
    assert mindmap.get_nodes_by_project(project_id)[0].title == "暮らしの改善"
    _input(app, "タイトル").set_value("編集後のテーマ")
    _textarea(app, "内容").set_value("変更した説明")
    _button(app, "変更を保存").click().run()
    assert not app.exception
    saved = mindmap.get_nodes_by_project(project_id)[0]
    assert (saved.title, saved.body) == ("編集後のテーマ", "変更した説明")
    _input(app, "子ノードのタイトル").set_value("具体的な問い")
    _textarea(app, "子ノードの内容").set_value("誰の課題を解くか")
    next(field for field in app.selectbox if field.label == "種類").select("note")
    _button(app, "子ノードを追加").click().run()
    assert not app.exception
    children = mindmap.get_nodes_by_project(project_id)[0].children
    assert [(child.title, child.kind, child.body) for child in children] == [
        ("具体的な問い", "note", "誰の課題を解くか"),
    ]
    assert db.fetch_tasks() == []
    app.button(key="new_session_btn").click().run()
    assert app.text_input(key="idea_theme")
    app.button(key=f"load_session_{project_id}").click().run()
    assert not app.exception
    assert app.session_state["current_session_id"] == project_id
    assert app.session_state[f"selected_mindmap_node_id_{project_id}"] == root_id
    assert mindmap.get_nodes_by_project(project_id)[0].children[0].title == "具体的な問い"


def test_manual_source_only_start_uses_first_nonempty_line() -> None:
    """資料だけの手動開始では最初の非空行をタイトルにし原文を保存する。"""
    app = _app()
    source = "資料の表題\n\n具体的な背景"
    app.text_area(key="idea_text_input").set_value("\n" + source + "\n")
    app.button(key="idea_start_manual").click().run()
    assert not app.exception
    session = db.get_idea_session(app.session_state["current_session_id"])
    assert session is not None
    assert session["title"] == "資料の表題"
    assert session["source_text"] == source
    assert mindmap.get_nodes_by_project(session["id"])[0].title == "資料の表題"


def test_initial_ai_failure_preserves_session_and_allows_manual_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生成障害時もセッションと手動ルートが残り、子ノードを追加できる。"""
    def fail_generation(*args: Any, **kwargs: Any) -> Any:
        """ネットワークを使わず生成障害を再現する。"""
        raise RuntimeError("オフラインです")

    monkeypatch.setattr(nlp, "generate_initial_mindmap", fail_generation)
    app = _app()
    app.text_input(key="idea_theme").set_value("障害時のテーマ")
    app.button(key="idea_start_ai").click().run()
    assert not app.exception
    assert any("手動で追加・編集できます" in warning.value for warning in app.warning)
    project_id = int(app.session_state["current_session_id"])
    assert len(db.fetch_idea_sessions()) == 1
    assert len(mindmap.get_nodes_by_project(project_id)) == 1
    _input(app, "子ノードのタイトル").set_value("手動で続ける")
    _button(app, "子ノードを追加").click().run()
    assert not app.exception
    assert mindmap.get_nodes_by_project(project_id)[0].children[0].title == "手動で続ける"


def test_successful_initial_ai_generation_has_no_extra_manual_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI初期生成が成功したら生成木だけを保存し手動ルートを重ねない。"""
    calls: List[Tuple[str, str]] = []

    def generate(source_text: str = "", *, theme: str = "") -> Dict[str, Any]:
        """入力を記録して固定の初期マップを返す。"""
        calls.append((source_text, theme))
        return {"kind": "root", "title": "生成テーマ", "children": [
            {"kind": "idea", "title": "生成された枝"},
        ]}

    monkeypatch.setattr(nlp, "generate_initial_mindmap", generate)
    app = _app()
    app.text_input(key="idea_theme").set_value("  入力テーマ  ")
    app.text_area(key="idea_text_input").set_value("資料の根拠")
    app.button(key="idea_start_ai").click().run()
    assert not app.exception
    roots = mindmap.get_nodes_by_project(app.session_state["current_session_id"])
    assert calls == [("資料の根拠", "入力テーマ")]
    assert len(roots) == 1 and roots[0].title == "生成テーマ"
    assert roots[0].children[0].title == "生成された枝"
    assert db.fetch_tasks() == []


def test_canvas_selection_changes_editor_and_event_is_processed_once() -> None:
    """キャンバス選択は編集対象を変え、同じ折り畳みイベントを再適用しない。"""
    app = _app()
    project_id, root_id = _start_manual(app)
    child_id = mindmap.create_node(project_id, "idea", "選択対象", parent_id=root_id)
    app.session_state["test_canvas_event"] = {
        "type": "select", "node_id": str(child_id), "event_id": "select-1",
    }
    app.run()
    assert not app.exception
    assert app.session_state[f"selected_mindmap_node_id_{project_id}"] == child_id
    assert _input(app, "タイトル").value == "選択対象"
    event = {"type": "toggle", "node_id": root_id, "event_id": "toggle-1"}
    app.session_state["test_canvas_event"] = event
    app.run()
    assert root_id in app.session_state[f"collapsed_mindmap_node_ids_{project_id}"]
    app.session_state["test_canvas_event"] = event
    app.run()
    assert not app.exception
    assert root_id in app.session_state[f"collapsed_mindmap_node_ids_{project_id}"]


def test_expansion_failure_leaves_map_editable(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI展開の失敗では既存木を変更せず、続けて手動追加できる。"""
    def fail_expansion(*args: Any, **kwargs: Any) -> Any:
        """展開の通信障害を模擬する。"""
        raise RuntimeError("接続できません")

    monkeypatch.setattr(nlp, "expand_node", fail_expansion)
    app = _app()
    project_id, root_id = _start_manual(app)
    app.button(key=f"expand_node_{root_id}").click().run()
    assert not app.exception
    assert any("手動で子ノードを追加できます" in error.value for error in app.error)
    assert mindmap.get_nodes_by_project(project_id)[0].children == []
    _input(app, "子ノードのタイトル").set_value("障害後の手動案")
    _button(app, "子ノードを追加").click().run()
    assert not app.exception
    assert mindmap.get_nodes_by_project(project_id)[0].children[0].title == "障害後の手動案"


def test_invalid_manual_edit_preserves_saved_node() -> None:
    """空タイトルの保存失敗は通知され、保存済みのタイトルを破壊しない。"""
    app = _app()
    project_id, _ = _start_manual(app)
    _input(app, "タイトル").set_value("  ")
    _button(app, "変更を保存").click().run()
    assert not app.exception
    assert any("保存できませんでした" in error.value for error in app.error)
    assert mindmap.get_nodes_by_project(project_id)[0].title == "暮らしの改善"


def test_plan_generation_is_opt_in_and_saves_only_checked_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """プラン生成だけでは登録せず、明示選択した行動だけをタスク化する。"""
    def generate(nodes: List[mindmap.MindmapNode]) -> List[Dict[str, str]]:
        """選択用の二候補を返す。"""
        return [
            {"title": "最初の実験", "due_hint": "今週（目安）", "reason": "仮説を確認"},
            {"title": "結果を整理", "due_hint": "実験後（目安）", "reason": "次の判断"},
        ]

    monkeypatch.setattr(nlp, "generate_action_plan", generate)
    app = _app()
    project_id, _ = _start_manual(app)
    app.button(key=f"generate_plan_{project_id}").click().run()
    assert not app.exception
    assert db.fetch_tasks() == []
    assert app.button(key=f"save_plan_{project_id}").disabled
    next(box for box in app.checkbox if box.label == "最初の実験").check().run()
    app.button(key=f"save_plan_{project_id}").click().run()
    assert not app.exception
    tasks = db.fetch_tasks()
    assert [task["raw_text"] for task in tasks] == ["最初の実験"]
    assert tasks[0]["due_date"] is None
    plan = app.session_state[f"idea_action_plan_{project_id}"]
    assert [action["title"] for action in plan["actions"]] == ["結果を整理"]
    assert app.button(key=f"save_plan_{project_id}").disabled


def test_plan_generation_failure_preserves_existing_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全体プランの生成失敗を表示して既存木と手動編集手段を残す。"""
    def fail_plan(nodes: List[mindmap.MindmapNode]) -> List[Dict[str, str]]:
        """全体プランの生成障害を再現する。"""
        raise RuntimeError("生成に失敗")

    monkeypatch.setattr(nlp, "generate_action_plan", fail_plan)
    app = _app()
    project_id, root_id = _start_manual(app)
    app.button(key=f"generate_plan_{project_id}").click().run()
    assert not app.exception
    assert any("プラン生成に失敗" in error.value for error in app.error)
    assert mindmap.get_nodes_by_project(project_id)[0].id == root_id
    assert _input(app, "タイトル").value == "暮らしの改善"
    assert db.fetch_tasks() == []


@pytest.mark.parametrize(
    ("mode", "kind"),
    [("related_ideas", "idea"), ("related_tasks", "task"),
     ("detail_steps", "detail"), ("questions", "note"),
     ("perspectives", "note"), ("combine", "idea")],
)
def test_all_expansion_modes_pass_context_and_persist_only_candidates(
    monkeypatch: pytest.MonkeyPatch, mode: str, kind: str,
) -> None:
    """六つのUIモードが経路・兄弟・既存子付きで正しい種類の子を追加する。"""
    prompts: List[str] = []

    def generate(prompt: str, *, hint: str = "", timeout: float = 45) -> Any:
        """生成層の入力を記録しモードに対応する一候補を返す。"""
        prompts.append(prompt)
        return [{"kind": kind, "title": "追加候補", "body": "具体的な内容"}]

    monkeypatch.setattr(nlp.llm, "generate_json", generate)
    app = _app()
    project_id, root_id = _start_manual(app)
    branch_id = mindmap.create_node(project_id, "idea", "選択した枝", parent_id=root_id)
    mindmap.create_node(project_id, "idea", "別の切り口", parent_id=root_id)
    mindmap.create_node(project_id, "note", "既存の問い", parent_id=branch_id)
    app.session_state["test_canvas_event"] = {
        "type": "select", "node_id": branch_id, "event_id": "select-branch",
    }
    app.session_state[f"idea_action_plan_{project_id}"] = {
        "actions": [{"title": "古い候補"}], "version": "old",
    }
    app.run()
    app.selectbox(key=f"expansion_mode_{branch_id}").select(mode)
    app.button(key=f"expand_node_{branch_id}").click().run()
    assert not app.exception
    assert len(prompts) == 1
    assert all(text in prompts[0] for text in (
        "暮らしの改善", "選択した枝", "別の切り口", "既存の問い",
    ))
    assert f"kindは必ず{kind}" in prompts[0]
    branch = next(node for node in mindmap.get_nodes_by_project(project_id)[0].children
                  if node.id == branch_id)
    assert [(child.title, child.kind) for child in branch.children] == [
        ("既存の問い", "note"), ("追加候補", kind),
    ]
    assert f"idea_action_plan_{project_id}" not in app.session_state
    assert db.fetch_tasks() == []


def test_initial_branch_retry_recovers_without_replacing_manual_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """初期枝生成が一度失敗しても手動ルートを保持して再試行で枝だけ追加する。"""
    calls: List[Tuple[str, str]] = []

    def generate(source_text: str = "", *, theme: str = "") -> Dict[str, Any]:
        """最初は失敗し、次の試行では別タイトルの生成木を返す。"""
        calls.append((source_text, theme))
        if len(calls) == 1:
            raise RuntimeError("一時的な障害")
        return {"kind": "root", "title": "AIが変更したテーマ", "children": [
            {"kind": "idea", "title": "最初の切り口", "children": [
                {"kind": "note", "title": "検証したい問い"},
            ]},
        ]}

    monkeypatch.setattr(nlp, "generate_initial_mindmap", generate)
    project_id = db.create_idea_session("保持するテーマ", "", "背景の資料")
    root_id = mindmap.create_node(project_id, "root", "保持するテーマ")
    app = _app()
    app.button(key=f"load_session_{project_id}").click().run()
    app.button(key=f"generate_initial_branches_{root_id}").click().run()
    assert not app.exception
    assert any("手動編集は続けられます" in error.value for error in app.error)
    assert mindmap.get_nodes_by_project(project_id)[0].children == []
    assert _input(app, "タイトル").value == "保持するテーマ"
    app.button(key=f"generate_initial_branches_{root_id}").click().run()
    assert not app.exception
    roots = mindmap.get_nodes_by_project(project_id)
    assert calls == [("背景の資料", "保持するテーマ"), ("背景の資料", "保持するテーマ")]
    assert len(roots) == 1
    assert (roots[0].id, roots[0].title) == (root_id, "保持するテーマ")
    assert roots[0].children[0].children[0].title == "検証したい問い"
    assert len(db.fetch_idea_sessions()) == 1
    assert db.fetch_tasks() == []


def test_session_delete_requires_confirmation_and_cancel_keeps_map() -> None:
    """セッション削除は確認前とキャンセル時にはデータを保持し確定時だけ消す。"""
    app = _app()
    project_id, root_id = _start_manual(app)
    app.button(key=f"del_session_{project_id}").click().run()
    assert not app.exception
    assert db.get_idea_session(project_id) is not None
    assert mindmap.get_nodes_by_project(project_id)[0].id == root_id
    app.button(key=f"no_del_session_{project_id}").click().run()
    assert not app.exception
    assert db.get_idea_session(project_id) is not None
    app.button(key=f"del_session_{project_id}").click().run()
    app.button(key=f"yes_del_session_{project_id}").click().run()
    assert not app.exception
    assert db.get_idea_session(project_id) is None
    assert mindmap.get_nodes_by_project(project_id) == []
    assert app.text_input(key="idea_theme")


def _open_add_child(app: AppTest, node_id: int, event_id: str = "add-1") -> None:
    """キャンバスの子追加イベントを注入し選択パネルを開く。"""
    app.session_state["test_canvas_event"] = {
        "type": "add_child", "node_id": node_id, "event_id": event_id,
    }
    app.run()
    assert not app.exception
    assert app.radio(key=f"add_child_method_{node_id}")


@pytest.mark.parametrize("as_task", [False, True])
def test_plus_selects_parent_and_manual_save_reopens_collapsed_branch(as_task: bool) -> None:
    """＋で親を選び、手入力のアイデアまたはタスクノードを追加して枝を開く。"""
    app = _app()
    project_id, root_id = _start_manual(app)
    branch_id = mindmap.create_node(project_id, "idea", "追加先", parent_id=root_id)
    mindmap.create_node(project_id, "idea", "既存の子", parent_id=branch_id)
    app.session_state[f"collapsed_mindmap_node_ids_{project_id}"] = {branch_id}
    _open_add_child(app, branch_id)
    assert app.session_state[f"selected_mindmap_node_id_{project_id}"] == branch_id
    assert branch_id in app.session_state[f"collapsed_mindmap_node_ids_{project_id}"]
    app.radio(key=f"add_child_method_{branch_id}").set_value("✏️ 自分で入力する").run()
    _input(app, "追加する子ノードのタイトル（必須）").set_value("  新しい枝  ")
    next(box for box in app.checkbox if box.label == "タスクにする").set_value(as_task)
    _button(app, "子ノードを保存").click().run()
    assert not app.exception
    branch = mindmap.get_nodes_by_project(project_id)[0].children[0]
    assert [(child.title, child.kind, child.depth) for child in branch.children] == [
        ("既存の子", "idea", 2), ("新しい枝", "task" if as_task else "idea", 2),
    ]
    assert branch_id not in app.session_state[f"collapsed_mindmap_node_ids_{project_id}"]
    assert db.fetch_tasks() == []
    assert any("マインドマップの使い方" in caption.value for caption in app.caption)
    app.session_state["test_canvas_event"] = {
        "type": "add_child", "node_id": branch_id, "event_id": "add-1",
    }
    app.run()
    assert not app.exception
    assert len(mindmap.get_nodes_by_project(project_id)[0].children[0].children) == 2
    assert len(app.radio) == 0


@pytest.mark.parametrize("limit_name, limit", [("MAX_MINDMAP_NODES", 1), ("MAX_MINDMAP_DEPTH", 0)])
def test_plus_manual_requires_title_and_respects_capacity(
    monkeypatch: pytest.MonkeyPatch, limit_name: str, limit: int,
) -> None:
    """空タイトルと容量・階層上限超過は保存せず、入力値と既存木を残す。"""
    app = _app()
    project_id, root_id = _start_manual(app)
    _open_add_child(app, root_id)
    app.radio(key=f"add_child_method_{root_id}").set_value("✏️ 自分で入力する").run()
    _input(app, "追加する子ノードのタイトル（必須）").set_value("  ")
    _button(app, "子ノードを保存").click().run()
    assert not app.exception
    assert any("タイトルを入力" in warning.value for warning in app.warning)
    monkeypatch.setattr(mindmap, limit_name, limit)
    _input(app, "追加する子ノードのタイトル（必須）").set_value("容量超過")
    _button(app, "子ノードを保存").click().run()
    assert not app.exception
    assert any("保存できませんでした" in error.value for error in app.error)
    assert _input(app, "追加する子ノードのタイトル（必須）").value == "容量超過"
    assert mindmap.get_nodes_by_project(project_id)[0].children == []


@pytest.mark.parametrize("mode", ["related_ideas", "related_tasks"])
def test_plus_ai_reuses_expansion_and_persists_children(
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """＋パネルから既存の生成処理でアイデアまたはタスクの子ノードを保存する。"""
    kind = ideas_view.EXPANSION_MODES[mode][1]

    def generate(*args: Any, **kwargs: Any) -> List[Dict[str, str]]:
        """通信せず、選択モードに適合する候補を返す。"""
        assert args[3] == mode
        assert "path" in kwargs
        return [{"kind": kind, "title": "AIの追加候補", "body": "生成の説明"}]

    monkeypatch.setattr(nlp, "expand_node", generate)
    app = _app()
    project_id, root_id = _start_manual(app)
    _open_add_child(app, root_id)
    app.selectbox(key=f"expansion_mode_{root_id}").select(mode)
    app.button(key=f"expand_node_{root_id}").click().run()
    assert not app.exception
    children = mindmap.get_nodes_by_project(project_id)[0].children
    assert [(child.title, child.kind) for child in children] == [("AIの追加候補", kind)]
    assert db.fetch_tasks() == []


@pytest.mark.parametrize("failure", [RuntimeError("オフライン"), ValueError("APIキーがありません")])
def test_plus_ai_failure_allows_switch_to_manual(
    monkeypatch: pytest.MonkeyPatch, failure: Exception,
) -> None:
    """キーなし・通信障害でも追加パネルから手入力へ切り替えて保存できる。"""
    def fail(*args: Any, **kwargs: Any) -> Any:
        """ネットワークに接続せず生成失敗を再現する。"""
        raise failure

    monkeypatch.setattr(nlp, "expand_node", fail)
    app = _app()
    project_id, root_id = _start_manual(app)
    _open_add_child(app, root_id)
    app.button(key=f"expand_node_{root_id}").click().run()
    assert not app.exception
    assert any("手動で子ノードを追加できます" in error.value for error in app.error)
    app.radio(key=f"add_child_method_{root_id}").set_value("✏️ 自分で入力する").run()
    _input(app, "追加する子ノードのタイトル（必須）").set_value("手入力で継続")
    _button(app, "子ノードを保存").click().run()
    assert not app.exception
    assert mindmap.get_nodes_by_project(project_id)[0].children[0].title == "手入力で継続"


@pytest.mark.parametrize("event", [
    {"type": "add_child", "node_id": True, "event_id": "a"},
    {"type": "add_child", "node_id": 1.5, "event_id": "a"},
    {"type": "add_child", "node_id": [], "event_id": "a"},
    {"type": "add_child", "node_id": 999, "event_id": "a"},
    {"type": "add_child", "node_id": 1, "event_id": ""},
    {"type": "add_child", "node_id": 1},
])
def test_plus_invalid_events_are_ignored(event: Any) -> None:
    """不正な子追加イベントを既存の検証方針に従って無視する。"""
    assert ideas_view._parse_mindmap_event(event, {1}) is None
