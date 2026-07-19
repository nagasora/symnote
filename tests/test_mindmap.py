from __future__ import annotations

import sqlite3

import pytest

from symnote.core.db import get_connection, init_db
from symnote.core.mindmap import get_nodes_by_project, save_mindmap_tree


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
