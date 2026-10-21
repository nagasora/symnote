"""SymNote Web の認証（Google OAuth / 合言葉 / E2E）・セッションCookie・CSRF防御。"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
import secrets
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode, urlparse
import urllib.request

from starlette.requests import Request
from starlette.responses import Response

from symnote.core import postgres
from symnote.core.accounts import (
    InviteRequiredError,
     SESSION_TTL_SECONDS,
    UserRecord,
    authenticate_email,
    create_session,
    ensure_local_owner_user,
    get_user_by_session,
    invite_user,
    revoke_session,
    save_web_oauth_state,
    consume_web_oauth_state,
)
from symnote.core.runtime import PASSCODE_LIMITER, app_passcode, passcode_source

SESSION_COOKIE_NAME = "symnote_session"
OAUTH_STATE_COOKIE_NAME = "symnote_oauth_state"
CSRF_HEADER_NAME = "x-symnote"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
OAUTH_STATE_TTL_SECONDS = 600
MAX_OAUTH_STATES = 200


@dataclass(frozen=True)
class OAuthPendingState:
    code_verifier: str
    redirect_uri: str
    created_at: float


_oauth_states: Dict[str, OAuthPendingState] = {}


def google_oauth_config() -> Optional[Tuple[str, str]]:
    """Google Web OAuth の (client_id, client_secret) を返す。未設定なら None。"""
    client_id = os.getenv("SYMNOTE_GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("SYMNOTE_GOOGLE_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        return client_id, client_secret
    return None


def e2e_mode_enabled() -> bool:
    """E2E テスト用のモックログインが有効かを返す。"""
    return os.getenv("SYMNOTE_E2E", "").strip().lower() in {"1", "true", "yes"}


def is_loopback_request(request: Request) -> bool:
    """リクエスト元がローカルループバック（または TestClient）かつトンネル経由でないかを判定する。"""
    # Cloudflare 等のリバースプロキシヘッダーが付いている場合は外部経由とみなす
    if request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for"):
        return False
    client_host = (request.client.host if request.client else "").strip().lower()
    return client_host in {"127.0.0.1", "::1", "localhost", "testclient"}


def auth_mode() -> str:
    """現在の認証モードを返す: 'google' | 'passcode' | 'local'。"""
    if google_oauth_config() is not None:
        return "google"
    if app_passcode():
        return "passcode"
    return "local"


def is_https_request(request: Request) -> bool:
    """HTTPS（または Cloudflare Tunnel 経由の HTTPS）リクエストかを返す。"""
    if request.url.scheme == "https":
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return forwarded_proto == "https"


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    """HttpOnly / SameSite=Lax（HTTPS時は Secure）なセッション Cookie を設定する。"""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=is_https_request(request),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """セッション Cookie を削除する。"""
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")


def verify_csrf(request: Request) -> Optional[str]:
    """状態変更リクエストの CSRF ヘッダーと Origin を検証する。問題があればエラーメッセージを返す。"""
    if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return None
    marker = request.headers.get(CSRF_HEADER_NAME, "").strip()
    if marker != "1":
        return "不正なリクエストです（X-SymNote ヘッダーが必要です）。"
    origin = request.headers.get("origin", "").strip()
    if origin:
        parsed_origin = urlparse(origin)
        origin_host = (parsed_origin.netloc or "").lower()
        req_host = (request.headers.get("host") or request.url.netloc or "").lower()
        public_origin = os.getenv("SYMNOTE_PUBLIC_ORIGIN", "").strip()
        allowed_hosts = {req_host}
        if public_origin:
            allowed_hosts.add(urlparse(public_origin).netloc.lower())
        if origin_host not in allowed_hosts:
            return "許可されていない送信元（Origin）からのリクエストです。"
    return None


def resolve_request_user(request: Request) -> Optional[UserRecord]:
    """リクエストのセッション Cookie（またはローカル開発時の自動オーナー）から利用者を返す。"""
    token = request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    if token:
        user = get_user_by_session(token)
        if user is not None:
            return user
    # Google OAuth も合言葉も未設定で、かつローカルからのアクセスなら自動的にオーナーとして扱う
    if (
        not postgres.enabled()
        and auth_mode() == "local"
        and not e2e_mode_enabled()
        and is_loopback_request(request)
    ):
        return ensure_local_owner_user()
    return None


def public_base_url(request: Request) -> str:
    """OAuth コールバック URL の組み立てに使う公開基底 URL を返す。"""
    configured = os.getenv("SYMNOTE_PUBLIC_ORIGIN", "").strip().rstrip("/")
    if configured:
        return configured
    scheme = "https" if is_https_request(request) else request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


def build_google_authorization_url(request: Request) -> str:
    """Google OAuth 2.0 (PKCE) の認可画面 URL を生成する。"""
    creds = google_oauth_config()
    if creds is None:
        raise RuntimeError("Google ログインが設定されていません（SYMNOTE_GOOGLE_CLIENT_ID / SECRET）。")
    client_id, _ = creds
    now = time.time()
    expired = [k for k, v in _oauth_states.items() if now - v.created_at > OAUTH_STATE_TTL_SECONDS]
    for k in expired:
        _oauth_states.pop(k, None)
    if len(_oauth_states) >= MAX_OAUTH_STATES:
        oldest = min(_oauth_states, key=lambda k: _oauth_states[k].created_at)
        _oauth_states.pop(oldest, None)

    state = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    redirect_uri = f"{public_base_url(request)}/api/auth/google/callback"
    pending = OAuthPendingState(
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        created_at=now,
    )
    if postgres.enabled():
        save_web_oauth_state(
            state,
            json.dumps({
                "code_verifier": code_verifier, "redirect_uri": redirect_uri, "created_at": now,
            }),
            now, ttl=OAUTH_STATE_TTL_SECONDS, max_states=MAX_OAUTH_STATES,
        )
    else:
        _oauth_states[state] = pending
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_google_code(code: str, pending: OAuthPendingState) -> Dict[str, Any]:
    """認可コードをアクセストークンに交換し、Google UserInfo を取得する。"""
    creds = google_oauth_config()
    if creds is None:
        raise RuntimeError("Google ログインが設定されていません。")
    client_id, client_secret = creds
    token_data = urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": pending.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": pending.code_verifier,
        }
    ).encode("utf-8")
    token_req = urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=token_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(token_req, timeout=10) as resp:  # noqa: S310
        token_payload = json.loads(resp.read().decode("utf-8"))
    access_token = token_payload.get("access_token")
    if not access_token:
        raise ValueError("Google からアクセストークンを取得できませんでした。")

    info_req = urllib.request.Request(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    with urllib.request.urlopen(info_req, timeout=10) as resp:  # noqa: S310
        user_info = json.loads(resp.read().decode("utf-8"))
    return dict(user_info)


def complete_google_callback(state: str, code: str, browser_state: str) -> UserRecord:
    """Google OAuth コールバックを検証し、招待済みユーザーとしてログインさせる。"""
    # PKCE alone does not stop a different browser from submitting an attacker's callback.
    if not browser_state or not secrets.compare_digest(state, browser_state):
        raise ValueError("ログインを開始したブラウザからやり直してください。")
    if postgres.enabled():
        encoded = consume_web_oauth_state(state)
        pending = OAuthPendingState(**json.loads(encoded)) if encoded else None
    else:
        pending = _oauth_states.pop(state, None)
    if pending is None or time.time() - pending.created_at > OAUTH_STATE_TTL_SECONDS:
        raise ValueError("ログインの有効期限が切れました。もう一度お試しください。")
    profile = exchange_google_code(code, pending)
    if not profile.get("email_verified", False):
        raise ValueError("Google アカウントのメールアドレスが確認されていません。")
    email = str(profile.get("email") or "").strip()
    name = str(profile.get("name") or "").strip()
    picture = str(profile.get("picture") or "").strip()
    return authenticate_email(email, name=name, picture=picture)


def authenticate_passcode(request: Request, entered: str) -> UserRecord:
    """合言葉（SYMNOTE_APP_PASSCODE）を検証してオーナーでログインする。"""
    if postgres.enabled():
        raise PermissionError("公開Web版ではGoogleログインを使用してください。")
    expected = app_passcode()
    if not expected:
        if auth_mode() == "local" and not e2e_mode_enabled() and is_loopback_request(request):
            return ensure_local_owner_user()
        raise PermissionError("合言葉ログインは設定されていません。")
    source = passcode_source(dict(request.headers))
    wait = PASSCODE_LIMITER.locked_for(source=source)
    if wait:
        minutes = int(wait // 60) + 1
        raise PermissionError(
            f"合言葉の入力を続けて間違えたため、あと約 {minutes} 分間ロックされています。"
        )
    if not PASSCODE_LIMITER.attempt(entered or "", expected, source=source):
        raise PermissionError("合言葉が違います。")
    return ensure_local_owner_user()


def authenticate_e2e_user(
    request: Request,
    *,
    email: str,
    name: str = "",
    is_owner: bool = False,
) -> UserRecord:
    """SYMNOTE_E2E=1 かつローカル接続時のみ使えるテストログイン。"""
    if not (e2e_mode_enabled() and is_loopback_request(request)):
        raise PermissionError("E2E ログインはローカル検証モードでのみ利用できます。")
    if is_owner:
        invite_user(email, is_owner=True, note="e2e-owner")
    return authenticate_email(email, name=name)


def apply_security_headers(response: Response) -> Response:
    """XSS・クリックジャッキング・MIMEスニッフィングを防ぐセキュリティヘッダーを付与する。"""
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


__all__ = [
    "CSRF_HEADER_NAME",
    "InviteRequiredError",
    "SESSION_COOKIE_NAME",
    "apply_security_headers",
    "auth_mode",
    "authenticate_e2e_user",
    "authenticate_passcode",
    "build_google_authorization_url",
    "clear_session_cookie",
    "complete_google_callback",
    "create_session",
    "e2e_mode_enabled",
    "google_oauth_config",
    "is_loopback_request",
    "resolve_request_user",
    "revoke_session",
    "set_session_cookie",
    "verify_csrf",
]
