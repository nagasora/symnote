from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple
import datetime as dt
import sqlite3

from symnote.config import load_config
from symnote.core.nlp import ClassificationResult

if TYPE_CHECKING:
    from symnote.core.weekly_review import WeeklyReviewPayload

ItemRow = Dict[str, Any]


def get_connection() -> sqlite3.Connection:
    """SQLite のコネクションを取得する。存在しない場合はパスを作成。"""
    config = load_config()
    Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """items テーブルを用意する（存在しなければ作成）。"""
    conn = get_connection()
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
              id INTEGER PRIMARY KEY,
              created_at TEXT,
              date TEXT,
              kind TEXT,              -- 'memo' | 'task' | 'weekly_review'
              raw_text TEXT,
              ai_category TEXT,       -- 'task' | 'idea' | 'someday'
              importance INTEGER,     -- 1-5
              urgency INTEGER,        -- 1-5
              effort TEXT,            -- 'short' | 'medium' | 'long'
              energy TEXT,            -- 'low' | 'mid' | 'high'
              status TEXT,            -- 'inbox' | 'today' | 'week' | 'done'
              tags TEXT,              -- comma separated
              embedding BLOB
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_date ON items(date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);")


def insert_memo(raw_text: str, date_str: Optional[str] = None, tags: str = "") -> int:
    """インボックス用のメモを登録する。"""
    if not date_str:
        date_str = dt.date.today().isoformat()
    now = dt.datetime.now().isoformat(timespec="seconds")

    conn = get_connection()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO items (
              created_at, date, kind, raw_text,
              ai_category, importance, urgency, effort, energy,
              status, tags, embedding
            )
            VALUES (?, ?, 'memo', ?, NULL, NULL, NULL, NULL, NULL,
                    'inbox', ?, NULL)
            """,
            (now, date_str, raw_text, tags),
        )
        return int(cur.lastrowid)


def fetch_inbox(limit: int = 50) -> List[ItemRow]:
    """status='inbox' のメモや未処理タスクを新しい順に取得。"""
    conn = get_connection()
    cur = conn.execute(
        """
        SELECT id, created_at, date, raw_text, tags, ai_category
        FROM items
        WHERE status = 'inbox'
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def fetch_tasks(
    statuses: Optional[Sequence[str]] = None,
    limit: int = 200,
) -> List[ItemRow]:
    """タスク(kind='task')を取得。status でフィルタ可能。"""
    conn = get_connection()
    query = [
        "SELECT id, created_at, date, raw_text, tags, ai_category,",
        "importance, urgency, effort, energy, status",
        "FROM items WHERE kind = 'task'",
    ]
    params: List[Any] = []
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        query.append(f"AND status IN ({placeholders})")
        params.extend(statuses)
    query.append("ORDER BY created_at DESC LIMIT ?")
    params.append(limit)

    cur = conn.execute(" ".join(query), params)
    return [dict(r) for r in cur.fetchall()]


def fetch_items_by_date(date_str: str) -> List[ItemRow]:
    """指定日の items を kind/status など含めて取得。"""
    conn = get_connection()
    cur = conn.execute(
        """
        SELECT id, kind, status, raw_text, tags, importance, urgency, effort, energy, ai_category
        FROM items
        WHERE date = ?
        ORDER BY created_at DESC
        """,
        (date_str,),
    )
    return [dict(r) for r in cur.fetchall()]


def fetch_counts_by_date(start_date: str, end_date: str) -> List[Tuple[str, int, int]]:
    """期間内の日付ごとのタスク数/メモ数を集計。"""
    conn = get_connection()
    cur = conn.execute(
        """
        SELECT date,
               SUM(CASE WHEN kind = 'task' THEN 1 ELSE 0 END) AS task_count,
               SUM(CASE WHEN kind = 'memo' THEN 1 ELSE 0 END) AS memo_count
        FROM items
        WHERE date BETWEEN ? AND ?
        GROUP BY date
        ORDER BY date
        """,
        (start_date, end_date),
    )
    return [(r["date"], r["task_count"], r["memo_count"]) for r in cur.fetchall()]


def insert_weekly_review(payload: "WeeklyReviewPayload") -> int:
    """週次振り返りを items に保存して ID を返す。"""
    now = dt.datetime.now().isoformat(timespec="seconds")
    markdown = payload.to_markdown()
    conn = get_connection()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO items (
              created_at, date, kind, raw_text,
              ai_category, importance, urgency, effort, energy,
              status, tags, embedding
            )
            VALUES (?, ?, 'weekly_review', ?, NULL, NULL, NULL, NULL, NULL,
                    'done', NULL, NULL)
            """,
            (now, payload.period_start, markdown),
        )
        return int(cur.lastrowid)


def update_item_fields(item_id: int, **fields: Any) -> None:
    """任意のカラムを更新する。"""
    if not fields:
        return
    allowed = {
        "ai_category",
        "importance",
        "urgency",
        "effort",
        "energy",
        "status",
        "tags",
        "kind",
        "raw_text",
        "date",
    }
    updates: List[str] = []
    params: List[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        updates.append(f"{key} = ?")
        params.append(value)
    if not updates:
        return
    params.append(item_id)
    conn = get_connection()
    with conn:
        conn.execute(
            f"UPDATE items SET {', '.join(updates)} WHERE id = ?", params
        )


@dataclass(frozen=True)
class ClassificationSummary:
    processed: int
    converted_to_task: int
    updated_status_today: int
    updated_status_week: int


def classify_inbox_items(
    classify_fn: Callable[[str], ClassificationResult]
) -> ClassificationSummary:
    """
    未分類のインボックスメモをルールベース分類し、task ならタスク化する。
    戻り値は処理数とステータス変更数の簡易サマリ。
    """
    conn = get_connection()
    cur = conn.execute(
        """
        SELECT id, raw_text
        FROM items
        WHERE status = 'inbox'
          AND ai_category IS NULL
        """
    )
    rows = cur.fetchall()
    if not rows:
        return ClassificationSummary(0, 0, 0, 0)

    processed = 0
    converted_to_task = 0
    updated_status_today = 0
    updated_status_week = 0

    with conn:
        for row in rows:
            processed += 1
            result = classify_fn(row["raw_text"])
            kind = "task" if result.ai_category == "task" else "memo"
            status = "inbox"
            if result.status_suggestion == "today":
                status = "today"
                updated_status_today += 1
            elif result.status_suggestion == "week":
                status = "week"
                updated_status_week += 1

            conn.execute(
                """
                UPDATE items
                SET kind = ?, ai_category = ?, importance = ?, urgency = ?,
                    effort = ?, energy = ?, status = ?
                WHERE id = ?
                """,
                (
                    kind,
                    result.ai_category,
                    result.importance,
                    result.urgency,
                    result.effort,
                    result.energy,
                    status,
                    row["id"],
                ),
            )
            converted_to_task += 1 if kind == "task" else 0

    return ClassificationSummary(
        processed=processed,
        converted_to_task=converted_to_task,
        updated_status_today=updated_status_today,
        updated_status_week=updated_status_week,
    )


def fetch_weekly_review_sources(
    start_date: str, end_date: str
) -> Dict[str, List[ItemRow]]:
    """週次振り返り用に期間内の task/memo をまとめて取得。"""
    conn = get_connection()
    tasks_cur = conn.execute(
        """
        SELECT *
        FROM items
        WHERE date BETWEEN ? AND ?
          AND kind = 'task'
        """,
        (start_date, end_date),
    )
    memos_cur = conn.execute(
        """
        SELECT *
        FROM items
        WHERE date BETWEEN ? AND ?
          AND kind = 'memo'
        """,
        (start_date, end_date),
    )
    return {"tasks": [dict(r) for r in tasks_cur.fetchall()],
            "memos": [dict(r) for r in memos_cur.fetchall()]}
