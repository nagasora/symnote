from __future__ import annotations

import sqlite3

import pytest

from symnote.app import _mindmap_nodes_with_positions, _parse_mindmap_event
from symnote.core.db import get_connection, init_db
from symnote.core.mindmap import (
    MindmapNode,
    create_node,
    delete_node_and_descendants,
    ensure_memo_item_for_node,
    get_nodes_by_project,
    save_mindmap_tree,
    save_node_expansion,
)


def test_save_mindmap_tree_persists_complete_hierarchy(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "mindmap.db"))
    init_db()

    root_id = save_mindmap_tree(
        42,
        {
            "kind": "root",
            "title": "Project",
            "children": [
                {
                    "kind": "idea",
                    "title": "Idea",
                    "body": "Context",
                    "children": [{"kind": "task", "title": "Next step", "children": []}],
                }
            ],
        },
    )

    roots = get_nodes_by_project(42)
    assert roots[0].id == root_id
    assert roots[0].children[0].title == "Idea"
    assert roots[0].children[0].children[0].title == "Next step"

    with pytest.raises(ValueError, match="already has"):
        save_mindmap_tree(
            42,
            {"kind": "root", "title": "Duplicate", "children": []},
        )
    assert len(get_nodes_by_project(42)) == 1


def test_save_mindmap_tree_validates_entire_tree_before_writing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "invalid-mindmap.db"))
    init_db()

    with pytest.raises(ValueError, match="children"):
        save_mindmap_tree(
            42,
            {
                "kind": "root",
                "title": "Project",
                "children": [{"kind": "idea", "title": "Broken", "children": "not a list"}],
            },
        )

    assert get_nodes_by_project(42) == []


def test_save_mindmap_tree_normalizes_leaf_children(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "normalized-mindmap.db"))
    init_db()

    save_mindmap_tree(
        42,
        {
            "kind": "root",
            "title": "Project",
            "children": [{"kind": "task", "title": "Leaf without children"}],
        },
    )

    leaf = get_nodes_by_project(42)[0].children[0]
    assert leaf.title == "Leaf without children"
    assert leaf.children == []


def test_save_mindmap_tree_rolls_back_if_descendant_insert_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "atomic-mindmap.db"))
    init_db()
    conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TRIGGER fail_mindmap_child
            BEFORE INSERT ON mindmap_nodes
            WHEN NEW.title = 'Fail here'
            BEGIN
                SELECT RAISE(ABORT, 'forced child failure');
            END
            """
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(sqlite3.DatabaseError, match="forced child failure"):
        save_mindmap_tree(
            42,
            {
                "kind": "root",
                "title": "Project",
                "children": [
                    {"kind": "idea", "title": "Saved first", "children": []},
                    {"kind": "idea", "title": "Fail here", "children": []},
                ],
            },
        )

    assert get_nodes_by_project(42) == []


def test_ensure_memo_item_for_node_creates_and_reuses_local_memo(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "mindmap-memo.db"))
    init_db()
    save_mindmap_tree(
        42,
        {
            "kind": "root",
            "title": "Project",
            "body": "Project context",
            "children": [],
        },
    )
    node = get_nodes_by_project(42)[0]

    memo_id = ensure_memo_item_for_node(node)
    assert ensure_memo_item_for_node(node) == memo_id

    conn = get_connection()
    try:
        item = conn.execute("SELECT kind, raw_text FROM items WHERE id = ?", (memo_id,)).fetchone()
    finally:
        conn.close()
    assert item["kind"] == "memo"
    assert item["raw_text"] == "Project\n\nProject context"


def test_collapsed_node_hides_its_descendants_from_mindmap_layout() -> None:
    root = MindmapNode(id=1, project_id=42, kind="root", title="Project", body="")
    branch = MindmapNode(id=2, project_id=42, kind="idea", title="Branch", body="", depth=1)
    leaf = MindmapNode(id=3, project_id=42, kind="task", title="Leaf", body="", depth=2)
    root.children = [branch]
    branch.children = [leaf]

    visible, positions = _mindmap_nodes_with_positions([root], collapsed_node_ids={branch.id})

    assert [node.id for node in visible] == [root.id, branch.id]
    assert leaf.id not in positions


def test_save_node_expansion_appends_children_atomically(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "mindmap-expansion.db"))
    init_db()
    root_id = save_mindmap_tree(
        42,
        {"kind": "root", "title": "Project", "children": []},
    )

    inserted = save_node_expansion(
        root_id,
        [
            {"kind": "wrong", "title": "First task", "body": "Do this"},
            {"title": "Second task", "body": "Then this"},
        ],
        "task",
    )

    root = get_nodes_by_project(42)[0]
    assert len(inserted) == 2
    assert [child.title for child in root.children] == ["First task", "Second task"]
    assert all(child.kind == "task" for child in root.children)
    assert all(child.parent_id == root_id for child in root.children)
    assert all(child.depth == 1 for child in root.children)

    with pytest.raises(ValueError, match="タイトル"):
        save_node_expansion(
            root_id,
            [{"title": "Valid"}, {"title": ""}],
            "idea",
        )
    assert len(get_nodes_by_project(42)[0].children) == 2


def test_parse_mindmap_event_rejects_invalid_component_data() -> None:
    valid_ids = {1, 2}

    assert _parse_mindmap_event(
        {"type": "toggle", "node_id": "2", "event_id": "instance:1"}, valid_ids
    ) == ("toggle", 2, "instance:1")
    assert _parse_mindmap_event(
        {"type": "delete", "node_id": 2, "event_id": "instance:2"}, valid_ids
    ) is None
    assert _parse_mindmap_event(
        {"type": "select", "node_id": 99, "event_id": "instance:3"}, valid_ids
    ) is None


def test_create_node_derives_hierarchy_and_rejects_cross_project_parent(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "mindmap-create-node.db"))
    init_db()
    root_id = create_node(42, "root", "Root")
    child_id = create_node(42, "idea", "Child", parent_id=root_id, depth=99, order_index=99)

    child = get_nodes_by_project(42)[0].children[0]
    assert child.id == child_id
    assert child.depth == 1
    assert child.order_index == 0

    with pytest.raises(ValueError, match="does not belong"):
        create_node(99, "idea", "Wrong project", parent_id=root_id)


def test_delete_node_removes_only_selected_subtree(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "mindmap-delete-subtree.db"))
    init_db()
    root_id = create_node(42, "root", "Root")
    removed_id = create_node(42, "idea", "Remove", parent_id=root_id)
    create_node(42, "detail", "Descendant", parent_id=removed_id)
    create_node(42, "idea", "Keep", parent_id=root_id)

    delete_node_and_descendants(removed_id)

    root = get_nodes_by_project(42)[0]
    assert [child.title for child in root.children] == ["Keep"]
