"""招待制ベータ向けの利用者・招待リスト・Webセッション管理（system.db）。

利用者ごとのタスク・メモ・ゴール・候補は各自の SQLite ファイルに完全分離し、
オーナー（SYMNOTE_OWNER_EMAIL）は既存の DB_PATH をそのまま使う。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Any, Iterator, List, Optional, Set

from symnote.config import default_owner_db_path, use_db_path
from symnote.core.db import SQLITE_BUSY_TIMEOUT_MS, init_db
def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def ensure_private_file(path: Path) -> None:
    if path.exists():
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
from symnote.core.usage_limit import use_usage_scope

SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
_initialized_user_dbs: Set[str] = set()
_init_lock = threading.Lock()


class InviteRequiredError(PermissionError):
    """招待されていないメールアドレスでのログイン試行を表す例外。"""


@dataclass(frozen=True)
class UserRecord:
    """SymNote Web の利用者情報。"""

    id: str
    email: str
    name: str
    picture: str
    is_owner: bool
    db_path: str
    created_at: str
    last_login_at: Optional[str] = None
    onboarded_at: Optional[str] = None


@dataclass(frozen=True)
class InviteRecord:
    """招待済みメールアドレスの記録。"""

    email: str
    is_owner: bool
    note: str
    invited_at: str
    accepted_at: Optional[str] = None


def normalize_email(email: str) -> str:
    """メールアドレスを小文字・前後空白除去で正規化する。"""
    cleaned = (email or "").strip().lower()
    if not cleaned or "@" not in cleaned:
        raise ValueError("有効なメールアドレスを指定してください。")
    return cleaned


def data_root() -> Path:
    """system.db と users/<id>/symnote.db を置く基底ディレクトリを返す。"""
    explicit_dir = os.getenv("SYMNOTE_DATA_DIR", "").strip()
    if explicit_dir:
        return Path(explicit_dir).expanduser().resolve()
    explicit_sys = os.getenv("SYMNOTE_SYSTEM_DB_PATH", "").strip()
    if explicit_sys:
        return Path(explicit_sys).expanduser().resolve().parent
    return (Path(__file__).resolve().parents[3] / "data").resolve()


def system_db_path() -> str:
    """system.db のファイルパスを返す。"""
    explicit_sys = os.getenv("SYMNOTE_SYSTEM_DB_PATH", "").strip()
    if explicit_sys:
        return str(Path(explicit_sys).expanduser().resolve())
    return str(data_root() / "system.db")


def users_data_dir() -> Path:
    """招待利用者ごとの SQLite を置くディレクトリを返す。"""
    return data_root() / "users"


@contextmanager
def _system_connection() -> Any:
    path = Path(system_db_path())
    ensure_private_dir(path.parent)
    conn = sqlite3.connect(str(path), timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        _ensure_system_schema(conn)
        yield conn
    finally:
        conn.close()
    ensure_private_file(path)


def _ensure_system_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY,
              email TEXT NOT NULL UNIQUE,
              name TEXT NOT NULL DEFAULT '',
              picture TEXT NOT NULL DEFAULT '',
              is_owner INTEGER NOT NULL DEFAULT 0 CHECK(is_owner IN (0, 1)),
              db_path TEXT NOT NULL,
              created_at TEXT NOT NULL,
              last_login_at TEXT,
              onboarded_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS invites (
              email TEXT PRIMARY KEY,
              is_owner INTEGER NOT NULL DEFAULT 0 CHECK(is_owner IN (0, 1)),
              note TEXT NOT NULL DEFAULT '',
              invited_at TEXT NOT NULL,
              accepted_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              expires_at INTEGER NOT NULL,
              last_seen_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS llm_usage ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL, day TEXT NOT NULL, called_at REAL NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_usage_day_scope ON llm_usage(day, scope)"
        )


def _row_to_user(row: sqlite3.Row) -> UserRecord:
    is_owner = bool(row["is_owner"])
    # オーナーの DB パスは常に現在の環境設定（default_owner_db_path）を優先する
    db_path = default_owner_db_path() if is_owner else str(row["db_path"])
    return UserRecord(
        id=str(row["id"]),
        email=str(row["email"]),
        name=str(row["name"] or ""),
        picture=str(row["picture"] or ""),
        is_owner=is_owner,
        db_path=db_path,
        created_at=str(row["created_at"]),
        last_login_at=row["last_login_at"],
        onboarded_at=row["onboarded_at"],
    )


def _row_to_invite(row: sqlite3.Row) -> InviteRecord:
    return InviteRecord(
        email=str(row["email"]),
        is_owner=bool(row["is_owner"]),
        note=str(row["note"] or ""),
        invited_at=str(row["invited_at"]),
        accepted_at=row["accepted_at"],
    )


def ensure_user_db_initialized(db_path: str, *, tighten_permissions: bool = True) -> None:
    """利用者の SQLite DB を初期化し、必要なら所有者のみの権限（600）に絞る。"""
    resolved = str(Path(db_path).expanduser())
    with _init_lock:
        if resolved in _initialized_user_dbs and (
            resolved == ":memory:" or Path(resolved).exists()
        ):
            return
        if resolved != ":memory:" and not resolved.startswith("file:"):
            target = Path(resolved)
            if tighten_permissions:
                ensure_private_dir(target.parent)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
        with use_db_path(resolved):
            init_db()
        if tighten_permissions and resolved != ":memory:" and not resolved.startswith("file:"):
            ensure_private_file(Path(resolved))
        _initialized_user_dbs.add(resolved)


@contextmanager
def user_scope(user: UserRecord) -> Iterator[UserRecord]:
    """指定ユーザーの DB と AI 利用回数スコープに切り替えて処理を実行する。"""
    ensure_user_db_initialized(user.db_path, tighten_permissions=not user.is_owner)
    with use_db_path(user.db_path), use_usage_scope(
        user.id, system_db_path=system_db_path()
    ):
        yield user


def configured_owner_email() -> Optional[str]:
    """環境変数 SYMNOTE_OWNER_EMAIL が設定されていれば正規化して返す。"""
    raw = os.getenv("SYMNOTE_OWNER_EMAIL", "").strip()
    if not raw or "@" not in raw:
        return None
    return raw.lower()


def invite_user(email: str, *, is_owner: bool = False, note: str = "") -> InviteRecord:
    """メールアドレスを招待リストへ追加（または更新）する。"""
    normalized = normalize_email(email)
    now = dt.datetime.now().isoformat(timespec="seconds")
    with _system_connection() as conn, conn:
        conn.execute(
            """
            INSERT INTO invites (email, is_owner, note, invited_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
              is_owner = excluded.is_owner,
              note = CASE WHEN excluded.note != '' THEN excluded.note ELSE invites.note END
            """,
            (normalized, 1 if is_owner else 0, (note or "").strip(), now),
        )
        if is_owner:
            conn.execute(
                "UPDATE users SET is_owner = 1, db_path = ? WHERE email = ?",
                (default_owner_db_path(), normalized),
            )
        row = conn.execute(
            "SELECT * FROM invites WHERE email = ?", (normalized,)
        ).fetchone()
        return _row_to_invite(row)


def revoke_invite(email: str) -> bool:
    """招待を取り消す。同時に該当ユーザーのセッションも破棄する。"""
    normalized = normalize_email(email)
    with _system_connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        user_row = conn.execute(
            "SELECT id FROM users WHERE email = ?", (normalized,)
        ).fetchone()
        if user_row is not None:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_row["id"],))
        cur = conn.execute("DELETE FROM invites WHERE email = ?", (normalized,))
        return bool(cur.rowcount)


def list_invites() -> List[InviteRecord]:
    """登録済みの招待一覧を新しい順に返す。"""
    with _system_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM invites ORDER BY invited_at DESC, email ASC"
        ).fetchall()
        return [_row_to_invite(r) for r in rows]


def list_users() -> List[UserRecord]:
    """登録済みの利用者一覧を返す。"""
    with _system_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY is_owner DESC, created_at ASC"
        ).fetchall()
        return [_row_to_user(r) for r in rows]


def get_user(user_id: str) -> Optional[UserRecord]:
    """ID で利用者を取得する。"""
    with _system_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_user(row) if row is not None else None


def get_user_by_email(email: str) -> Optional[UserRecord]:
    """メールアドレスで利用者を取得する。"""
    normalized = normalize_email(email)
    with _system_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (normalized,)
        ).fetchone()
        return _row_to_user(row) if row is not None else None


def ensure_local_owner_user() -> UserRecord:
    """ローカル実行や合言葉ログイン用のオーナーアカウントを用意して返す。"""
    owner_email = configured_owner_email() or "owner@local"
    now = dt.datetime.now().isoformat(timespec="seconds")
    owner_db = default_owner_db_path()
    with _system_connection() as conn, conn:
        existing = conn.execute(
            "SELECT * FROM users WHERE is_owner = 1 ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if existing is None:
            existing = conn.execute(
                "SELECT * FROM users WHERE email = ?", (owner_email,)
            ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO users (id, email, name, picture, is_owner, db_path, created_at, last_login_at, onboarded_at)
                VALUES ('owner', ?, 'Owner', '', 1, ?, ?, ?, ?)
                """,
                (owner_email, owner_db, now, now, now),
            )
            row = conn.execute("SELECT * FROM users WHERE id = 'owner'").fetchone()
        else:
            conn.execute(
                "UPDATE users SET is_owner = 1, db_path = ?, last_login_at = ? WHERE id = ?",
                (owner_db, now, existing["id"]),
            )
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (existing["id"],)
            ).fetchone()
        user = _row_to_user(row)
    ensure_user_db_initialized(user.db_path, tighten_permissions=False)
    return user


def authenticate_email(
    email: str,
    *,
    name: str = "",
    picture: str = "",
) -> UserRecord:
    """Google 認証済みメールアドレスを照合し、招待済みならユーザーを作成・更新して返す。"""
    normalized = normalize_email(email)
    owner_email = configured_owner_email()
    is_env_owner = owner_email is not None and normalized == owner_email
    now = dt.datetime.now().isoformat(timespec="seconds")

    with _system_connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        invite = conn.execute(
            "SELECT * FROM invites WHERE email = ?", (normalized,)
        ).fetchone()
        existing = conn.execute(
            "SELECT * FROM users WHERE email = ?", (normalized,)
        ).fetchone()

        if invite is None and not is_env_owner:
            raise InviteRequiredError(
                "このメールアドレスはまだ招待されていません。管理者に招待を依頼してください。"
            )

        is_owner = bool(
            is_env_owner
            or (invite is not None and invite["is_owner"])
            or (existing is not None and existing["is_owner"])
        )

        if existing is None:
            user_id = "owner" if is_owner and conn.execute(
                "SELECT 1 FROM users WHERE id = 'owner'"
            ).fetchone() is None else f"u_{secrets.token_hex(8)}"
            db_path = (
                default_owner_db_path()
                if is_owner
                else str((users_data_dir() / user_id / "symnote.db").resolve())
            )
            display_name = (name or "").strip() or normalized.split("@", 1)[0]
            # オーナーは既存データを使うためオンボーディング完了扱いにする
            onboarded_at = now if is_owner else None
            conn.execute(
                """
                INSERT INTO users (id, email, name, picture, is_owner, db_path, created_at, last_login_at, onboarded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    normalized,
                    display_name,
                    (picture or "").strip(),
                    1 if is_owner else 0,
                    db_path,
                    now,
                    now,
                    onboarded_at,
                ),
            )
        else:
            user_id = str(existing["id"])
            db_path = default_owner_db_path() if is_owner else str(existing["db_path"])
            display_name = (name or "").strip() or str(existing["name"] or "")
            display_pic = (picture or "").strip() or str(existing["picture"] or "")
            conn.execute(
                """
                UPDATE users
                SET name = ?, picture = ?, is_owner = ?, db_path = ?, last_login_at = ?
                WHERE id = ?
                """,
                (
                    display_name,
                    display_pic,
                    1 if is_owner else 0,
                    db_path,
                    now,
                    user_id,
                ),
            )

        if invite is not None and not invite["accepted_at"]:
            conn.execute(
                "UPDATE invites SET accepted_at = ? WHERE email = ?",
                (now, normalized),
            )
        elif is_env_owner and invite is None:
            conn.execute(
                """
                INSERT INTO invites (email, is_owner, note, invited_at, accepted_at)
                VALUES (?, 1, 'owner', ?, ?)
                ON CONFLICT(email) DO NOTHING
                """,
                (normalized, now, now),
            )

        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        user = _row_to_user(row)

    ensure_user_db_initialized(user.db_path, tighten_permissions=not user.is_owner)
    return user


def complete_onboarding(user_id: str, *, name: Optional[str] = None) -> UserRecord:
    """初回オンボーディング完了を記録する。"""
    now = dt.datetime.now().isoformat(timespec="seconds")
    with _system_connection() as conn, conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise LookupError(f"ユーザー {user_id} は存在しません。")
        final_name = (name or "").strip() or str(row["name"] or "")
        conn.execute(
            "UPDATE users SET onboarded_at = COALESCE(onboarded_at, ?), name = ? WHERE id = ?",
            (now, final_name, user_id),
        )
        updated = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_user(updated)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _session_access_allowed(conn: sqlite3.Connection, user: sqlite3.Row) -> bool:
    """セッション発行・参照時にも現在の招待または本人オーナー設定を確認する。"""
    owner_email = configured_owner_email()
    if owner_email is not None and user["email"] == owner_email:
        return True
    if owner_email is None and user["email"] == "owner@local" and user["is_owner"]:
        return True
    return conn.execute(
        "SELECT 1 FROM invites WHERE email = ?", (user["email"],)
    ).fetchone() is not None


def create_session(user_id: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    """Web ログインセッションを作成し、Cookie に載せる生トークンを返す。"""
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    now_int = int(time.time())
    now_iso = dt.datetime.now().isoformat(timespec="seconds")
    with _system_connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None or not _session_access_allowed(conn, user):
            raise InviteRequiredError("この利用者には現在有効な招待がありません。")
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_int,))
        conn.execute(
            """
            INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (token_hash, user_id, now_iso, now_int + int(ttl_seconds), now_int),
        )
    return raw_token


def get_user_by_session(token: str) -> Optional[UserRecord]:
    """セッショントークンから有効な利用者を返す。期限切れや存在しない場合は None。"""
    if not token or not token.strip():
        return None
    token_hash = _hash_token(token.strip())
    now_int = int(time.time())
    with _system_connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT u.*
            FROM sessions AS s
            JOIN users AS u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (token_hash, now_int),
        ).fetchone()
        if row is None:
            return None
        if not _session_access_allowed(conn, row):
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            return None
        conn.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
            (now_int, token_hash),
        )
        return _row_to_user(row)


def revoke_session(token: str) -> None:
    """指定セッショントークンを無効化する。"""
    if not token or not token.strip():
        return
    token_hash = _hash_token(token.strip())
    with _system_connection() as conn, conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
