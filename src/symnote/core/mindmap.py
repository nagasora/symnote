"""マインドマップの検証・保存と、ローカルタスクへの変換を提供する。"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple
import datetime as dt
import re
import sqlite3
import unicodedata
from symnote.core.db import connection, insert_item_in_transaction

NodeKind = Literal["root", "idea", "task", "detail", "note", "link"]
VALID_NODE_KINDS = frozenset({"root", "idea", "task", "detail", "note", "link"})
MAX_MINDMAP_NODES = 250
MAX_MINDMAP_DEPTH = 8
MAX_NODE_TITLE_LENGTH = 200
MAX_NODE_BODY_LENGTH = 4000
MAX_EXPANSION_ITEMS = 8


def _validate_tree(node_data: Dict[str, Any]) -> None:
    """AI が生成した木を反復処理で検証し、安全な上限内で正規化する。"""
    if not isinstance(node_data, dict):
        raise ValueError("A mind map node must be a dictionary.")
    if node_data.get("kind") != "root":
        raise ValueError("The top-level mind map node must have kind 'root'.")

    stack: List[Tuple[Dict[str, Any], int]] = [(node_data, 0)]
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
    """既存トランザクション内で検証済みノードと子孫を保存する。"""
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
    """木全体を検証し、ルートと全子孫を原子的に保存してルート ID を返す。"""
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
    """マインドマップ内のノードとその子孫を表す。"""
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
        """SQLite の行をノードへ変換する。"""
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
    """親から階層情報を導出し、検証済みの手動ノードを作成する。"""
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
        _check_node_capacity(conn, project_id, depth, 1)
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
    """AI の追加候補を検証し、既存子との重複を除いて原子的に保存する。"""
    if expected_kind not in {"idea", "task", "detail", "note"}:
        raise ValueError("Unsupported expansion kind.")
    if not isinstance(items, list) or not items:
        raise ValueError("AIから追加できるノードが返されませんでした。")
    if len(items) > MAX_EXPANSION_ITEMS:
        raise ValueError("AIが一度に多すぎるノードを返しました。")

    normalized: List[Tuple[str, str]] = []
    seen: set[str] = set()
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
        identity = normalize_node_title(normalized_title)
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
            existing = {
                normalize_node_title(row["title"])
                for row in conn.execute(
                    "SELECT title FROM mindmap_nodes WHERE parent_id = ?", (parent_id,)
                )
            }
            normalized = [item for item in normalized if normalize_node_title(item[0]) not in existing]
            if normalized:
                _check_node_capacity(conn, int(parent["project_id"]), int(parent["depth"]) + 1, len(normalized))
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
    """指定プロジェクトのノードを階層と表示順に従って取得する。"""
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
    """平坦なノード一覧から親子関係を復元する。"""
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
    """許可されたノード項目を検証して更新する。"""
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
    """リンク先タスク・メモを残し、選択した部分木だけを削除する。"""
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

def normalize_node_title(title: str) -> str:
    """全半角・大文字小文字・空白の差を除いて重複判定用の表題を返す。"""
    return " ".join(unicodedata.normalize("NFKC", title).casefold().split())


def _check_node_capacity(
    conn: sqlite3.Connection, project_id: int, depth: int, additional: int
) -> None:
    """手動追加や展開でも初期生成と同じ深さ・個数の上限を守る。"""
    if depth > MAX_MINDMAP_DEPTH:
        raise ValueError("マップの階層が深すぎます。別の枝に追加してください。")
    count = int(conn.execute(
        "SELECT COUNT(*) FROM mindmap_nodes WHERE project_id = ?", (project_id,)
    ).fetchone()[0])
    if count + additional > MAX_MINDMAP_NODES:
        raise ValueError("マップのノード数が上限に達しています。不要なノードを整理してください。")


def _validate_due_date(due_date: str | None) -> str | None:
    """利用者が指定した期限日だけを ISO 日付として検証する。"""
    if due_date is None or due_date == "":
        return None
    if not isinstance(due_date, str):
        raise ValueError("期限日は YYYY-MM-DD 形式で指定してください。")
    try:
        return dt.date.fromisoformat(due_date).isoformat()
    except ValueError:
        raise ValueError("期限日は YYYY-MM-DD 形式で指定してください。") from None


def _ensure_item_in_transaction(
    conn: sqlite3.Connection, node_id: int, kind: str, due_date: str | None = None
) -> int:
    """最新のノードを参照し、項目作成とリンク更新を同一トランザクションで行う。"""
    row = conn.execute("SELECT * FROM mindmap_nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        raise ValueError("ノードが見つかりません。マップを再読み込みしてください。")
    linked = conn.execute(
        "SELECT id, kind FROM items WHERE id = ?", (row["linked_item_id"],)
    ).fetchone()
    if linked is not None and linked["kind"] == kind:
        return int(linked["id"])
    text = row["title"]
    if kind == "memo" and row["body"]:
        text = f"{text}\n\n{row['body']}"
    item_id = insert_item_in_transaction(
        conn,
        kind=kind,
        raw_text=text,
        tags="mindmap",
        due_date=due_date if kind == "task" else None,
        status="inbox",
        ai_category=kind,
        importance=3 if kind == "task" else None,
        urgency=3 if kind == "task" else None,
    )
    conn.execute(
        "UPDATE mindmap_nodes SET linked_item_id = ?, updated_at = ? WHERE id = ?",
        (item_id, dt.datetime.now().isoformat(timespec="seconds"), node_id),
    )
    return item_id


def ensure_task_item_for_node(node: MindmapNode, due_date: str | None = None) -> int:
    """ノードを期限付きタスクへ変換し、再試行でも二重登録しない。"""
    due_date = _validate_due_date(due_date)
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        item_id = _ensure_item_in_transaction(conn, node.id, "task", due_date)
    node.linked_item_id = item_id
    return item_id


def ensure_memo_item_for_node(node: MindmapNode) -> int:
    """ノードの表題と本文をローカルメモとして保存しリンクする。"""
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        item_id = _ensure_item_in_transaction(conn, node.id, "memo")
    node.linked_item_id = item_id
    return item_id


def flatten_nodes(nodes: List[MindmapNode]) -> List[MindmapNode]:
    """入れ子のノードを、入力順を保ち循環を除いて平坦化する。"""
    result: List[MindmapNode] = []
    visited: set[int] = set()
    stack = list(reversed(nodes))
    while stack:
        node = stack.pop()
        if node.id in visited:
            continue
        visited.add(node.id)
        result.append(node)
        stack.extend(reversed(node.children))
    return result


def _node_context(node: MindmapNode) -> Dict[str, Any]:
    """AI に渡すノード情報を必要最小限の辞書へ変換する。"""
    return {"id": node.id, "kind": node.kind, "title": node.title, "body": node.body}


def get_expansion_context(nodes: List[MindmapNode], node_id: int) -> Dict[str, Any]:
    """ルートからの経路・兄弟・既存の子を返し、展開時の文脈喪失を防ぐ。"""
    flat = flatten_nodes(nodes)
    node_map = {node.id: node for node in flat}
    if node_id not in node_map:
        raise ValueError("展開対象のノードが見つかりません。")
    parent_map = {child.id: parent.id for parent in flat for child in parent.children}
    node = node_map[node_id]
    path: List[Dict[str, Any]] = []
    current: MindmapNode | None = node
    visited: set[int] = set()
    while current is not None and current.id not in visited:
        visited.add(current.id)
        path.append(_node_context(current))
        parent_id = parent_map.get(current.id, current.parent_id)
        current = node_map.get(parent_id)
    parent_id = parent_map.get(node.id, node.parent_id)
    parent = node_map.get(parent_id)
    sibling_nodes = parent.children if parent is not None else nodes
    return {
        "path": list(reversed(path)),
        "siblings": [_node_context(item) for item in sibling_nodes if item.id != node_id],
        "existing_children": [_node_context(item) for item in node.children],
    }


def _escape_outline(text: str) -> str:
    """改行と Markdown・HTML の記号を無害化してアウトラインの構造を守る。"""
    value = " ".join(text.splitlines())
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|<>~$])", r"\\\1", value)


def export_markdown(nodes: List[MindmapNode]) -> str:
    """折りたたみ状態に依存せず、マップ全体を Markdown アウトラインへ変換する。"""
    lines: List[str] = []
    visited: set[int] = set()
    stack: List[Tuple[MindmapNode, int]] = [(node, 0) for node in reversed(nodes)]
    labels = {"root": "テーマ", "idea": "アイデア", "task": "タスク", "detail": "手順", "note": "メモ・問い", "link": "リンク"}
    while stack:
        node, depth = stack.pop()
        if node.id in visited:
            continue
        visited.add(node.id)
        indent = "  " * depth
        label = labels.get(node.kind, node.kind)
        lines.append(f"{indent}- [{label}] {_escape_outline(node.title)}")
        if node.body:
            lines.append(f"{indent}  {_escape_outline(node.body)}")
        stack.extend((child, depth + 1) for child in reversed(node.children))
    return "\n".join(lines) + ("\n" if lines else "")


def create_tasks_from_action_plan(
    project_id: int, actions: List[Dict[str, Any]]
) -> List[int]:
    """承認された候補を原子的にノード・タスクへ保存し、同じ表題の再登録を防ぐ。

    due_hint は説明として保存するだけで、確定期限には変換しない。
    due_date に明示された日付のみタスクの期限へ反映する。
    """
    if not isinstance(actions, list) or len(actions) > MAX_EXPANSION_ITEMS:
        raise ValueError("アクションプランは一度に8件まで選択してください。")
    prepared: List[Tuple[str, str, str | None]] = []
    seen: set[str] = set()
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("タスク候補の形式が不正です。")
        title = action.get("title", "")
        reason = action.get("reason", "")
        hint = action.get("due_hint", "")
        if not all(isinstance(value, str) for value in (title, reason, hint)) or not title.strip():
            raise ValueError("タスク候補にはタイトル・理由・期限目安を文字列で指定してください。")
        title = title.strip()
        body = f"理由: {reason}\n期限目安: {hint}".strip()
        if len(title) > MAX_NODE_TITLE_LENGTH or len(body) > MAX_NODE_BODY_LENGTH:
            raise ValueError("タスク候補の内容が長すぎます。")
        due = _validate_due_date(action.get("due_date"))
        identity = normalize_node_title(title)
        if identity not in seen:
            seen.add(identity)
            prepared.append((title, body, due))
    if not prepared:
        return []
    ids: List[int] = []
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        root = conn.execute(
            "SELECT id, depth FROM mindmap_nodes WHERE project_id = ? AND parent_id IS NULL ORDER BY id LIMIT 1",
            (project_id,),
        ).fetchone()
        if root is None:
            raise ValueError("タスク候補を保存するマップが見つかりません。")
        existing = {
            normalize_node_title(row["title"]): int(row["id"])
            for row in conn.execute(
                "SELECT n.id, n.title FROM mindmap_nodes AS n "
                "LEFT JOIN items AS i ON i.id = n.linked_item_id "
                "WHERE n.project_id = ? AND (n.kind = 'task' OR i.kind = 'task') "
                "ORDER BY CASE WHEN i.kind = 'task' THEN 1 ELSE 0 END, n.id DESC",
                (project_id,),
            )
        }
        additional = sum(normalize_node_title(title) not in existing for title, _, _ in prepared)
        _check_node_capacity(conn, project_id, int(root["depth"]) + 1, additional)
        next_order = int(conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) + 1 FROM mindmap_nodes WHERE parent_id = ?",
            (root["id"],),
        ).fetchone()[0])
        now = dt.datetime.now().isoformat(timespec="seconds")
        for title, body, due in prepared:
            identity = normalize_node_title(title)
            node_id = existing.get(identity)
            if node_id is None:
                node_id = _insert_tree_node(
                    conn, project_id,
                    {"kind": "task", "title": title, "body": body, "children": []},
                    int(root["id"]), int(root["depth"]) + 1, next_order, now,
                )
                next_order += 1
                existing[identity] = node_id
            ids.append(_ensure_item_in_transaction(conn, node_id, "task", due))
    return ids


def append_initial_mindmap_children(root_id: int, tree: Dict[str, Any]) -> List[int]:
    """空の手動ルートを保持し、再生成した初期マップの枝を一括保存する。"""
    _validate_tree(tree)
    if not tree["children"]:
        raise ValueError("AI から最初の枝が返されませんでした。")
    children_count = 0
    stack = list(tree["children"])
    while stack:
        item = stack.pop()
        children_count += 1
        stack.extend(item["children"])
    now = dt.datetime.now().isoformat(timespec="seconds")
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        root = conn.execute("SELECT * FROM mindmap_nodes WHERE id = ?", (root_id,)).fetchone()
        if root is None or root["kind"] != "root" or root["parent_id"] is not None:
            raise ValueError("初期生成の対象ルートが見つかりません。")
        if conn.execute("SELECT 1 FROM mindmap_nodes WHERE parent_id = ?", (root_id,)).fetchone():
            raise ValueError("マップは既に編集されています。既存ノードから AI 展開を利用してください。")
        _check_node_capacity(conn, int(root["project_id"]), 1, children_count)
        return [
            _insert_tree_node(conn, int(root["project_id"]), child, root_id, 1, index, now)
            for index, child in enumerate(tree["children"])
        ]
