"""利用者別の暗号化接続、認可state、排他取り込みと出典記録。"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from typing import Any

from symnote.core import candidates, db
from symnote.core.usage_limit import current_scope

PROVIDERS = {"gmail", "slack"}


def provider_name(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ValueError("未対応の情報源です。")
    return provider


def _cipher():
    from cryptography.fernet import Fernet

    try:
        return Fernet(os.environ.get("SYMNOTE_INTEGRATION_KEY", "").encode("ascii"))
    except (ValueError, UnicodeError):
        raise ValueError("連携用の暗号鍵が設定されていません。管理者に確認してください。") from None


def encryption_ready() -> bool:
    try:
        _cipher()
        return True
    except (ValueError, ImportError):
        return False


def seal(provider: str, data: Any) -> str:
    envelope = {"owner": current_scope(), "provider": provider_name(provider), "data": data}
    return _cipher().encrypt(json.dumps(envelope).encode()).decode()


def unseal(provider: str, value: str) -> Any:
    try:
        envelope = json.loads(_cipher().decrypt(value.encode()))
        if envelope["owner"] != current_scope() or envelope["provider"] != provider:
            raise ValueError()
        return envelope["data"]
    except Exception:
        raise ValueError(
            "保存した接続を確認できません。管理者に確認して再接続してください。"
        ) from None


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def save_state(provider: str, session: str, payload: dict) -> str:
    provider_name(provider)
    if not session:
        raise PermissionError("ブラウザでログインしてから接続してください。")
    state = secrets.token_urlsafe(32)
    with db.connection() as conn, conn:
        conn.execute(
            "DELETE FROM source_oauth_states WHERE provider = ? OR expires_at < ?",
            (provider, time.time()),
        )
        conn.execute(
            "INSERT INTO source_oauth_states(state_hash, provider, session_hash, "
            "expires_at, payload) VALUES (?, ?, ?, ?, ?)",
            (digest(state), provider, digest(session), time.time() + 600, seal(provider, payload)),
        )
    return state


def consume_state(provider: str, session: str, state: str) -> dict:
    if not session or not state or len(state) > 200:
        raise PermissionError("接続の確認ができません。設定からやり直してください。")
    with db.connection() as conn, conn:
        row = conn.execute(
            "UPDATE source_oauth_states SET consumed = 1 WHERE state_hash = ? AND provider = ? "
            "AND session_hash = ? AND expires_at > ? AND consumed = 0 RETURNING payload",
            (digest(state), provider_name(provider), digest(session), time.time()),
        ).fetchone()
    if not row:
        raise PermissionError(
            "接続の確認ができません。有効期限またはログイン状態を確認してください。"
        )
    payload = unseal(provider, row["payload"])
    payload["state_hash"] = digest(state)
    return payload


def load(provider: str) -> dict | None:
    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM source_connections WHERE provider = ?", (provider_name(provider),)
        ).fetchone()
    return dict(row) if row else None


def save_connection(
    provider: str, credentials: dict, account: str, workspace: str, *, state_hash: str = ""
) -> None:
    encrypted = seal(provider, credentials)
    with db.connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if (
            state_hash
            and not conn.execute(
                "DELETE FROM source_oauth_states WHERE state_hash = ? AND provider = ? "
                "AND consumed = 1 AND expires_at > ? RETURNING state_hash",
                (state_hash, provider, time.time()),
            ).fetchone()
        ):
            raise ValueError("接続操作は解除されたか期限切れです。設定からやり直してください。")
        old = conn.execute(
            "SELECT * FROM source_connections WHERE provider = ?", (provider,)
        ).fetchone()
        if old and old["run_until"] > time.time():
            raise ValueError("取り込み中です。完了してから再接続してください。")
        conn.execute("DELETE FROM source_connections WHERE provider = ?", (provider,))
        conn.execute(
            "INSERT INTO source_connections(provider, revision, credentials, account, "
            "workspace, verified_at) VALUES (?, ?, ?, ?, ?, ?)",
            (provider, secrets.token_hex(16), encrypted, account, workspace, time.time()),
        )


def disconnect(provider: str) -> None:
    with db.connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT run_until FROM source_connections WHERE provider = ?",
            (provider_name(provider),),
        ).fetchone()
        if row and row["run_until"] > time.time():
            raise ValueError("取り込み中です。完了してから解除してください。")
        conn.execute("DELETE FROM source_connections WHERE provider = ?", (provider,))
        conn.execute("DELETE FROM source_oauth_states WHERE provider = ?", (provider,))


def acquire(provider: str) -> tuple[dict, str]:
    run = secrets.token_hex(16)
    with db.connection() as conn, conn:
        changed = conn.execute(
            "UPDATE source_connections SET run_id = ?, run_until = ? "
            "WHERE provider = ? AND run_until <= ?",
            (run, time.time() + 900, provider_name(provider), time.time()),
        ).rowcount
        if not changed:
            raise ValueError("未接続、または別の取り込みが実行中です。")
        row = conn.execute(
            "SELECT * FROM source_connections WHERE provider = ?", (provider,)
        ).fetchone()
    return dict(row), run


def guard(conn: Any, provider: str, revision: str, run: str) -> None:
    if not conn.execute(
        "UPDATE source_connections SET run_until = run_until "
        "WHERE provider = ? AND revision = ? AND run_id = ? AND run_until > ?",
        (provider, revision, run, time.time()),
    ).rowcount:
        raise ValueError("接続が変更されたため取り込みを中止しました。再試行してください。")


def renew(provider: str, row: dict, run: str) -> None:
    with db.connection() as conn, conn:
        guard(conn, provider, row["revision"], run)
        conn.execute(
            "UPDATE source_connections SET run_until = ? WHERE provider = ?",
            (time.time() + 900, provider),
        )


def update_credentials(provider: str, row: dict, run: str, value: dict) -> None:
    encrypted = seal(provider, value)
    with db.connection() as conn, conn:
        guard(conn, provider, row["revision"], run)
        conn.execute(
            "UPDATE source_connections SET credentials = ? WHERE provider = ?",
            (encrypted, provider),
        )


def set_channels(row: dict, run: str, channel_ids: list[str]) -> None:
    with db.connection() as conn, conn:
        guard(conn, "slack", row["revision"], run)
        conn.execute(
            "UPDATE source_connections SET channels = ?, checkpoint = '{}' "
            "WHERE provider = 'slack'",
            (json.dumps(channel_ids),),
        )


def processed(provider: str) -> set[str]:
    with db.connection() as conn:
        return {
            r["source_ref"]
            for r in conn.execute(
                "SELECT source_ref FROM source_processed WHERE provider = ?", (provider,)
            ).fetchall()
        }


def save_batch(provider: str, row: dict, run: str, proposals: list, refs: list[str]):
    with db.connection() as conn, conn:
        guard(conn, provider, row["revision"], run)
        result = candidates.propose_candidates_in_transaction(conn, proposals)
        for ref in refs:
            conn.execute(
                "INSERT INTO source_processed(provider, source_ref) VALUES (?, ?) "
                "ON CONFLICT DO NOTHING",
                (provider, ref),
            )
    return result


def save_checkpoint(provider: str, row: dict, run: str, checkpoint: dict) -> None:
    with db.connection() as conn, conn:
        guard(conn, provider, row["revision"], run)
        conn.execute(
            "UPDATE source_connections SET checkpoint = ? WHERE provider = ?",
            (json.dumps(checkpoint), provider),
        )


def fetched(provider: str, row: dict, run: str) -> None:
    with db.connection() as conn, conn:
        guard(conn, provider, row["revision"], run)
        conn.execute(
            "UPDATE source_connections SET last_fetch_at = ? WHERE provider = ?",
            (time.time(), provider),
        )


def finish(provider: str, row: dict, run: str, error: str = "", *, success: bool = False) -> None:
    with db.connection() as conn, conn:
        conn.execute(
            "UPDATE source_connections SET run_id = '', run_until = 0, last_error = ?, "
            "last_success_at = CASE WHEN ? = 1 THEN ? ELSE last_success_at END "
            "WHERE provider = ? AND revision = ? AND run_id = ?",
            (error, int(success), time.time(), provider, row["revision"], run),
        )
