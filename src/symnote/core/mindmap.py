from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional
import datetime as dt
import sqlite3
from symnote.core.db import connection

NodeKind = Literal["root", "idea", "task", "detail", "note", "link"]
VALID_NODE_KINDS = frozenset({"root", "idea", "task", "detail", "note", "link"})
MAX_MINDMAP_NODES = 250
MAX_MINDMAP_DEPTH = 8
MAX_NODE_TITLE_LENGTH = 200
MAX_NODE_BODY_LENGTH = 4000
MAX_EXPANSION_ITEMS = 8


def _validate_tree(node_data: Dict[str, Any]) -> None:
    """Validate and normalize a bounded AI-generated tree iteratively."""
    if not isinstance(node_data, dict):
        raise ValueError("A mind map node must be a dictionary.")
    if node_data.get("kind") != "root":
        raise ValueError("The top-level mind map node must have kind 'root'.")

    stack: List[tuple[Dict[str, Any], int]] = [(node_data, 0)]
    node_count = 0
    while stack:
        current, depth = stack.pop()
        node_count += 1
        if node_count > MAX_MINDMAP_NODES:
            raise ValueError("Mind map contains too many nodes.")
        if depth > MAX_MINDMAP_DEPTH:
            raise ValueError("Mind map is too deeply nested.")
        if not isinstance(current, dict):
            raise ValueError("A mind map node must be a dictionary.")
        missing = {"title", "kind"}.difference(current)
        if missing:
            raise ValueError(
                f"Mind map node is missing required fields: {', '.join(sorted(missing))}."
            )
        title = current["title"]
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Mind map node title must be a non-empty string.")
        if len(title.strip()) > MAX_NODE_TITLE_LENGTH:
            raise ValueError("Mind map node title is too long.")
        if current["kind"] not in VALID_NODE_KINDS:
            raise ValueError(f"Unsupported mind map node kind: {current['kind']!r}.")
        body = current.setdefault("body", "")
        if not isinstance(body, str):
            raise ValueError("Mind map node body must be a string.")
        if len(body) > MAX_NODE_BODY_LENGTH:
            raise ValueError("Mind map node body is too long.")
        children = current.setdefault("children", [])
        if not isinstance(children, list):
            raise ValueError("Mind map node children must be a list.")
        for child in reversed(children):
            if not isinstance(child, dict):
                raise ValueError("A mind map node must be a dictionary.")
            stack.append((child, depth + 1))


def _insert_tree_node(
    conn: sqlite3.Connection,
    project_id: int,
    node_data: Dict[str, Any],
    parent_id: Optional[int],
    depth: int,
    order_index: int,
    now: str,
) -> int:
    """Insert one validated node and all descendants with an existing connection."""
    cursor = conn.execute(
        """
        INSERT INTO mindmap_nodes (
            project_id, parent_id, kind, title, body,
            order_index, depth, linked_item_id, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            project_id,
            parent_id,
            node_data["kind"],
            node_data["title"].strip(),
            node_data.get("body", ""),
            order_index,
            depth,
            now,
            now,
        ),
    )
    node_id = int(cursor.lastrowid)
    for child_index, child in enumerate(node_data["children"]):
        _insert_tree_node(
            conn,
            project_id,
            child,
            node_id,
            depth + 1,
            child_index,
            now,
        )
    return node_id


def save_mindmap_tree(project_id: int, tree: Dict[str, Any]) -> int:
    """Persist a complete mind map atomically and return its root node ID.

    The whole generated tree is validated before opening a write transaction.  If
    a database error occurs while inserting a descendant, SQLite rolls back the
    root and every previously inserted node as well.
    """
    _validate_tree(tree)
    now = dt.datetime.now().isoformat(timespec="seconds")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            exists = conn.execute(
                "SELECT 1 FROM mindmap_nodes WHERE project_id = ? LIMIT 1", (project_id,)
            ).fetchone()
            if exists is not None:
                raise ValueError("This project already has a mind map.")
            root_id = _insert_tree_node(conn, project_id, tree, None, 0, 0, now)
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return root_id

@dataclass
class MindmapNode:
    """Represents a single node in the mind map."""
    id: int
    project_id: int
    kind: NodeKind
    title: str
    body: str
    parent_id: Optional[int] = None
    order_index: int = 0
    depth: int = 0
    linked_item_id: Optional[int] = None
    created_at: str = ""
    updated_at: str = ""
    children: List["MindmapNode"] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MindmapNode":
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            kind=row["kind"],
            title=row["title"],
            body=row["body"] or "",
            parent_id=row["parent_id"],
            order_index=row["order_index"],
            depth=row["depth"],
            linked_item_id=row["linked_item_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

def create_node(
    project_id: int,
    kind: NodeKind,
    title: str,
    body: str = "",
    parent_id: Optional[int] = None,
    order_index: int = 0,
    depth: int = 0,
    linked_item_id: Optional[int] = None,
) -> int:
    """Create a validated node, deriving hierarchy fields from its parent."""
    if kind not in VALID_NODE_KINDS:
        raise ValueError(f"Unsupported mind map node kind: {kind!r}.")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Mind map node title must be a non-empty string.")
    if not isinstance(body, str):
        raise ValueError("Mind map node body must be a string.")
    if len(title.strip()) > MAX_NODE_TITLE_LENGTH or len(body) > MAX_NODE_BODY_LENGTH:
        raise ValueError("Mind map node content is too long.")
    now = dt.datetime.now().isoformat(timespec="seconds")
    with connection() as conn, conn:
        if parent_id is None:
            if kind != "root":
                raise ValueError("A node without a parent must have kind 'root'.")
            if conn.execute(
                "SELECT 1 FROM mindmap_nodes WHERE project_id = ? LIMIT 1", (project_id,)
            ).fetchone():
                raise ValueError("This project already has a mind map.")
            depth = 0
            order_index = 0
        else:
            parent = conn.execute(
                "SELECT project_id, depth FROM mindmap_nodes WHERE id = ?", (parent_id,)
            ).fetchone()
            if parent is None or int(parent["project_id"]) != project_id:
                raise ValueError("Parent node does not belong to this project.")
            depth = int(parent["depth"]) + 1
            order_index = int(
                conn.execute(
                    "SELECT COALESCE(MAX(order_index), -1) + 1 FROM mindmap_nodes "
                    "WHERE parent_id = ?",
                    (parent_id,),
                ).fetchone()[0]
            )
        cur = conn.execute(
            """
            INSERT INTO mindmap_nodes (
                project_id, parent_id, kind, title, body,
                order_index, depth, linked_item_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                parent_id,
                kind,
                title.strip(),
                body,
                order_index,
                depth,
                linked_item_id,
                now,
                now,
            ),
        )
        return cur.lastrowid


def save_node_expansion(
    parent_id: int, items: List[Dict[str, Any]], expected_kind: NodeKind
) -> List[int]:
    """Validate and atomically append AI-generated children to a node."""
    if expected_kind not in {"idea", "task", "detail"}:
        raise ValueError("Unsupported expansion kind.")
    if not isinstance(items, list) or not items:
        raise ValueError("AIから追加できるノードが返されませんでした。")
    if len(items) > MAX_EXPANSION_ITEMS:
        raise ValueError("AIが一度に多すぎるノードを返しました。")

    normalized: List[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("AIが不正なノードデータを返しました。")
        title = item.get("title")
        body = item.get("body", "")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("生成されたノードにタイトルがありません。")
        if not isinstance(body, str):
            raise ValueError("生成されたノードの本文が不正です。")
        normalized_title = title.strip()
        normalized_body = body.strip()
        if len(normalized_title) > MAX_NODE_TITLE_LENGTH:
            raise ValueError("生成されたノードのタイトルが長すぎます。")
        if len(normalized_body) > MAX_NODE_BODY_LENGTH:
            raise ValueError("生成されたノードの本文が長すぎます。")
        identity = (normalized_title.casefold(), normalized_body.casefold())
        if identity not in seen:
            normalized.append((normalized_title, normalized_body))
            seen.add(identity)
    if not normalized:
        raise ValueError("追加できる一意なノードがありませんでした。")

    now = dt.datetime.now().isoformat(timespec="seconds")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            parent = conn.execute(
                "SELECT project_id, depth FROM mindmap_nodes WHERE id = ?", (parent_id,)
            ).fetchone()
            if parent is None:
                raise ValueError("親ノードが見つかりません。")
            row = conn.execute(
                "SELECT COALESCE(MAX(order_index), -1) FROM mindmap_nodes WHERE parent_id = ?",
                (parent_id,),
            ).fetchone()
            next_order = int(row[0]) + 1
            inserted_ids: List[int] = []
            for offset, (title, body) in enumerate(normalized):
                cursor = conn.execute(
                """
                INSERT INTO mindmap_nodes (
                    project_id, parent_id, kind, title, body,
                    order_index, depth, linked_item_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                    (
                        parent["project_id"],
                        parent_id,
                        expected_kind,
                        title,
                        body,
                        next_order + offset,
                        int(parent["depth"]) + 1,
                        now,
                        now,
                    ),
                )
                inserted_ids.append(int(cursor.lastrowid))
        except Exception:
            conn.rollback()
            raise
        conn.commit()
    return inserted_ids

def get_nodes_by_project(project_id: int) -> List[MindmapNode]:
    """Retrieves all nodes for a given project, ordered by depth and order_index."""
    with connection() as conn:
        cur = conn.execute(
            """
            SELECT * FROM mindmap_nodes
            WHERE project_id = ?
            ORDER BY depth ASC, order_index ASC
            """,
            (project_id,),
        )
        rows = cur.fetchall()
    nodes = [MindmapNode.from_row(r) for r in rows]
    return _build_tree(nodes)

def _build_tree(nodes: List[MindmapNode]) -> List[MindmapNode]:
    """Reconstructs the tree structure from a flat list of nodes."""
    node_map = {node.id: node for node in nodes}
    roots = []
    
    for node in nodes:
        if node.parent_id and node.parent_id in node_map:
            parent = node_map[node.parent_id]
            parent.children.append(node)
        else:
            roots.append(node)
            
    return roots

def update_node(node_id: int, **fields: Any) -> None:
    """Updates fields of a mindmap node."""
    allowed = {"title", "body", "order_index", "linked_item_id", "kind"}
    if "kind" in fields and fields["kind"] not in VALID_NODE_KINDS:
        raise ValueError("Unsupported mind map node kind.")
    if "title" in fields and (
        not isinstance(fields["title"], str)
        or not fields["title"].strip()
        or len(fields["title"].strip()) > MAX_NODE_TITLE_LENGTH
    ):
        raise ValueError("Invalid mind map node title.")
    if "body" in fields and (
        not isinstance(fields["body"], str) or len(fields["body"]) > MAX_NODE_BODY_LENGTH
    ):
        raise ValueError("Invalid mind map node body.")
    updates = []
    params = []
    
    for key, value in fields.items():
        if key in allowed:
            updates.append(f"{key} = ?")
            params.append(value)
            
    if not updates:
        return

    updates.append("updated_at = ?")
    params.append(dt.datetime.now().isoformat(timespec="seconds"))
    params.append(node_id)
    
    with connection() as conn, conn:
        conn.execute(
            f"UPDATE mindmap_nodes SET {', '.join(updates)} WHERE id = ?",
            params
        )

def delete_node_and_descendants(node_id: int) -> None:
    """Delete a subtree in one SQLite statement, retaining linked tasks and memos."""
    with connection() as conn, conn:
        conn.execute(
            """
            WITH RECURSIVE subtree(id) AS (
                SELECT id FROM mindmap_nodes WHERE id = ?
                UNION ALL
                SELECT child.id
                FROM mindmap_nodes AS child
                JOIN subtree ON child.parent_id = subtree.id
            )
            DELETE FROM mindmap_nodes WHERE id IN (SELECT id FROM subtree)
            """,
            (node_id,),
        )

def ensure_task_item_for_node(node: MindmapNode) -> int:
    """
    Ensures a TaskCard node has a corresponding item in the items table.
    Returns the item_id.
    """
    if node.linked_item_id:
        return node.linked_item_id
        
    from symnote.core.db import insert_task
    
    # Create new task
    item_id = insert_task(
        raw_text=node.title,
        status="inbox"
    )
    
    # Link it back
    update_node(node.id, linked_item_id=item_id)
    node.linked_item_id = item_id
    return item_id


def ensure_memo_item_for_node(node: MindmapNode) -> int:
    """Save a mind-map node as a memo and retain the local item link."""
    if node.linked_item_id:
        return node.linked_item_id

    from symnote.core.db import insert_memo

    text = node.title if not node.body else f"{node.title}\n\n{node.body}"
    item_id = insert_memo(raw_text=text, tags="mindmap")
    update_node(node.id, linked_item_id=item_id)
    node.linked_item_id = item_id
    return item_id
