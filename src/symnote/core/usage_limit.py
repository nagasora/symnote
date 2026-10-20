"""AI（Gemini）呼び出し回数の上限管理。過剰な呼び出しによる想定外の請求を防ぐ。

上限は「利用者ごと（1 分あたり・1 日あたり）」と「アプリ全体の 1 日あたり」の三段で、
どれかを超えたら通信する前に止める。記録は SQLite に残し、プロセス再起動やタブを
複数開いても回数がリセットされないようにする。
"""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
import datetime as dt
import os
from pathlib import Path
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from symnote.core.db import SQLITE_BUSY_TIMEOUT_MS, connection

DEFAULT_PER_MINUTE = 10
DEFAULT_DAILY_PER_USER = 200
DEFAULT_DAILY_GLOBAL = 1000
DEFAULT_MAX_PROMPT_CHARS = 30000
LOCAL_SCOPE = "local"

_current_scope: contextvars.ContextVar[str] = contextvars.ContextVar("llm_usage_scope", default=LOCAL_SCOPE)
_system_usage_db_path: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "llm_usage_system_db_path",
    default=None,
)


class UsageLimitExceeded(RuntimeError):
    """AI 呼び出しの上限に達したことを表す。"""


@dataclass(frozen=True)
class UsageLimits:
    """AI 呼び出しの各上限値。0 以下はその上限を無効にする。"""

    per_minute: int = DEFAULT_PER_MINUTE
    daily_per_user: int = DEFAULT_DAILY_PER_USER
    daily_global: int = DEFAULT_DAILY_GLOBAL
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS


@dataclass(frozen=True)
class UsageSnapshot:
    """今日の AI 呼び出し回数と上限。"""

    scope: str
    today_scope: int
    today_global: int
    limits: UsageLimits


def _int_env(name: str, default: int) -> int:
    """整数の環境変数を読む。不正値は既定値にする。"""
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def load_limits() -> UsageLimits:
    """環境変数から上限を読む。"""
    return UsageLimits(
        per_minute=_int_env("SYMNOTE_LLM_PER_MINUTE", DEFAULT_PER_MINUTE),
        daily_per_user=_int_env("SYMNOTE_LLM_DAILY_PER_USER", DEFAULT_DAILY_PER_USER),
        daily_global=_int_env("SYMNOTE_LLM_DAILY_GLOBAL", DEFAULT_DAILY_GLOBAL),
        max_prompt_chars=_int_env("SYMNOTE_LLM_MAX_PROMPT_CHARS", DEFAULT_MAX_PROMPT_CHARS),
    )


def set_usage_scope(scope: str) -> contextvars.Token:
    """以降の AI 呼び出しを数える利用者（またはセッション）を設定する。"""
    return _current_scope.set(scope.strip() or LOCAL_SCOPE)


@contextmanager
def use_usage_scope(scope: str, *, system_db_path: Optional[str] = None) -> Iterator[None]:
    """スコープと（任意で）全体集計用の system.db パスを一時的に切り替える。"""
    scope_token = _current_scope.set(scope.strip() or LOCAL_SCOPE)
    db_token = _system_usage_db_path.set(system_db_path) if system_db_path is not None else None
    try:
        yield
    finally:
        _current_scope.reset(scope_token)
        if db_token is not None:
            _system_usage_db_path.reset(db_token)


def current_scope() -> str:
    """現在の利用者スコープ。"""
    return _current_scope.get()


@contextmanager
def _usage_connection() -> Any:
    sys_db = _system_usage_db_path.get()
    if not sys_db:
        with connection() as conn:
            yield conn
        return
    if sys_db != ":memory:" and not sys_db.startswith("file:"):
        Path(sys_db).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(sys_db, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.close()


def _ensure_table(conn) -> None:  # type: ignore[no-untyped-def]
    # 呼び出しの直前に必要になる記録用の表なので、版付きマイグレーションの完了を待たずに用意する。
    conn.execute(
        "CREATE TABLE IF NOT EXISTS llm_usage ("
        "id INTEGER PRIMARY KEY, scope TEXT NOT NULL, day TEXT NOT NULL, called_at REAL NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_day_scope ON llm_usage(day, scope)")


def _today(now: float) -> str:
    return dt.datetime.fromtimestamp(now).date().isoformat()


def check_prompt_size(prompt: str, limits: Optional[UsageLimits] = None) -> None:
    """入力が長すぎる場合は通信前に止める（長文は 1 回でも高額になりうる）。"""
    limits = limits or load_limits()
    if limits.max_prompt_chars > 0 and len(prompt) > limits.max_prompt_chars:
        raise UsageLimitExceeded(
            f"AI に渡す文章が長すぎます（{len(prompt):,} 文字 / 上限 {limits.max_prompt_chars:,} 文字）。"
            "短くしてから再度お試しください。"
        )


def acquire(scope: Optional[str] = None, *, now: Optional[float] = None, limits: Optional[UsageLimits] = None) -> None:
    """上限内なら 1 回分を記録する。超えていれば記録せず UsageLimitExceeded を送出する。"""
    scope = scope or current_scope()
    now = time.time() if now is None else now
    limits = limits or load_limits()
    day = _today(now)
    with _usage_connection() as conn:
        _ensure_table(conn)
        # 判定と記録の間に他のタブ・プロセスが割り込むと上限を超えて通るため、書込ロックを先に取る。
        conn.execute("BEGIN IMMEDIATE")
        try:
            if limits.per_minute > 0:
                recent = conn.execute(
                    "SELECT count(*) FROM llm_usage WHERE scope = ? AND called_at > ?",
                    (scope, now - 60),
                ).fetchone()[0]
                if recent >= limits.per_minute:
                    raise UsageLimitExceeded(
                        "AI の呼び出しが集中しています。1 分ほど待ってから再度お試しください。"
                    )
            if limits.daily_per_user > 0:
                used = conn.execute(
                    "SELECT count(*) FROM llm_usage WHERE day = ? AND scope = ?", (day, scope)
                ).fetchone()[0]
                if used >= limits.daily_per_user:
                    raise UsageLimitExceeded(
                        f"今日の AI 利用回数の上限（{limits.daily_per_user} 回）に達しました。"
                        "明日また使えます。手動での編集はそのまま使えます。"
                    )
            if limits.daily_global > 0:
                total = conn.execute("SELECT count(*) FROM llm_usage WHERE day = ?", (day,)).fetchone()[0]
                if total >= limits.daily_global:
                    raise UsageLimitExceeded(
                        "本日の AI 機能は全体の上限に達したため一時停止しています。明日また使えます。"
                    )
            conn.execute(
                "INSERT INTO llm_usage(scope, day, called_at) VALUES (?, ?, ?)", (scope, day, now)
            )
            cutoff = (dt.date.fromisoformat(day) - dt.timedelta(days=7)).isoformat()
            conn.execute("DELETE FROM llm_usage WHERE day < ?", (cutoff,))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def usage_snapshot(scope: Optional[str] = None, *, now: Optional[float] = None) -> UsageSnapshot:
    """今日の利用回数と上限を返す。"""
    scope = scope or current_scope()
    now = time.time() if now is None else now
    day = _today(now)
    with _usage_connection() as conn, conn:
        _ensure_table(conn)
        mine = conn.execute(
            "SELECT count(*) FROM llm_usage WHERE day = ? AND scope = ?", (day, scope)
        ).fetchone()[0]
        total = conn.execute("SELECT count(*) FROM llm_usage WHERE day = ?", (day,)).fetchone()[0]
    return UsageSnapshot(scope=scope, today_scope=int(mine), today_global=int(total), limits=load_limits())
