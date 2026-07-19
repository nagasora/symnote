from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional
import datetime as dt
import sqlite3
from symnote.core.db import connection

NodeKind = Literal["root", "idea", "task", "detail", "note", "link"]
VALID_NODE_KINDS = frozenset({"root", "idea", "task", "detail", "note", "link"})


def _validate_tree(node_data: Dict[str, Any]) -> None:
    """Validate an AI-generated mind map before writing any of it to SQLite."""
    if not isinstance(node_data, dict):
        raise ValueError("A mind map node must be a dictionary.")

    required_fields = {"title", "children", "kind"}
    missing = required_fields.difference(node_data)
    if missing:
        raise ValueError(f"Mind map node is missing required fields: {', '.join(sorted(missing))}.")

    title = node_data["title"]
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Mind map node title must be a non-empty string.")
    if not isinstance(node_data["kind"], str) or node_data["kind"] not in VALID_NODE_KINDS:
        raise ValueError(f"Unsupported mind map node kind: {node_data['kind']!r}.")
    children = node_data["children"]
    if not isinstance(children, list):
        raise ValueError("Mind map node children must be a list.")
    if "body" in node_data and not isinstance(node_data["body"], str):
        raise ValueError("Mind map node body must be a string.")

    for child in children:
        _validate_tree(child)


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
    with connection() as conn, conn:
        return _insert_tree_node(conn, project_id, tree, None, 0, 0, now)

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
    """Creates a new mindmap node."""
    now = dt.datetime.now().isoformat(timespec="seconds")
    with connection() as conn, conn:
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
                title,
                body,
                order_index,
                depth,
                linked_item_id,
                now,
                now,
            ),
        )
        return cur.lastrowid

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
    """Deletes a node and all its children recursively."""
    # SQLite doesn't support recursive delete easily without CTEs or foreign key cascade (which might not be enabled)
    # For now, we'll do a simple fetch-and-delete or just delete by project if clearing all.
    # To properly delete a subtree, we need to find all descendants.
    with connection() as conn, conn:
        # 1. Find all descendants
        ids_to_delete = {node_id}

        # Iterative BFS to find all children
        queue = [node_id]
        while queue:
            current_id = queue.pop(0)
            cur = conn.execute("SELECT id FROM mindmap_nodes WHERE parent_id = ?", (current_id,))
            children = [row[0] for row in cur.fetchall()]
            ids_to_delete.update(children)
            queue.extend(children)

        # 2. Delete them
        placeholders = ",".join("?" for _ in ids_to_delete)
        conn.execute(f"DELETE FROM mindmap_nodes WHERE id IN ({placeholders})", list(ids_to_delete))

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
    return item_id
