from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple
from contextlib import contextmanager
import datetime as dt
import sqlite3

from symnote.config import load_config
from symnote.core.nlp import ClassificationResult

if TYPE_CHECKING:
    from symnote.core.weekly_review import WeeklyReviewPayload

ItemRow = Dict[str, Any]
SQLITE_BUSY_TIMEOUT_MS = 5_000
SCHEMA_VERSION = 10


@contextmanager
def connection() -> Any:
    """Yield a configured SQLite connection and close it reliably afterwards."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def get_connection() -> sqlite3.Connection:
    """SQLite のコネクションを取得する。存在しない場合はパスを作成。"""
    config = load_config()
    if config.db_path != ":memory:" and not config.db_path.startswith("file:"):
        Path(config.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.db_path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _migrate_db(conn: sqlite3.Connection) -> None:
    """必要なカラムが足りない場合に ALTER TABLE で追加する簡易マイグレーション。"""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise RuntimeError("This database requires a newer version of SymNote.")

    if current < 1:
        # Version 1 is the original base schema already created by init_db.
        conn.execute("PRAGMA user_version = 1")
        current = 1
    if current < 2:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
        if "due_date" not in columns:
            conn.execute("ALTER TABLE items ADD COLUMN due_date TEXT")
        # Legacy task ``date`` represented its intended date. Preserve that
        # interpretation while retaining date as the historical creation date.
        conn.execute(
            "UPDATE items SET due_date = date WHERE kind = 'task' AND due_date IS NULL"
        )
        conn.execute("PRAGMA user_version = 2")
        current = 2
    if current < 3:
        # Indexes are created below after all current-version tables exist.
        conn.execute("PRAGMA user_version = 3")
        current = 3
    if current < 4:
        # Keep calendar state separate from user-owned tasks.  A task mutation
        # only queues work here; network calls are performed by the sync service.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calendar_task_sync (
              task_id INTEGER PRIMARY KEY,
              event_id TEXT,
              last_synced_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calendar_sync_outbox (
              task_id INTEGER PRIMARY KEY,
              operation TEXT NOT NULL CHECK(operation IN ('upsert', 'delete')),
              changed_at TEXT NOT NULL
            )
            """
        )
        conn.execute("PRAGMA user_version = 4")
        current = 4
    if current < 5:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
        if "recurrence_rule_id" not in columns:
            conn.execute("ALTER TABLE items ADD COLUMN recurrence_rule_id INTEGER")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_recurrence_rules (
              id INTEGER PRIMARY KEY,
              frequency TEXT NOT NULL CHECK(frequency IN ('daily', 'weekly')),
              interval_days INTEGER NOT NULL CHECK(interval_days > 0),
              end_date TEXT,
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
              created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_recurrence_occurrence "
            "ON items(recurrence_rule_id, due_date) WHERE recurrence_rule_id IS NOT NULL"
        )
        conn.execute("PRAGMA user_version = 5")
        current = 5
    if current < 6:
        # Calendar sync was added after tasks already existed in local
        # databases.  Seed those tasks once so connecting Calendar does not
        # appear to succeed while silently syncing nothing.
        conn.execute(
            """
            INSERT INTO calendar_sync_outbox (task_id, operation, changed_at)
            SELECT id, 'upsert', ?
            FROM items
            WHERE kind = 'task'
            ON CONFLICT(task_id) DO NOTHING
            """,
            (dt.datetime.now().isoformat(timespec="seconds"),),
        )
        conn.execute("PRAGMA user_version = 6")
        current = 6
    if current < 7:
        # Keep the day and time separate so existing date-only tasks remain
        # valid while new tasks can carry an exact local deadline.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
        if "due_time" not in columns:
            conn.execute("ALTER TABLE items ADD COLUMN due_time TEXT")
        rule_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_recurrence_rules)")
        }
        if "end_date" not in rule_columns:
            conn.execute("ALTER TABLE task_recurrence_rules ADD COLUMN end_date TEXT")
        conn.execute("PRAGMA user_version = 7")
        current = 7
    if current < 8:
        # Morning digest events are derived delivery state.  Task data remains
        # local and authoritative, while this table lets Calendar events be
        # updated or deleted safely after an interrupted/offline sync.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calendar_morning_digest_sync (
              digest_date TEXT PRIMARY KEY,
              event_id TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              last_synced_at TEXT NOT NULL
            )
            """
        )
        conn.execute("PRAGMA user_version = 8")
        current = 8
    if current < 9:
        # 外部 AI が抽出した ToDo は信頼できない入力なので、items へ直接入れず
        # 人の承認を待つ別テーブルに置く。UNIQUE で同じ出典からの再提案を弾く。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_candidates (
              id INTEGER PRIMARY KEY,
              created_at TEXT NOT NULL,
              source TEXT NOT NULL,
              source_ref TEXT NOT NULL,
              source_url TEXT,
              title TEXT NOT NULL,
              details TEXT NOT NULL DEFAULT '',
              excerpt TEXT NOT NULL DEFAULT '',
              due_date TEXT,
              due_time TEXT,
              confidence REAL,
              status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'approved', 'rejected')),
              decided_at TEXT,
              task_id INTEGER,
              UNIQUE(source, source_ref, title)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_candidates_status "
            "ON task_candidates(status, created_at)"
        )
        conn.execute("PRAGMA user_version = 9")
        current = 9
    if current < 10:
        # 対象カレンダーを変えたとき、旧カレンダーの朝のまとめを消せるように記録する。
        # 既存行は NULL のままにし、同期時に「現在の設定のカレンダー」とみなす。
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(calendar_morning_digest_sync)")
        }
        if "calendar_id" not in columns:
            conn.execute("ALTER TABLE calendar_morning_digest_sync ADD COLUMN calendar_id TEXT")
        conn.execute("PRAGMA user_version = 10")


def init_db() -> None:
    """items テーブルを用意する（存在しなければ作成）。"""
    with connection() as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
              id INTEGER PRIMARY KEY,
              created_at TEXT,
              date TEXT,
              due_date TEXT,
              due_time TEXT,
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
        _migrate_db(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_date ON items(date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_due_date ON items(due_date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_calendar_outbox_changed ON calendar_sync_outbox(changed_at);")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_calendar_morning_digest_synced "
            "ON calendar_morning_digest_sync(last_synced_at);"
        )

        # idea_sessions table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idea_sessions (
                id INTEGER PRIMARY KEY,
                title TEXT,
                summary TEXT,
                source_text TEXT,
                chat_history TEXT, -- JSON string
                created_at TEXT,
                updated_at TEXT
            );
            """
        )

        # mindmap_nodes table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mindmap_nodes (
              id INTEGER PRIMARY KEY,
              project_id INTEGER NOT NULL,    -- 1 mindmap = 1 project (usually root node id or session id)
              parent_id INTEGER,              -- NULL = root
              kind TEXT NOT NULL,             -- 'root' | 'idea' | 'task' | 'detail' | 'note' | 'link'
              title TEXT NOT NULL,
              body TEXT,
              order_index INTEGER DEFAULT 0,
              depth INTEGER DEFAULT 0,
              linked_item_id INTEGER,         -- items.id external reference
              created_at TEXT,
              updated_at TEXT
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mindmap_project ON mindmap_nodes(project_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mindmap_parent ON mindmap_nodes(parent_id);")
        


def insert_item(
    kind: str,
    raw_text: str,
    date_str: Optional[str] = None,
    tags: str = "",
    due_date: Optional[str] = None,
    due_time: Optional[str] = None,
    status: str = "inbox",
    ai_category: Optional[str] = None,
    importance: Optional[int] = None,
    urgency: Optional[int] = None,
    effort: Optional[str] = None,
    energy: Optional[str] = None,
    recurrence_rule_id: Optional[int] = None,
) -> int:
    """汎用的なアイテム登録。"""
    with connection() as conn, conn:
        return insert_item_in_transaction(
            conn,
            kind=kind,
            raw_text=raw_text,
            date_str=date_str,
            tags=tags,
            due_date=due_date,
            due_time=due_time,
            status=status,
            ai_category=ai_category,
            importance=importance,
            urgency=urgency,
            effort=effort,
            energy=energy,
            recurrence_rule_id=recurrence_rule_id,
        )


def insert_item_in_transaction(
    conn: sqlite3.Connection,
    kind: str,
    raw_text: str,
    date_str: Optional[str] = None,
    tags: str = "",
    due_date: Optional[str] = None,
    due_time: Optional[str] = None,
    status: str = "inbox",
    ai_category: Optional[str] = None,
    importance: Optional[int] = None,
    urgency: Optional[int] = None,
    effort: Optional[str] = None,
    energy: Optional[str] = None,
    recurrence_rule_id: Optional[int] = None,
) -> int:
    """呼び出し側のトランザクション内でアイテムを登録し、その ID を返す。"""
    if not date_str:
        date_str = dt.date.today().isoformat()
    if due_time:
        try:
            due_time = dt.time.fromisoformat(due_time).strftime("%H:%M")
        except ValueError as exc:
            raise ValueError("due_time must be an ISO time") from exc
    now = dt.datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        """
        INSERT INTO items (
          created_at, date, due_date, due_time, kind, raw_text,
          ai_category, importance, urgency, effort, energy,
          status, tags, embedding, recurrence_rule_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            now,
            date_str,
            due_date,
            due_time,
            kind,
            raw_text,
            ai_category,
            importance,
            urgency,
            effort,
            energy,
            status,
            tags,
            recurrence_rule_id,
        ),
    )
    item_id = int(cur.lastrowid)
    if kind == "task":
        _queue_calendar_sync(conn, item_id, "upsert")
    return item_id


def insert_memo(raw_text: str, date_str: Optional[str] = None, tags: str = "") -> int:
    """インボックス用のメモを登録する。"""
    return insert_item(kind="memo", raw_text=raw_text, date_str=date_str, tags=tags)


def insert_task(
    raw_text: str,
    date_str: Optional[str] = None,
    tags: str = "",
    due_date: Optional[str] = None,
    due_time: Optional[str] = None,
    status: str = "inbox",
    recurrence_rule_id: Optional[int] = None,
) -> int:
    """タスクを直接登録する。"""
    task_id = insert_item(
        kind="task",
        raw_text=raw_text,
        date_str=date_str,
        tags=tags,
        due_date=due_date,
        due_time=due_time,
        status=status,
        ai_category="task",
        importance=3,
        urgency=3,
        recurrence_rule_id=recurrence_rule_id,
    )
    return task_id


def create_recurring_task(
    raw_text: str,
    due_date: str,
    frequency: str,
    interval: int = 1,
    tags: str = "",
    status: str = "inbox",
    due_time: Optional[str] = None,
    end_date: Optional[str] = None,
) -> int:
    """Create the first occurrence of a daily, weekly, or biweekly task."""
    if frequency not in {"daily", "weekly"} or interval < 1:
        raise ValueError("Unsupported recurrence rule")
    start_date = dt.date.fromisoformat(due_date)
    if due_time:
        try:
            due_time = dt.time.fromisoformat(due_time).strftime("%H:%M")
        except ValueError as exc:
            raise ValueError("due_time must be an ISO time") from exc
    if end_date:
        recurrence_end = dt.date.fromisoformat(end_date)
        if recurrence_end < start_date:
            raise ValueError("end_date must not be before due_date")
    now = dt.datetime.now().isoformat(timespec="seconds")
    interval_days = interval if frequency == "daily" else interval * 7
    with connection() as conn, conn:
        rule = conn.execute(
            """
            INSERT INTO task_recurrence_rules (frequency, interval_days, end_date, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (frequency, interval_days, end_date, now),
        )
        rule_id = int(rule.lastrowid)
        cur = conn.execute(
            """
            INSERT INTO items (
              created_at, date, due_date, due_time, kind, raw_text, ai_category,
              importance, urgency, effort, energy, status, tags, embedding,
              recurrence_rule_id
            ) VALUES (?, ?, ?, ?, 'task', ?, 'task', 3, 3, NULL, NULL, ?, ?, NULL, ?)
            """,
            (now, dt.date.today().isoformat(), due_date, due_time, raw_text, status, tags, rule_id),
        )
        task_id = int(cur.lastrowid)
        _queue_calendar_sync(conn, task_id, "upsert")
        return task_id


def recurrence_label(task: ItemRow) -> Optional[str]:
    """Return a compact user-facing cadence label for a task occurrence."""
    interval_days = task.get("recurrence_interval_days")
    if not interval_days:
        return None
    if interval_days == 1:
        label = "毎日"
    elif interval_days == 7:
        label = "毎週"
    elif interval_days == 14:
        label = "隔週"
    else:
        label = f"{interval_days}日ごと"
    if end_date := task.get("recurrence_end_date"):
        return f"{label}（{end_date}まで）"
    return label


def complete_task(item_id: int) -> Optional[int]:
    """Complete an occurrence and atomically create its next occurrence, if any."""
    with connection() as conn, conn:
        return _complete_task_in_transaction(conn, item_id)


def _complete_task_in_transaction(conn: sqlite3.Connection, item_id: int) -> Optional[int]:
    """呼び出し側のトランザクション内でタスクを完了し、繰り返しの次回分を作成する。"""
    task = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not task or task["kind"] != "task" or task["status"] == "done":
        return None
    conn.execute("UPDATE items SET status = 'done' WHERE id = ?", (item_id,))
    _queue_calendar_sync(conn, item_id, "upsert")
    rule_id = task["recurrence_rule_id"]
    if rule_id is None:
        return None
    rule = conn.execute(
        "SELECT interval_days, end_date FROM task_recurrence_rules WHERE id = ? AND active = 1",
        (rule_id,),
    ).fetchone()
    if not rule:
        return None
    try:
        base_due = dt.date.fromisoformat(task["due_date"] or dt.date.today().isoformat())
    except ValueError:
        base_due = dt.date.today()
    next_due = (base_due + dt.timedelta(days=rule["interval_days"])).isoformat()
    if rule["end_date"] and next_due > rule["end_date"]:
        conn.execute("UPDATE task_recurrence_rules SET active = 0 WHERE id = ?", (rule_id,))
        return None
    existing = conn.execute(
        "SELECT id FROM items WHERE recurrence_rule_id = ? AND due_date = ?",
        (rule_id, next_due),
    ).fetchone()
    if existing:
        return int(existing["id"])
    now = dt.datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        """
        INSERT INTO items (
          created_at, date, due_date, due_time, kind, raw_text, ai_category,
          importance, urgency, effort, energy, status, tags, embedding,
          recurrence_rule_id
        ) VALUES (?, ?, ?, ?, 'task', ?, ?, ?, ?, ?, ?, 'inbox', ?, NULL, ?)
        """,
        (
            now,
            dt.date.today().isoformat(),
            next_due,
            task["due_time"],
            task["raw_text"],
            task["ai_category"],
            task["importance"],
            task["urgency"],
            task["effort"],
            task["energy"],
            task["tags"],
            rule_id,
        ),
    )
    next_id = int(cur.lastrowid)
    _queue_calendar_sync(conn, next_id, "upsert")
    return next_id


def stop_task_recurrence(item_id: int) -> None:
    """Stop generating future occurrences while preserving all task history."""
    with connection() as conn, conn:
        row = conn.execute(
            "SELECT recurrence_rule_id FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row and row["recurrence_rule_id"] is not None:
            conn.execute(
                "UPDATE task_recurrence_rules SET active = 0 WHERE id = ?",
                (row["recurrence_rule_id"],),
            )

def insert_standalone_memo(
    raw_text: str,
    date_str: Optional[str] = None,
    tags: str = "",
) -> int:
    """メモを直接登録する。"""
    return insert_item(
        kind="memo",
        raw_text=raw_text,
        date_str=date_str,
        tags=tags,
        status="inbox",
        ai_category="memo",
    )


def delete_item(item_id: int) -> None:
    """アイテムを削除する。"""
    with connection() as conn, conn:
        row = conn.execute("SELECT kind FROM items WHERE id = ?", (item_id,)).fetchone()
        if row and row["kind"] == "task":
            # Retain the event mapping after the local task disappears so an
            # interrupted/offline delete is retried on the next sync.
            _queue_calendar_sync(conn, item_id, "delete")
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))


def fetch_inbox(limit: int = 50) -> List[ItemRow]:
    """status='inbox' のメモや未処理タスクを新しい順に取得。"""
    with connection() as conn:
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


def fetch_memos(limit: int = 100) -> List[ItemRow]:
    """Return standalone memos without exposing the internal inbox status."""
    with connection() as conn:
        cur = conn.execute(
            """
            SELECT id, created_at, date, raw_text, tags, ai_category
            FROM items
            WHERE kind = 'memo'
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
    query = [
        "SELECT i.id, i.kind, i.created_at, i.date, i.due_date, i.due_time, i.raw_text, i.tags,",
        "i.ai_category,",
        "i.importance, i.urgency, i.effort, i.energy, i.status, i.recurrence_rule_id,",
        "r.interval_days AS recurrence_interval_days, r.end_date AS recurrence_end_date",
        "FROM items AS i LEFT JOIN task_recurrence_rules AS r ON r.id = i.recurrence_rule_id",
        "WHERE i.kind = 'task'",
    ]
    params: List[Any] = []
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        query.append(f"AND i.status IN ({placeholders})")
        params.extend(statuses)
    query.append("ORDER BY i.created_at DESC LIMIT ?")
    params.append(limit)

    with connection() as conn:
        cur = conn.execute(" ".join(query), params)
        return [dict(r) for r in cur.fetchall()]


def fetch_items_by_date(date_str: str) -> List[ItemRow]:
    """指定日の items を kind/status など含めて取得。"""
    with connection() as conn:
        cur = conn.execute(
        """
        SELECT id, kind, status, raw_text, tags, importance, urgency, effort, energy, ai_category, due_date, date
        FROM items
        WHERE date = ?
        ORDER BY created_at DESC
        """,
        (date_str,),
        )
        return [dict(r) for r in cur.fetchall()]


def fetch_tasks_due_on(date_str: str) -> List[ItemRow]:
    """指定日を期限とするタスクを取得。"""
    with connection() as conn:
        cur = conn.execute(
        """
        SELECT id, kind, status, raw_text, tags, importance, urgency, effort, energy, ai_category, due_date, date
        FROM items
        WHERE due_date = ? AND kind = 'task'
        ORDER BY created_at DESC
        """,
        (date_str,),
        )
        return [dict(r) for r in cur.fetchall()]


def fetch_tasks_for_today_view(today_str: str) -> List[ItemRow]:
    """
    今日ビューに表示すべきタスクを取得する。
    条件:
    1. kind = 'task'
    2. status != 'done'
    3. (status = 'today') OR (date <= today_str)
    """
    with connection() as conn:
        cur = conn.execute(
        """
        SELECT id, created_at, date, due_date, raw_text, tags, ai_category,
               importance, urgency, effort, energy, status
        FROM items
        WHERE kind = 'task'
          AND status != 'done'
          AND (status = 'today' OR (due_date IS NOT NULL AND due_date <= ?))
        ORDER BY 
            CASE WHEN status = 'today' THEN 0 ELSE 1 END,
            CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,
            due_date ASC,
            importance DESC
        """,
        (today_str,),
        )
        return [dict(r) for r in cur.fetchall()]


def fetch_tasks_for_morning_digest(target_date: str) -> List[ItemRow]:
    """Return unfinished tasks due on or before ``target_date``.

    The Calendar digest includes overdue tasks so they remain visible, but
    excludes future and status-only "today" tasks.
    """
    with connection() as conn:
        cur = conn.execute(
            """
            SELECT id, raw_text, tags, due_date
            FROM items
            WHERE kind = 'task'
              AND status != 'done'
              AND due_date IS NOT NULL
              AND due_date <= ?
            ORDER BY due_date ASC, id ASC
            """,
            (target_date,),
        )
        return [dict(r) for r in cur.fetchall()]


def fetch_counts_by_date(start_date: str, end_date: str) -> List[Tuple[str, int, int]]:
    """期間内の日付ごとのタスク数/メモ数を集計。"""
    with connection() as conn:
        cur = conn.execute(
        """
        SELECT CASE WHEN kind = 'task' THEN due_date ELSE date END AS display_date,
               SUM(CASE WHEN kind = 'task' AND status != 'done' THEN 1 ELSE 0 END) AS task_count,
               SUM(CASE WHEN kind = 'memo' THEN 1 ELSE 0 END) AS memo_count
        FROM items
        WHERE (CASE WHEN kind = 'task' THEN due_date ELSE date END) BETWEEN ? AND ?
        GROUP BY display_date
        ORDER BY display_date
        """,
        (start_date, end_date),
        )
        return [(r["display_date"], r["task_count"], r["memo_count"]) for r in cur.fetchall()]


def fetch_due_date_counts_by_date(start_date: str, end_date: str) -> List[Tuple[str, int]]:
    """期間内の日付ごとの期限タスク数を集計。"""
    with connection() as conn:
        cur = conn.execute(
        """
        SELECT due_date, COUNT(id) AS due_task_count
        FROM items
        WHERE due_date BETWEEN ? AND ?
          AND kind = 'task'
          AND status != 'done'
        GROUP BY due_date
        ORDER BY due_date
        """,
        (start_date, end_date),
        )
        return [(r["due_date"], r["due_task_count"]) for r in cur.fetchall()]



def insert_weekly_review(payload: "WeeklyReviewPayload") -> int:
    """週次振り返りを items に保存して ID を返す。"""
    now = dt.datetime.now().isoformat(timespec="seconds")
    markdown = payload.to_markdown()
    with connection() as conn, conn:
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
        "due_date",
        "due_time",
    }
    updates: List[str] = []
    params: List[Any] = []
    completes = False
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "status" and value == "done":
            # 完了は complete_task と同じ経路を通し、繰り返しの次回分を作らせる。
            completes = True
            continue
        updates.append(f"{key} = ?")
        params.append(value)
    if not updates and not completes:
        return
    params.append(item_id)
    with connection() as conn, conn:
        if updates:
            conn.execute(f"UPDATE items SET {', '.join(updates)} WHERE id = ?", params)
        row = conn.execute("SELECT kind FROM items WHERE id = ?", (item_id,)).fetchone()
        if completes:
            if row and row["kind"] == "task":
                _complete_task_in_transaction(conn, item_id)
            else:
                conn.execute("UPDATE items SET status = 'done' WHERE id = ?", (item_id,))
        if row and row["kind"] == "task":
            _queue_calendar_sync(conn, item_id, "upsert")


def _queue_calendar_sync(conn: sqlite3.Connection, task_id: int, operation: str) -> None:
    """Record local task changes for later Calendar delivery, never doing I/O here."""
    conn.execute(
        """
        INSERT INTO calendar_sync_outbox (task_id, operation, changed_at)
        VALUES (?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
          operation = excluded.operation,
          changed_at = excluded.changed_at
        """,
        (task_id, operation, dt.datetime.now().isoformat(timespec="seconds")),
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
    with connection() as conn, conn:
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
            if kind == "task":
                _queue_calendar_sync(conn, row["id"], "upsert")
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
    with connection() as conn:
        tasks_cur = conn.execute(
            """
            SELECT *
            FROM items
            WHERE kind = 'task'
              AND (date BETWEEN ? AND ? OR due_date BETWEEN ? AND ?)
            """,
            (start_date, end_date, start_date, end_date),
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


def create_idea_session(title: str, summary: str, source_text: str, chat_history: str = "[]") -> int:
    """新しいアイデアセッションを作成する。"""
    now = dt.datetime.now().isoformat(timespec="seconds")
    with connection() as conn, conn:
        cur = conn.execute(
            """
            INSERT INTO idea_sessions (title, summary, source_text, chat_history, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (title, summary, source_text, chat_history, now, now),
        )
        return cur.lastrowid


def update_idea_session_chat(session_id: int, chat_history: str) -> None:
    """アイデアセッションのチャット履歴を更新する。"""
    now = dt.datetime.now().isoformat(timespec="seconds")
    with connection() as conn, conn:
        conn.execute(
            """
            UPDATE idea_sessions
            SET chat_history = ?, updated_at = ?
            WHERE id = ?
            """,
            (chat_history, now, session_id),
        )


def fetch_idea_sessions() -> List[ItemRow]:
    """アイデアセッション一覧を取得する（更新日時順）。"""
    with connection() as conn:
        cur = conn.execute(
            """
            SELECT id, title, summary, updated_at
            FROM idea_sessions
            ORDER BY updated_at DESC
            """
        )
        return [dict(r) for r in cur.fetchall()]


def get_idea_session(session_id: int) -> Optional[ItemRow]:
    """指定されたIDのアイデアセッションを取得する。"""
    with connection() as conn:
        cur = conn.execute(
            """
            SELECT *
            FROM idea_sessions
            WHERE id = ?
            """,
            (session_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def delete_idea_session(session_id: int) -> None:
    """アイデアセッションを削除する。"""
    with connection() as conn, conn:
        conn.execute("DELETE FROM mindmap_nodes WHERE project_id = ?", (session_id,))
        conn.execute("DELETE FROM idea_sessions WHERE id = ?", (session_id,))


def search_items(
    query: str,
    kinds: Sequence[str] = ("task", "memo"),
    include_done: bool = False,
    limit: int = 20,
) -> List[ItemRow]:
    """本文とタイトル（tags）の部分一致でタスク・メモを検索する。"""
    terms = [term for term in query.split() if term]
    if not terms or not kinds:
        return []
    clauses: List[str] = [f"kind IN ({','.join('?' for _ in kinds)})"]
    params: List[Any] = list(kinds)
    if not include_done:
        clauses.append("COALESCE(status, '') != 'done'")
    for term in terms:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append(
            "(raw_text LIKE ? ESCAPE '\\' OR COALESCE(tags, '') LIKE ? ESCAPE '\\')"
        )
        params.extend([f"%{escaped}%", f"%{escaped}%"])
    params.append(max(1, min(int(limit), 100)))
    with connection() as conn:
        cur = conn.execute(
            f"""
            SELECT id, kind, created_at, date, due_date, due_time, raw_text, tags, status
            FROM items
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            params,
        )
        return [dict(row) for row in cur.fetchall()]
