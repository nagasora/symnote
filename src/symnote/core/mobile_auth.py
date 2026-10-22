"""Short-lived, browser-bound handoff from Web Google OAuth to the native app."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import threading
import time
from dataclasses import dataclass

from symnote.core.accounts import _system_connection

MOBILE_REDIRECT_URI = "app.symnote.mobile://auth/callback"
MOBILE_FLOW_TTL_SECONDS = 600
MOBILE_CODE_TTL_SECONDS = 60
MAX_MOBILE_FLOWS = 200
MAX_MOBILE_CODES = 200

_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_schema_lock = threading.Lock()


@dataclass(frozen=True)
class MobilePendingFlow:
    app_state: str
    expires_at: float


@dataclass(frozen=True)
class MobileCallbackHandoff:
    code: str
    app_state: str


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _ensure_schema(conn: object) -> None:
    """Create mobile-only system tables on first use without editing account schema."""
    with _schema_lock, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mobile_oauth_flows (
              state_hash TEXT PRIMARY KEY,
              code_challenge TEXT NOT NULL,
              app_state TEXT NOT NULL,
              created_at DOUBLE PRECISION NOT NULL,
              expires_at DOUBLE PRECISION NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mobile_exchange_codes (
              code_hash TEXT PRIMARY KEY,
              code_challenge TEXT NOT NULL,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              created_at DOUBLE PRECISION NOT NULL,
              expires_at DOUBLE PRECISION NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mobile_oauth_flows_expiry "
            "ON mobile_oauth_flows(expires_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mobile_exchange_codes_expiry "
            "ON mobile_exchange_codes(expires_at)"
        )


def valid_code_challenge(value: str) -> bool:
    return len(value) == 43 and _BASE64URL_RE.fullmatch(value) is not None


def valid_app_state(value: str) -> bool:
    return 16 <= len(value) <= 128 and _BASE64URL_RE.fullmatch(value) is not None


def valid_code(value: str) -> bool:
    return 32 <= len(value) <= 128 and _BASE64URL_RE.fullmatch(value) is not None


def valid_verifier(value: str) -> bool:
    return 43 <= len(value) <= 128 and _BASE64URL_RE.fullmatch(value) is not None


def verifier_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def register_mobile_flow(
    oauth_state: str,
    code_challenge: str,
    app_state: str,
    *,
    now: float | None = None,
) -> None:
    """Bind an existing Web OAuth state to a native PKCE challenge."""
    if (
        not oauth_state
        or not valid_code_challenge(code_challenge)
        or not valid_app_state(app_state)
    ):
        raise ValueError("invalid mobile authorization request")
    current = time.time() if now is None else float(now)
    with _system_connection() as conn, conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM mobile_oauth_flows WHERE expires_at <= ?", (current,))
        count = int(conn.execute("SELECT COUNT(*) FROM mobile_oauth_flows").fetchone()[0])
        if count >= MAX_MOBILE_FLOWS:
            conn.execute(
                "DELETE FROM mobile_oauth_flows WHERE state_hash IN "
                "(SELECT state_hash FROM mobile_oauth_flows ORDER BY created_at LIMIT ?)",
                (count - MAX_MOBILE_FLOWS + 1,),
            )
        conn.execute(
            """
            INSERT INTO mobile_oauth_flows
              (state_hash, code_challenge, app_state, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                _hash(oauth_state),
                code_challenge,
                app_state,
                current,
                current + MOBILE_FLOW_TTL_SECONDS,
            ),
        )


def get_mobile_flow(oauth_state: str) -> MobilePendingFlow | None:
    """Return a mobile marker without consuming it, including for safe expiry errors."""
    if not oauth_state:
        return None
    with _system_connection() as conn, conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT app_state, expires_at FROM mobile_oauth_flows WHERE state_hash = ?",
            (_hash(oauth_state),),
        ).fetchone()
        if row is None:
            return None
        return MobilePendingFlow(
            app_state=str(row["app_state"]),
            expires_at=float(row["expires_at"]),
        )


def create_callback_handoff(
    oauth_state: str,
    user_id: str,
    *,
    now: float | None = None,
) -> MobileCallbackHandoff | None:
    """Atomically replace a pending mobile flow with a one-use exchange code."""
    current = time.time() if now is None else float(now)
    raw_code = secrets.token_urlsafe(32)
    with _system_connection() as conn, conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM mobile_oauth_flows WHERE expires_at <= ?", (current,))
        conn.execute("DELETE FROM mobile_exchange_codes WHERE expires_at <= ?", (current,))
        row = conn.execute(
            "DELETE FROM mobile_oauth_flows WHERE state_hash = ? "
            "RETURNING code_challenge, app_state",
            (_hash(oauth_state),),
        ).fetchone()
        if row is None:
            return None
        count = int(conn.execute("SELECT COUNT(*) FROM mobile_exchange_codes").fetchone()[0])
        if count >= MAX_MOBILE_CODES:
            conn.execute(
                "DELETE FROM mobile_exchange_codes WHERE code_hash IN "
                "(SELECT code_hash FROM mobile_exchange_codes ORDER BY created_at LIMIT ?)",
                (count - MAX_MOBILE_CODES + 1,),
            )
        conn.execute(
            """
            INSERT INTO mobile_exchange_codes
              (code_hash, code_challenge, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                _hash(raw_code),
                str(row["code_challenge"]),
                user_id,
                current,
                current + MOBILE_CODE_TTL_SECONDS,
            ),
        )
        return MobileCallbackHandoff(code=raw_code, app_state=str(row["app_state"]))


def exchange_mobile_code(
    code: str,
    verifier: str,
    *,
    now: float | None = None,
) -> str | None:
    """Validate PKCE, then atomically consume the code and return its user id."""
    if not valid_code(code) or not valid_verifier(verifier):
        return None
    current = time.time() if now is None else float(now)
    presented_challenge = verifier_challenge(verifier)
    with _system_connection() as conn, conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM mobile_exchange_codes WHERE expires_at <= ?", (current,))
        row = conn.execute(
            "SELECT code_challenge, user_id FROM mobile_exchange_codes "
            "WHERE code_hash = ? AND expires_at > ?",
            (_hash(code), current),
        ).fetchone()
        if row is None or not secrets.compare_digest(
            str(row["code_challenge"]), presented_challenge
        ):
            return None
        deleted = conn.execute(
            "DELETE FROM mobile_exchange_codes WHERE code_hash = ? RETURNING user_id",
            (_hash(code),),
        ).fetchone()
        return str(deleted["user_id"]) if deleted is not None else None


__all__ = [
    "MAX_MOBILE_CODES",
    "MAX_MOBILE_FLOWS",
    "MOBILE_CODE_TTL_SECONDS",
    "MOBILE_FLOW_TTL_SECONDS",
    "MOBILE_REDIRECT_URI",
    "MobileCallbackHandoff",
    "MobilePendingFlow",
    "create_callback_handoff",
    "exchange_mobile_code",
    "get_mobile_flow",
    "register_mobile_flow",
    "valid_app_state",
    "valid_code_challenge",
    "verifier_challenge",
]
