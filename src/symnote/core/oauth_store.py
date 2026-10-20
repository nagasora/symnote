"""リモート MCP 接続用 OAuth クライアントと発行済みトークンの保存を扱うモジュール。

トークンは SHA-256 ハッシュだけを保存し、平文は発行時にクライアントへ返すだけにする。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import time
from dataclasses import dataclass
from typing import List, Literal, Optional

from symnote.core.db import connection

TokenKind = Literal["access", "refresh"]


@dataclass(frozen=True)
class TokenRecord:
    """保存済みトークン 1 件の属性。"""

    kind: TokenKind
    client_id: str
    scopes: List[str]
    resource: Optional[str]
    expires_at: int


def hash_token(token: str) -> str:
    """トークン文字列を保存・照合用の SHA-256 16 進表現に変換する。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    """現在時刻を ISO 形式の文字列で返す。"""
    return dt.datetime.now().isoformat(timespec="seconds")


def save_client(client_id: str, client_info_json: str) -> None:
    """登録されたクライアント情報（JSON）を保存する。同じ ID は上書きする。"""
    with connection() as conn, conn:
        conn.execute(
            "INSERT INTO oauth_clients (client_id, client_info, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(client_id) DO UPDATE SET client_info = excluded.client_info",
            (client_id, client_info_json, _now_iso()),
        )


def load_client(client_id: str) -> Optional[str]:
    """クライアント情報の JSON を返す。未登録なら None。"""
    with connection() as conn:
        row = conn.execute(
            "SELECT client_info FROM oauth_clients WHERE client_id = ?", (client_id,)
        ).fetchone()
    return None if row is None else str(row["client_info"])


def evict_unused_clients(max_clients: int) -> int:
    """登録数が上限以上なら、トークン未発行の古いクライアントから削除して空きを作る。

    戻り値は削除件数。トークンを持つ（承認済みの）クライアントは削除しない。
    """
    with connection() as conn, conn:
        total = int(conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0])
        overflow = total - max_clients + 1
        if overflow <= 0:
            return 0
        cursor = conn.execute(
            "DELETE FROM oauth_clients WHERE client_id IN ("
            "SELECT client_id FROM oauth_clients WHERE client_id NOT IN "
            "(SELECT DISTINCT client_id FROM oauth_tokens) "
            "ORDER BY created_at, rowid LIMIT ?)",
            (overflow,),
        )
        return cursor.rowcount


def count_clients() -> int:
    """登録済みクライアント数を返す。"""
    with connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0])


def store_token(
    token: str,
    kind: TokenKind,
    client_id: str,
    scopes: List[str],
    resource: Optional[str],
    expires_at: int,
) -> None:
    """トークンをハッシュ化して保存する。"""
    with connection() as conn, conn:
        conn.execute(
            "INSERT INTO oauth_tokens "
            "(token_hash, kind, client_id, scopes, resource, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                hash_token(token),
                kind,
                client_id,
                " ".join(scopes),
                resource,
                expires_at,
                _now_iso(),
            ),
        )


def load_token(token: str, kind: TokenKind, now: Optional[float] = None) -> Optional[TokenRecord]:
    """有効期限内のトークンを返す。種類違い・期限切れ・未知なら None。"""
    current = time.time() if now is None else now
    with connection() as conn:
        row = conn.execute(
            "SELECT kind, client_id, scopes, resource, expires_at FROM oauth_tokens "
            "WHERE token_hash = ? AND kind = ? AND consumed_at IS NULL",
            (hash_token(token), kind),
        ).fetchone()
    if row is None or int(row["expires_at"]) <= current:
        return None
    return TokenRecord(
        kind=row["kind"],
        client_id=row["client_id"],
        scopes=[scope for scope in str(row["scopes"]).split(" ") if scope],
        resource=row["resource"],
        expires_at=int(row["expires_at"]),
    )


def consume_refresh_token(token: str, now: Optional[float] = None) -> bool:
    """未使用のリフレッシュトークンを使用済みにする。今回初めて使われた場合だけ True。

    UPDATE の条件で未使用を確かめるため、同じトークンの同時使用でも成功は 1 回に限られる。
    """
    current = int(time.time() if now is None else now)
    with connection() as conn, conn:
        cursor = conn.execute(
            "UPDATE oauth_tokens SET consumed_at = ? "
            "WHERE token_hash = ? AND kind = 'refresh' AND consumed_at IS NULL AND expires_at > ?",
            (current, hash_token(token), current),
        )
        return cursor.rowcount == 1


def consumed_refresh_token_client(token: str) -> Optional[str]:
    """使用済みのリフレッシュトークンなら発行先の client_id を返す。"""
    with connection() as conn:
        row = conn.execute(
            "SELECT client_id FROM oauth_tokens "
            "WHERE token_hash = ? AND kind = 'refresh' AND consumed_at IS NOT NULL",
            (hash_token(token),),
        ).fetchone()
    return None if row is None else str(row["client_id"])


def delete_token(token: str) -> bool:
    """トークン 1 件を削除する。削除できたら True。"""
    with connection() as conn, conn:
        cursor = conn.execute("DELETE FROM oauth_tokens WHERE token_hash = ?", (hash_token(token),))
        return cursor.rowcount > 0


def delete_client_tokens(client_id: str) -> int:
    """クライアントに発行したトークンをすべて削除し、件数を返す。"""
    with connection() as conn, conn:
        cursor = conn.execute("DELETE FROM oauth_tokens WHERE client_id = ?", (client_id,))
        return cursor.rowcount


def purge_expired_tokens(now: Optional[float] = None) -> int:
    """期限切れトークンを削除し、件数を返す。"""
    current = time.time() if now is None else now
    with connection() as conn, conn:
        cursor = conn.execute("DELETE FROM oauth_tokens WHERE expires_at <= ?", (int(current),))
        return cursor.rowcount


def revoke_all() -> int:
    """すべてのクライアントとトークンを削除し、削除したクライアント数を返す。"""
    with connection() as conn, conn:
        conn.execute("DELETE FROM oauth_tokens")
        cursor = conn.execute("DELETE FROM oauth_clients")
        return cursor.rowcount
