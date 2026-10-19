"""SQLite にタスクとメモを保存し、版管理された移行を提供する。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple
from contextlib import contextmanager
import datetime as dt
import sqlite3

from symnote.config import load_config
from symnote.core.recurrence import RecurrenceRule, first_occurrence, next_occurrence, rule_label

if TYPE_CHECKING:
    # 型注釈専用。実行時に nlp を読み込むと Gemini SDK の読み込みで起動が数秒以上遅れ、
    # AI を使わない MCP サーバーや定期同期の起動待ちがタイムアウトするため。
    from symnote.core.nlp import ClassificationResult
    from symnote.core.weekly_review import WeeklyReviewPayload

ItemRow = Dict[str, Any]
SQLITE_BUSY_TIMEOUT_MS = 5_000
SCHEMA_VERSION = 16


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
        current = 10
    if current < 11:
        # claude.ai などのリモート MCP クライアントの登録情報と発行済みトークン。
        # トークンは平文で持たず SHA-256 で照合し、DB が漏れても再利用されないようにする。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_clients (
              client_id TEXT PRIMARY KEY,
              client_info TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_tokens (
              token_hash TEXT PRIMARY KEY,
              kind TEXT NOT NULL CHECK(kind IN ('access', 'refresh')),
              client_id TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
              scopes TEXT NOT NULL DEFAULT '',
              resource TEXT,
              expires_at INTEGER NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_oauth_tokens_client ON oauth_tokens(client_id, kind)"
        )
        conn.execute("PRAGMA user_version = 11")
        current = 11
    if current < 12:
        # 使用済みリフレッシュトークンを消さずに印を付け、
        # 再提示（盗用の兆候）を検知できるようにする。
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(oauth_tokens)")}
        if "consumed_at" not in columns:
            conn.execute("ALTER TABLE oauth_tokens ADD COLUMN consumed_at INTEGER")
        conn.execute("PRAGMA user_version = 12")
        current = 12
    if current < 13:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS calendar_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calendar_event_cache (
              calendar_id TEXT NOT NULL, range_start TEXT NOT NULL, range_end TEXT NOT NULL,
              fetched_at TEXT NOT NULL, payload TEXT NOT NULL,
              PRIMARY KEY(calendar_id, range_start, range_end)
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(calendar_task_sync)")}
        for name in ("calendar_id", "content_hash"):
            if name not in columns:
                conn.execute(f"ALTER TABLE calendar_task_sync ADD COLUMN {name} TEXT")
        conn.execute("PRAGMA user_version = 13")
        current = 13
    if current < 14:
        # ゴールツリー：すべてのタスクを「なぜやるか」に結び付け、並行する仕事を一目で追えるようにする。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS goals (
              id INTEGER PRIMARY KEY,
              parent_id INTEGER REFERENCES goals(id) ON DELETE CASCADE,
              title TEXT NOT NULL,
              why TEXT NOT NULL DEFAULT '',
              success_criteria TEXT NOT NULL DEFAULT '',
              target_date TEXT,
              status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'done', 'archived')),
              order_index INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_parent ON goals(parent_id)")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
        # Why not 外部キー付きの列: SQLite の ALTER TABLE ADD COLUMN では ON DELETE を後付けできず、
        # ゴール削除時の付け替えは goals モジュールで明示的に行う。
        if "goal_id" not in columns:
            conn.execute("ALTER TABLE items ADD COLUMN goal_id INTEGER")
        if "estimate_minutes" not in columns:
            conn.execute("ALTER TABLE items ADD COLUMN estimate_minutes INTEGER")
        if "completed_at" not in columns:
            conn.execute("ALTER TABLE items ADD COLUMN completed_at TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_goal ON items(goal_id)")
        # クエスト（ゲーム要素）：完了 1 件につき 1 行。task_id を一意にして二重付与を防ぐ。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quest_log (
              id INTEGER PRIMARY KEY,
              task_id INTEGER NOT NULL UNIQUE,
              title TEXT NOT NULL,
              xp INTEGER NOT NULL CHECK(xp >= 0),
              gold INTEGER NOT NULL CHECK(gold >= 0),
              bonus TEXT NOT NULL DEFAULT '',
              completed_at TEXT NOT NULL,
              seen INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_quest_log_completed ON quest_log(completed_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rewards (
              id INTEGER PRIMARY KEY,
              title TEXT NOT NULL,
              cost INTEGER NOT NULL CHECK(cost > 0),
              icon TEXT NOT NULL DEFAULT '🎁',
              active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reward_redemptions (
              id INTEGER PRIMARY KEY,
              reward_id INTEGER NOT NULL REFERENCES rewards(id),
              title TEXT NOT NULL,
              cost INTEGER NOT NULL CHECK(cost > 0),
              redeemed_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS achievements (
              code TEXT PRIMARY KEY,
              unlocked_at TEXT NOT NULL,
              seen INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # 秘書キャラクターなどアプリ全体の小さな設定（キー・値）。
        conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS secretary_messages (
              id INTEGER PRIMARY KEY,
              role TEXT NOT NULL CHECK(role IN ('user', 'secretary')),
              content TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("PRAGMA user_version = 14")
        current = 14
    if current < 15:
        # 時間割：見積もりとカレンダーの空きから組んだ今日のブロック。承認後にカレンダーへ書き出せる。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS time_blocks (
              id INTEGER PRIMARY KEY,
              day TEXT NOT NULL,
              task_id INTEGER,
              title TEXT NOT NULL,
              start_time TEXT NOT NULL,
              end_time TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned', 'exported', 'cancelled')),
              calendar_event_id TEXT,
              created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_time_blocks_day ON time_blocks(day)")
        # フォーカスモード：1 回の集中セッション。完走したらクエスト報酬にボーナスを足す。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS focus_sessions (
              id INTEGER PRIMARY KEY,
              task_id INTEGER,
              title TEXT NOT NULL,
              planned_minutes INTEGER NOT NULL CHECK(planned_minutes > 0),
              started_at TEXT NOT NULL,
              ended_at TEXT,
              outcome TEXT CHECK(outcome IN ('done', 'stopped')),
              bonus_xp INTEGER NOT NULL DEFAULT 0 CHECK(bonus_xp >= 0),
              bonus_gold INTEGER NOT NULL DEFAULT 0 CHECK(bonus_gold >= 0)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_focus_sessions_started ON focus_sessions(started_at)")
        # AI エージェントからの進捗報告。人が承認したものだけをタスク・ゴールへ反映する。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_reports (
              id INTEGER PRIMARY KEY,
              task_id INTEGER,
              goal_id INTEGER,
              agent TEXT NOT NULL DEFAULT '',
              summary TEXT NOT NULL,
              details TEXT NOT NULL DEFAULT '',
              suggested_status TEXT NOT NULL DEFAULT 'progress'
                CHECK(suggested_status IN ('progress', 'done', 'blocked')),
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
              created_at TEXT NOT NULL,
              reviewed_at TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_reports_status ON agent_reports(status)")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
        if "delegated_to" not in columns:
            conn.execute("ALTER TABLE items ADD COLUMN delegated_to TEXT")
        conn.execute("PRAGMA user_version = 15")
        current = 15
    if current < 16:
        conn.execute(
            """
            CREATE TABLE task_recurrence_rules_v16 (
              id INTEGER PRIMARY KEY,
              frequency TEXT NOT NULL CHECK(frequency IN ('daily', 'weekly', 'weekdays', 'monthly', 'custom')),
              interval_days INTEGER NOT NULL CHECK(interval_days > 0),
              end_date TEXT,
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
              created_at TEXT NOT NULL,
              weekdays TEXT NOT NULL DEFAULT '',
              month_day INTEGER CHECK(month_day BETWEEN 1 AND 31)
            )
            """
        )
        conn.execute(
            "INSERT INTO task_recurrence_rules_v16 "
            "(id, frequency, interval_days, end_date, active, created_at) "
            "SELECT id, frequency, interval_days, end_date, active, created_at FROM task_recurrence_rules"
        )
        conn.execute("DROP TABLE task_recurrence_rules")
        conn.execute("ALTER TABLE task_recurrence_rules_v16 RENAME TO task_recurrence_rules")
        conn.execute("PRAGMA user_version = 16")


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
    weekdays: Tuple[int, ...] = (),
    month_day: Optional[int] = None,
) -> int:
    """繰り返し規則と最初のタスクを同一トランザクションで作成する。"""
    start_date = dt.date.fromisoformat(due_date)
    interval_days = interval * 7 if frequency == "weekly" else interval
    rule_settings = RecurrenceRule(
        frequency,
        interval_days,
        weekdays,
        (start_date.day if month_day is None else month_day) if frequency == "monthly" else None,
    )
    start_date = first_occurrence(start_date, rule_settings)
    due_date = start_date.isoformat()
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
    with connection() as conn, conn:
        rule = conn.execute(
            """
            INSERT INTO task_recurrence_rules (frequency, interval_days, end_date, created_at, weekdays, month_day)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                frequency,
                interval_days,
                end_date,
                now,
                ",".join(map(str, sorted(set(weekdays)))),
                rule_settings.month_day,
            ),
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
    """停止状態も含めた繰り返し規則の表示名を返す。"""
    interval_days = task.get("recurrence_interval_days")
    if not interval_days:
        return None
    rule = RecurrenceRule(
        task.get("recurrence_frequency") or ("weekly" if interval_days in (7, 14) else "daily"),
        interval_days,
        tuple(int(day) for day in (task.get("recurrence_weekdays") or "").split(",") if day),
        task.get("recurrence_month_day"),
    )
    label = rule_label(rule)
    if end_date := task.get("recurrence_end_date"):
        label += f"（{end_date}まで）"
    if task.get("recurrence_active") == 0:
        label += "（停止中）"
    return label


def _set_task_recurrence_in_transaction(
    conn: sqlite3.Connection,
    item_id: int,
    rule: Optional[RecurrenceRule],
    end_date: Optional[str] = None,
) -> None:
    """履歴との関連を維持し、既存タスクの規則を設定・変更・停止する。"""
    task = conn.execute("SELECT * FROM items WHERE id = ? AND kind = 'task'", (item_id,)).fetchone()
    if not task:
        raise ValueError("タスクが見つかりません。")
    rule_id = task["recurrence_rule_id"]
    if rule is None:
        if rule_id:
            conn.execute("UPDATE task_recurrence_rules SET active = 0 WHERE id = ?", (rule_id,))
        return
    due = first_occurrence(
        dt.date.fromisoformat(task["due_date"]) if task["due_date"] else dt.date.today(), rule
    )
    if end_date and dt.date.fromisoformat(end_date) < due:
        raise ValueError("繰り返し終了日は最初の期限日以降にしてください。")
    values = (
        rule.frequency,
        rule.interval_days,
        end_date,
        ",".join(map(str, sorted(set(rule.weekdays)))),
        rule.month_day,
    )
    if rule_id:
        conn.execute(
            "UPDATE task_recurrence_rules SET frequency=?, interval_days=?, end_date=?, weekdays=?, month_day=?, active=1 WHERE id=?",
            (*values, rule_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO task_recurrence_rules (frequency, interval_days, end_date, weekdays, month_day, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (*values, dt.datetime.now().isoformat(timespec="seconds")),
        )
        rule_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE items SET recurrence_rule_id=?, due_date=? WHERE id=?",
        (rule_id, due.isoformat(), item_id),
    )
    _queue_calendar_sync(conn, item_id, "upsert")


def set_task_recurrence(
    item_id: int,
    rule: Optional[RecurrenceRule],
    end_date: Optional[str] = None,
) -> None:
    """単発タスクの繰り返し化、既存規則の変更、停止を原子的に保存する。"""
    with connection() as conn, conn:
        _set_task_recurrence_in_transaction(conn, item_id, rule, end_date)


def complete_task(item_id: int) -> Optional[int]:
    """Complete an occurrence and atomically create its next occurrence, if any."""
    with connection() as conn, conn:
        return _complete_task_in_transaction(conn, item_id)


def _complete_task_in_transaction(conn: sqlite3.Connection, item_id: int) -> Optional[int]:
    """完了日時・報酬・次回分を原子的に保存する。完了を取り消しても報酬は返還しない。"""
    from symnote.core.quest import award_for_completion_in_transaction

    task = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not task or task["kind"] != "task" or task["status"] == "done":
        return None
    completed_at = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute("UPDATE items SET status = 'done', completed_at = ? WHERE id = ?", (completed_at, item_id))
    completed_task = dict(task)
    completed_task["completed_at"] = completed_at
    award_for_completion_in_transaction(conn, completed_task)
    _queue_calendar_sync(conn, item_id, "upsert")
    from symnote.core.timeblock import cancel_blocks_for_task

    # 終わった枠は「やった記録」として残し、まだ来ていない枠だけ空ける。
    cancel_blocks_for_task(item_id, conn=conn, upcoming_only=True)
    rule_id = task["recurrence_rule_id"]
    if rule_id is None:
        return None
    rule = conn.execute(
        "SELECT * FROM task_recurrence_rules WHERE id = ? AND active = 1",
        (rule_id,),
    ).fetchone()
    if not rule:
        return None
    try:
        base_due = dt.date.fromisoformat(task["due_date"] or dt.date.today().isoformat())
    except ValueError:
        base_due = dt.date.today()
    settings = RecurrenceRule(
        rule["frequency"], rule["interval_days"],
        tuple(int(day) for day in rule["weekdays"].split(",") if day), rule["month_day"],
    )
    next_due = next_occurrence(base_due, settings).isoformat()
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
          recurrence_rule_id, goal_id, estimate_minutes
        ) VALUES (?, ?, ?, ?, 'task', ?, ?, ?, ?, ?, ?, 'inbox', ?, NULL, ?, ?, ?)
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
            task["goal_id"] if "goal_id" in task.keys() else None,
            task["estimate_minutes"] if "estimate_minutes" in task.keys() else None,
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


def _detach_task_references(conn: sqlite3.Connection, task_id: int) -> None:
    """削除するタスクへの報告・集中セッションを、同じ ID の新タスクに誤って効かないよう切り離す。"""
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE agent_reports SET status = 'rejected', reviewed_at = ? WHERE task_id = ? AND status = 'pending'",
        (now, task_id),
    )
    conn.execute(
        "UPDATE focus_sessions SET ended_at = ?, outcome = 'stopped' WHERE task_id = ? AND ended_at IS NULL",
        (now, task_id),
    )
    for table in ("agent_reports", "focus_sessions"):
        conn.execute(f"UPDATE {table} SET task_id = -task_id WHERE task_id = ?", (task_id,))


def detach_goal_references(conn: sqlite3.Connection, goal_ids: Sequence[int]) -> None:
    """削除するゴールへの承認待ち報告を却下し、同じ ID の新ゴールへ反映されないようにする。"""
    if not goal_ids:
        return
    now = dt.datetime.now().isoformat(timespec="seconds")
    marks = ",".join("?" for _ in goal_ids)
    conn.execute(
        f"UPDATE agent_reports SET status = 'rejected', reviewed_at = ? WHERE goal_id IN ({marks}) AND status = 'pending'",
        [now, *goal_ids],
    )
    conn.execute(f"UPDATE agent_reports SET goal_id = -goal_id WHERE goal_id IN ({marks})", list(goal_ids))


def delete_item(item_id: int) -> None:
    """アイテムを削除する。"""
    with connection() as conn, conn:
        row = conn.execute("SELECT kind FROM items WHERE id = ?", (item_id,)).fetchone()
        if row and row["kind"] == "task":
            # Retain the event mapping after the local task disappears so an
            # interrupted/offline delete is retried on the next sync.
            _queue_calendar_sync(conn, item_id, "delete")
            # items.id は削除後に再利用されうる。獲得済みの報酬履歴は残しつつ、同じ ID の
            # 新しいタスクの付与を妨げないよう、履歴側の task_id を負の値（再利用されない）へ退避する。
            conn.execute(
                "UPDATE quest_log SET task_id = -id WHERE task_id = ?",
                (item_id,),
            )
            _detach_task_references(conn, item_id)
            from symnote.core.timeblock import cancel_blocks_for_task

            cancel_blocks_for_task(item_id, conn=conn)
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
        "i.goal_id, i.estimate_minutes, i.delegated_to,",
        "r.interval_days AS recurrence_interval_days, r.end_date AS recurrence_end_date,",
        "r.frequency AS recurrence_frequency, r.weekdays AS recurrence_weekdays,",
        "r.month_day AS recurrence_month_day, r.active AS recurrence_active",
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
        SELECT id, created_at, date, due_date, due_time, raw_text, tags, ai_category,
               importance, urgency, effort, energy, status, goal_id, estimate_minutes, delegated_to
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
    recurrence_settings = fields.pop("recurrence_settings", None)
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
        "estimate_minutes",
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
    if not updates and not completes and recurrence_settings is None:
        return
    params.append(item_id)
    with connection() as conn, conn:
        if updates:
            conn.execute(f"UPDATE items SET {', '.join(updates)} WHERE id = ?", params)
        if recurrence_settings is not None:
            rule, end_date = recurrence_settings
            _set_task_recurrence_in_transaction(conn, item_id, rule, end_date)
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
