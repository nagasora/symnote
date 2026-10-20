"""マップの展開文脈・安全な出力・承認後のタスク化を検証する。"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any, Dict, List

import pytest

from symnote.core.db import connection, fetch_tasks, init_db
from symnote.core.mindmap import (
    MindmapNode,
    create_node,
    create_tasks_from_action_plan,
    ensure_memo_item_for_node,
    ensure_task_item_for_node,
    export_markdown,
    get_expansion_context,
    get_nodes_by_project,
    save_node_expansion,
)


@pytest.fixture
def root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MindmapNode:
    """利用者の DB と分離した一時マップを準備する。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "actions.db"))
    init_db()
    create_node(12, "root", "暮らしをよくする", "生活改善のアイデア")
    return get_nodes_by_project(12)[0]


def test_context_includes_path_siblings_and_existing_children(root: MindmapNode) -> None:
    """選択ノードの祖先・兄弟・既存子を過不足なく取得する。"""
    branch = create_node(12, "idea", "食事", parent_id=root.id)
    sibling = create_node(12, "idea", "睡眠", parent_id=root.id)
    leaf = create_node(12, "task", "野菜を買う", parent_id=branch)
    context = get_expansion_context(get_nodes_by_project(12), branch)
    assert [node["id"] for node in context["path"]] == [root.id, branch]
    assert [node["id"] for node in context["siblings"]] == [sibling]
    assert [node["id"] for node in context["existing_children"]] == [leaf]
    with pytest.raises(ValueError, match="見つかりません"):
        get_expansion_context([root], 999)


def test_expansion_removes_existing_and_response_duplicates(root: MindmapNode) -> None:
    """本文が違っても全半角・空白・大文字小文字だけ違うタイトルは重複扱いする。"""
    create_node(12, "note", "ＡＢＣ   plan", "既存", parent_id=root.id)
    ids = save_node_expansion(root.id, [
        {"title": "abc plan", "body": "別の説明"},
        {"title": "新たな問い", "body": "何を試す？"},
        {"title": " 新たな問い ", "body": "異なる本文"},
    ], "note")
    assert len(ids) == 1
    assert [node.title for node in get_nodes_by_project(12)[0].children] == ["ＡＢＣ   plan", "新たな問い"]
    assert save_node_expansion(root.id, [{"title": "新たな問い"}], "note") == []


def test_task_conversion_keeps_due_and_reuses_persisted_link(root: MindmapNode) -> None:
    """期限付きタスク変換は古いノードオブジェクトから再実行しても重複しない。"""
    stale = get_nodes_by_project(12)[0]
    task_id = ensure_task_item_for_node(root, due_date="2026-12-01")
    assert ensure_task_item_for_node(stale) == task_id
    assert fetch_tasks()[0]["due_date"] == "2026-12-01"
    assert len(fetch_tasks()) == 1
    with connection() as conn:
        assert conn.execute("SELECT task_id FROM calendar_sync_outbox").fetchone()[0] == task_id


def test_memo_link_is_not_mistaken_for_a_task(root: MindmapNode) -> None:
    """メモ保存済みのノードも実際のタスクへ変換できる。"""
    memo_id = ensure_memo_item_for_node(root)
    task_id = ensure_task_item_for_node(root)
    assert task_id != memo_id
    assert fetch_tasks()[0]["id"] == task_id


def test_deleted_link_can_be_recreated(root: MindmapNode) -> None:
    """リンク先が削除されている場合は有効なタスクを作り直す。"""
    item_id = ensure_task_item_for_node(root)
    with connection() as conn, conn:
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    new_id = ensure_task_item_for_node(root)
    assert fetch_tasks()[0]["id"] == new_id


def test_action_plan_is_user_selected_atomic_and_idempotent(root: MindmapNode) -> None:
    """選択した候補だけを保存し、期限目安は確定期限に変換しない。"""
    actions: List[Dict[str, Any]] = [
        {"title": "朝食を試す", "due_hint": "今週中", "reason": "継続しやすさを検証"},
        {"title": "記録を確認する", "due_hint": "一週間後", "reason": "効果測定", "due_date": "2026-12-01"},
    ]
    ids = create_tasks_from_action_plan(12, actions)
    assert len(ids) == 2
    assert create_tasks_from_action_plan(12, actions) == ids
    tasks = {task["id"]: task for task in fetch_tasks()}
    assert len(tasks) == 2
    assert tasks[ids[0]]["due_date"] is None
    assert tasks[ids[1]]["due_date"] == "2026-12-01"
    saved = get_nodes_by_project(12)[0].children
    assert all(node.kind == "task" and node.linked_item_id for node in saved)
    assert "今週中" in saved[0].body
    assert "継続しやすさを検証" in saved[0].body


def test_action_plan_rolls_back_entire_batch_on_write_failure(root: MindmapNode) -> None:
    """二つ目のタスク登録が失敗するとノード・項目・通知キューすべてを取り消す。"""
    with connection() as conn, conn:
        conn.execute("""
            CREATE TRIGGER fail_action BEFORE INSERT ON items
            WHEN NEW.raw_text = '失敗する'
            BEGIN SELECT RAISE(ABORT, 'forced failure'); END
        """)
    with pytest.raises(sqlite3.DatabaseError, match="forced failure"):
        create_tasks_from_action_plan(12, [{"title": "保存予定"}, {"title": "失敗する"}])
    assert fetch_tasks() == []
    assert get_nodes_by_project(12)[0].children == []
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM calendar_sync_outbox").fetchone()[0] == 0


def test_action_plan_reuses_existing_task_node(root: MindmapNode) -> None:
    """マップにあるタスク案と同名ならノードを増やさずリンクする。"""
    child_id = create_node(12, "task", "試作する", parent_id=root.id)
    ids = create_tasks_from_action_plan(12, [{"title": "試作する", "reason": "仮説検証"}])
    children = get_nodes_by_project(12)[0].children
    assert len(children) == 1
    assert children[0].id == child_id
    assert children[0].linked_item_id == ids[0]


def test_invalid_action_or_due_date_writes_nothing(root: MindmapNode) -> None:
    """一件でも形式が不正な候補があると書き込みを始めない。"""
    with pytest.raises(ValueError, match="期限日"):
        create_tasks_from_action_plan(12, [{"title": "有効"}, {"title": "不正", "due_date": "近日中"}])
    with pytest.raises(ValueError, match="タイトル"):
        create_tasks_from_action_plan(12, [{"title": ""}])
    assert fetch_tasks() == []
    assert get_nodes_by_project(12)[0].children == []


def test_export_includes_all_levels_and_escapes_markup(root: MindmapNode) -> None:
    """全ノードを安定した階層で出力し外部リンク・HTML を実行可能にしない。"""
    branch_id = create_node(12, "idea", "[危険](https://example.com)", "<script>\n説明", parent_id=root.id)
    create_node(12, "task", "試す", parent_id=branch_id)
    output = export_markdown(get_nodes_by_project(12))
    assert output.startswith("- [テーマ] 暮らしをよくする\n")
    assert "  - [アイデア] \\[危険\\]\\(https://example\\.com\\)" in output
    assert "    - [タスク] 試す" in output
    assert "\\<script\\> 説明" in output
    assert export_markdown([]) == ""


def test_plan_reuses_task_converted_from_idea(root: MindmapNode) -> None:
    """アイデア種別のまま個別タスク化した項目も全体計画で再利用する。"""
    create_node(12, "idea", "試作する", parent_id=root.id)
    idea = get_nodes_by_project(12)[0].children[0]
    first_id = ensure_task_item_for_node(idea)
    ids = create_tasks_from_action_plan(12, [{"title": "試作する"}])
    assert ids == [first_id]
    assert len(fetch_tasks()) == 1
    assert len(get_nodes_by_project(12)[0].children) == 1


def test_initial_retry_preserves_root_and_persists_all_children(root: MindmapNode) -> None:
    """空ルートへの初期再生成は手動ルートの内容を保持する。"""
    from symnote.core.mindmap import append_initial_mindmap_children

    ids = append_initial_mindmap_children(root.id, {
        "kind": "root", "title": "AIが考えた別タイトル", "children": [
            {"kind": "idea", "title": "生活習慣", "children": [{"kind": "note", "title": "何を変える？"}]},
            {"kind": "idea", "title": "環境を変える"},
        ],
    })
    saved = get_nodes_by_project(12)[0]
    assert saved.id == root.id and saved.title == root.title and saved.body == root.body
    assert [node.id for node in saved.children] == ids
    assert saved.children[0].children[0].depth == 2
    with pytest.raises(ValueError, match="既に編集"):
        append_initial_mindmap_children(root.id, {"kind": "root", "title": "再試行", "children": [{"kind": "idea", "title": "追加"}]})


def test_initial_retry_is_atomic(root: MindmapNode) -> None:
    """枝の途中で保存に失敗しても手動ルート以外を残さない。"""
    from symnote.core.mindmap import append_initial_mindmap_children

    with connection() as conn, conn:
        conn.execute("""
            CREATE TRIGGER fail_branch BEFORE INSERT ON mindmap_nodes
            WHEN NEW.title = '失敗する'
            BEGIN SELECT RAISE(ABORT, 'forced branch failure'); END
        """)
    with pytest.raises(sqlite3.DatabaseError, match="forced branch failure"):
        append_initial_mindmap_children(root.id, {"kind": "root", "title": "再試行", "children": [
            {"kind": "idea", "title": "保存されない"}, {"kind": "idea", "title": "失敗する"},
        ]})
    assert get_nodes_by_project(12)[0].children == []
