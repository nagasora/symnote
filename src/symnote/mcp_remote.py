"""claude.ai などから HTTPS 経由で使うリモート MCP サーバー（Streamable HTTP + OAuth 2.1）。

Mac 上で 127.0.0.1 に待ち受け、Cloudflare Tunnel などで公開 URL を割り当てて使う。
OAuth の認可サーバーも兼ね、同意画面でパスフレーズを確認できた場合だけトークンを発行する。
公開するツールはローカル版（stdio）と同じで、既定では参照と候補提案のみ。

起動例::

    SYMNOTE_MCP_PUBLIC_URL=https://symnote.example.com \\
    SYMNOTE_MCP_PASSPHRASE='長いパスフレーズ' \\
    DB_PATH=/absolute/path/to/symnote.db python -m symnote.mcp_remote
"""

from __future__ import annotations

import argparse
import hmac
import html
import ipaddress
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from symnote import mcp_server
from symnote.core import db, oauth_store

PUBLIC_URL_ENV = "SYMNOTE_MCP_PUBLIC_URL"
PASSPHRASE_ENV = "SYMNOTE_MCP_PASSPHRASE"
HOST_ENV = "SYMNOTE_MCP_HOST"
PORT_ENV = "SYMNOTE_MCP_PORT"
REDIRECT_URIS_ENV = "SYMNOTE_MCP_REDIRECT_URIS"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MIN_PASSPHRASE_LENGTH = 12
SCOPE = "symnote"
CONSENT_PATH = "/oauth/consent"
DEFAULT_REDIRECT_URIS: Tuple[str, ...] = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
)
# ChatGPT は接続ごとに https://chatgpt.com/connector/oauth/{callback_id} を戻り先にする。
CHATGPT_CALLBACK_PREFIX = "https://chatgpt.com/connector/oauth/"

PENDING_TTL_SECONDS = 10 * 60
CODE_TTL_SECONDS = 5 * 60
ACCESS_TOKEN_TTL_SECONDS = 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_PENDING_REQUESTS = 500
MAX_PENDING_PER_CLIENT = 10
MAX_STATE_LENGTH = 512
# S256 の code_challenge は SHA-256 の Base64URL（パディングなし）で常に 43 文字。
CODE_CHALLENGE_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
MAX_CLIENTS = 200
MAX_FAILED_ATTEMPTS = 5
GLOBAL_MAX_FAILED_ATTEMPTS = 50
FAILURE_WINDOW_SECONDS = 15 * 60
LOCKOUT_SECONDS = 15 * 60
MAX_TRACKED_ADDRESSES = 10_000
MAX_CONSENT_BODY_BYTES = 4096
MAX_CLIENT_METADATA_BYTES = 4096
MAX_CLIENT_REDIRECT_URIS = 5
MAX_CLIENT_NAME_CHARS = 100
REGISTRATIONS_PER_MINUTE = 20
# Why not SYMNOTE_MCP_DIRECT_WRITES を共有: ローカル接続のために有効にすると、インターネットに
# 公開した接続でもメール本文などの指示で承認を迂回した書き込みができてしまう。
REMOTE_DIRECT_WRITES_ENV = "SYMNOTE_MCP_REMOTE_DIRECT_WRITES"

SECURITY_HEADERS: Dict[str, str] = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    # Why not no-referrer: Chrome はフォーム POST の Origin を "null" にしてしまい、
    # 同意画面の送信元チェックで自分自身の送信まで弾いてしまう。
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    # Why not form-action 'self': Chrome は承認後の claude.ai へのリダイレクトにも
    # form-action を適用し、承認がブロックされてしまう。
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
    ),
}


@dataclass(frozen=True)
class RemoteSettings:
    """リモート MCP サーバーの設定値。"""

    public_url: str
    passphrase: str
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    allowed_redirect_uris: Tuple[str, ...] = DEFAULT_REDIRECT_URIS
    allow_direct_writes: bool = False

    @property
    def resource_url(self) -> str:
        """MCP エンドポイント（トークンの対象リソース）の URL。"""
        return f"{self.public_url}/mcp"

    @property
    def public_host(self) -> str:
        """公開 URL のホスト部（ポートを含む）。"""
        return urlparse(self.public_url).netloc


def _is_loopback(hostname: Optional[str]) -> bool:
    """ホスト名がループバック（自分の端末）を指すかを返す。"""
    if not hostname:
        return False
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def load_remote_settings() -> RemoteSettings:
    """環境変数から設定を読み込み、危険な設定なら ValueError を送出する。"""
    raw_url = os.getenv(PUBLIC_URL_ENV, "").strip().rstrip("/")
    if not raw_url:
        raise ValueError(f"{PUBLIC_URL_ENV} に公開 URL（例: https://symnote.example.com）を設定してください。")
    parsed = urlparse(raw_url)
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError(
            f"{PUBLIC_URL_ENV} にはパスを付けず、https://ホスト名 だけを設定してください。"
        )
    if parsed.scheme != "https" and not (parsed.scheme == "http" and _is_loopback(parsed.hostname)):
        raise ValueError(f"{PUBLIC_URL_ENV} は https:// で始まる URL にしてください。")

    passphrase = os.getenv(PASSPHRASE_ENV, "")
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise ValueError(
            f"{PASSPHRASE_ENV} に {MIN_PASSPHRASE_LENGTH} 文字以上の"
            "パスフレーズを設定してください。"
        )

    port_text = os.getenv(PORT_ENV, str(DEFAULT_PORT)).strip()
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise ValueError(f"{PORT_ENV} は 1〜65535 の数値にしてください。")

    extra_uris = tuple(
        uri.strip() for uri in os.getenv(REDIRECT_URIS_ENV, "").split(",") if uri.strip()
    )
    return RemoteSettings(
        public_url=raw_url,
        passphrase=passphrase,
        host=os.getenv(HOST_ENV, DEFAULT_HOST).strip() or DEFAULT_HOST,
        port=int(port_text),
        allowed_redirect_uris=DEFAULT_REDIRECT_URIS + extra_uris,
        allow_direct_writes=os.getenv(REMOTE_DIRECT_WRITES_ENV, "").strip().lower()
        in {"1", "true", "yes"},
    )


def is_allowed_redirect_uri(uri: str, allowed: Tuple[str, ...]) -> bool:
    """登録を許すリダイレクト先かを返す。

    許可リストとの完全一致か、ループバックへの http（Claude Code などのローカル
    クライアント）だけを許す。任意の URL を許すと、攻撃者が自分のサーバーを戻り先に
    登録した接続へユーザーを誘導し、パスフレーズ入力後の認可コードを奪えてしまう。
    """
    if uri in allowed:
        return True
    parsed = urlparse(uri)
    if uri.startswith(CHATGPT_CALLBACK_PREFIX):
        callback_id = uri[len(CHATGPT_CALLBACK_PREFIX):]
        # Why not 前方一致だけ: "/../" やクエリ付きで別パスへ戻されないよう、1 区画の ID に限る。
        return (
            not parsed.query
            and not parsed.fragment
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", callback_id) is not None
        )
    return parsed.scheme == "http" and _is_loopback(parsed.hostname)


@dataclass
class PendingAuthorization:
    """同意画面で承認を待っている認可リクエスト。"""

    client_id: str
    client_name: str
    params: AuthorizationParams
    expires_at: float


@dataclass(frozen=True)
class ConsentOutcome:
    """同意画面の送信結果。成功時は戻り先 URL、失敗時は表示するメッセージを持つ。"""

    redirect_url: Optional[str] = None
    error: Optional[str] = None


@dataclass
class _FailureTracker:
    """パスフレーズの連続失敗を数え、上限を超えたら一定時間ロックする。"""

    max_attempts: int = MAX_FAILED_ATTEMPTS
    failures: List[float] = field(default_factory=list)
    locked_until: float = 0.0

    def is_locked(self, now: float) -> bool:
        """ロック中かを返す。"""
        return now < self.locked_until

    def record_failure(self, now: float) -> None:
        """失敗を記録し、窓内の失敗が上限に達したらロックする。"""
        self.failures = [t for t in self.failures if now - t < FAILURE_WINDOW_SECONDS]
        self.failures.append(now)
        if len(self.failures) >= self.max_attempts:
            self.locked_until = now + LOCKOUT_SECONDS
            self.failures.clear()

    def reset(self) -> None:
        """成功時に失敗履歴を消す。"""
        self.failures.clear()


class SymNoteOAuthProvider:
    """MCP SDK の OAuthAuthorizationServerProvider を満たす、単一ユーザー向け認可サーバー。

    クライアントとトークンは SQLite に保存し、短命な認可リクエストと認可コードは
    メモリに置く（再起動したら接続をやり直せば済むため）。
    """

    def __init__(self, settings: RemoteSettings, clock: Callable[[], float] = time.time) -> None:
        self._settings = settings
        self._clock = clock
        self._pending: Dict[str, PendingAuthorization] = {}
        self._codes: Dict[str, AuthorizationCode] = {}
        self._address_failures: Dict[str, _FailureTracker] = {}
        self._global_failures = _FailureTracker(max_attempts=GLOBAL_MAX_FAILED_ATTEMPTS)
        self._registrations: List[float] = []

    # --- クライアント登録 -------------------------------------------------

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        """登録済みクライアントを返す。"""
        stored = oauth_store.load_client(client_id)
        if stored is None:
            return None
        return OAuthClientInformationFull.model_validate_json(stored)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """大きさ・頻度・リダイレクト先を検査してからクライアントを保存する。"""
        now = self._clock()
        self._registrations = [t for t in self._registrations if t > now - 60]
        if len(self._registrations) >= REGISTRATIONS_PER_MINUTE:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="登録が集中しています。しばらく待ってから再試行してください。",
            )
        metadata = client_info.model_dump_json()
        if (
            len(metadata.encode("utf-8")) > MAX_CLIENT_METADATA_BYTES
            or len(client_info.redirect_uris or []) > MAX_CLIENT_REDIRECT_URIS
            or len(client_info.client_name or "") > MAX_CLIENT_NAME_CHARS
        ):
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="クライアント情報が大きすぎます。",
            )
        self._registrations.append(now)
        redirect_uris = [str(uri) for uri in client_info.redirect_uris or []]
        rejected = [
            uri
            for uri in redirect_uris
            if not is_allowed_redirect_uri(uri, self._settings.allowed_redirect_uris)
        ]
        if not redirect_uris or rejected:
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="このサーバーに登録できないリダイレクト先です: "
                + ", ".join(rejected),
            )
        # Why not 上限で登録を拒否: 匿名の大量登録だけで本人の新規接続を締め出せてしまうため、
        # 承認前（トークン未発行）の古い登録から追い出して枠を空ける。
        oauth_store.evict_unused_clients(MAX_CLIENTS)
        if oauth_store.count_clients() >= MAX_CLIENTS:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="承認済みの接続が多すぎます。--revoke-all で整理してください。",
            )
        assert client_info.client_id is not None
        oauth_store.save_client(client_info.client_id, metadata)

    # --- 認可（同意画面） ---------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """認可リクエストを保留し、同意画面の URL を返す。"""
        requested_resource = params.resource.rstrip("/") if params.resource else None
        if requested_resource is not None and requested_resource != self._settings.resource_url:
            raise AuthorizeError(
                error="invalid_request",
                error_description="このサーバー以外のリソースは要求できません。",
            )
        if params.state is not None and len(params.state) > MAX_STATE_LENGTH:
            raise AuthorizeError(error="invalid_request", error_description="state が長すぎます。")
        if not CODE_CHALLENGE_PATTERN.fullmatch(params.code_challenge or ""):
            raise AuthorizeError(
                error="invalid_request", error_description="code_challenge の形式が正しくありません。"
            )
        self._prune()
        assert client.client_id is not None
        self._make_room_for(client.client_id)
        request_id = secrets.token_urlsafe(32)
        self._pending[request_id] = PendingAuthorization(
            client_id=client.client_id,
            client_name=client.client_name or client.client_id,
            params=params,
            expires_at=self._clock() + PENDING_TTL_SECONDS,
        )
        return f"{self._settings.public_url}{CONSENT_PATH}?request={request_id}"

    def _make_room_for(self, client_id: str) -> None:
        """保留の上限に達していれば、最も多く保留しているクライアントの最古の要求を消す。

        Why not 全体で最古を消す: 匿名で登録したクライアントから連投するだけで、
        本人が開いている同意画面の要求が追い出されてしまう。
        """
        own = [key for key, item in self._pending.items() if item.client_id == client_id]
        if len(own) >= MAX_PENDING_PER_CLIENT:
            victims = own
        elif len(self._pending) >= MAX_PENDING_REQUESTS:
            counts: Dict[str, int] = {}
            for item in self._pending.values():
                counts[item.client_id] = counts.get(item.client_id, 0) + 1
            heaviest = max(counts, key=lambda key: counts[key])
            victims = [key for key, item in self._pending.items() if item.client_id == heaviest]
        else:
            return
        del self._pending[min(victims, key=lambda key: self._pending[key].expires_at)]

    def pending_request(self, request_id: str) -> Optional[PendingAuthorization]:
        """有効期限内の保留中リクエストを返す。"""
        self._prune()
        return self._pending.get(request_id)

    def approve(self, request_id: str, passphrase: str, address: str = "") -> ConsentOutcome:
        """パスフレーズを確認し、正しければ認可コード付きの戻り先 URL を返す。

        失敗は送信元アドレスごとに数えてロックし、別の場所にいる本人まで締め出さないようにする。
        アドレスを替えながらの総当たりには、全体の失敗数による広めの上限で備える。
        """
        now = self._clock()
        address_tracker = self._address_tracker(address)
        if address_tracker.is_locked(now) or self._global_failures.is_locked(now):
            return ConsentOutcome(
                error="失敗が続いたため一時的にロックしています。"
                "15 分ほど待ってから再度お試しください。"
            )
        pending = self.pending_request(request_id)
        if pending is None:
            return ConsentOutcome(
                error="接続リクエストの有効期限が切れました。claude.ai から接続し直してください。"
            )
        expected = self._settings.passphrase.encode("utf-8")
        if not hmac.compare_digest(passphrase.encode("utf-8"), expected):
            address_tracker.record_failure(now)
            self._global_failures.record_failure(now)
            return ConsentOutcome(error="パスフレーズが違います。")
        address_tracker.reset()
        del self._pending[request_id]
        code = secrets.token_urlsafe(32)
        params = pending.params
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [SCOPE],
            expires_at=now + CODE_TTL_SECONDS,
            client_id=pending.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource or self._settings.resource_url,
        )
        return ConsentOutcome(
            redirect_url=construct_redirect_uri(
                str(params.redirect_uri), code=code, state=params.state
            )
        )

    def _address_tracker(self, address: str) -> _FailureTracker:
        """送信元アドレスの失敗記録を返す。記録が増えすぎたらロック中以外を捨てる。"""
        if address not in self._address_failures:
            if len(self._address_failures) >= MAX_TRACKED_ADDRESSES:
                now = self._clock()
                self._address_failures = {
                    key: tracker
                    for key, tracker in self._address_failures.items()
                    if tracker.is_locked(now)
                }
            self._address_failures[address] = _FailureTracker()
        return self._address_failures[address]

    def deny(self, request_id: str) -> Optional[str]:
        """リクエストを拒否し、access_denied を伝える戻り先 URL を返す。"""
        pending = self._pending.pop(request_id, None)
        if pending is None or pending.expires_at <= self._clock():
            return None
        return construct_redirect_uri(
            str(pending.params.redirect_uri), error="access_denied", state=pending.params.state
        )

    def _prune(self) -> None:
        """期限切れの保留リクエストと認可コードを捨てる。"""
        now = self._clock()
        self._pending = {k: v for k, v in self._pending.items() if v.expires_at > now}
        self._codes = {k: v for k, v in self._codes.items() if v.expires_at > now}

    # --- トークン ---------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        """クライアントに発行した有効な認可コードを返す。"""
        self._prune()
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """認可コードを 1 回だけ使えるようにしてトークンへ交換する。"""
        self._codes.pop(authorization_code.code, None)
        return self._issue_tokens(
            authorization_code.client_id, authorization_code.scopes, authorization_code.resource
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[RefreshToken]:
        """クライアントに発行した有効なリフレッシュトークンを返す。

        使用済みのトークンが再び提示されたら盗用とみなし、その接続のトークンをすべて失効させる
        （本人と攻撃者のどちらが先に使ったかは区別できないため、両方に再接続を求める）。
        """
        record = oauth_store.load_token(refresh_token, "refresh", self._clock())
        if record is None:
            reused_by = oauth_store.consumed_refresh_token_client(refresh_token)
            if reused_by is not None:
                oauth_store.delete_client_tokens(reused_by)
            return None
        if record.client_id != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=record.client_id,
            scopes=record.scopes,
            expires_at=record.expires_at,
            resource=record.resource,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: List[str]
    ) -> OAuthToken:
        """リフレッシュトークンを使い捨てにして新しい組を発行する（ローテーション）。"""
        if not oauth_store.consume_refresh_token(refresh_token.token, self._clock()):
            raise TokenError(
                error="invalid_grant", error_description="refresh token は使用済みです。"
            )
        return self._issue_tokens(refresh_token.client_id, scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        """有効なアクセストークンを返す。"""
        record = oauth_store.load_token(token, "access", self._clock())
        if record is None:
            return None
        return AccessToken(
            token=token,
            client_id=record.client_id,
            scopes=record.scopes,
            expires_at=record.expires_at,
            resource=record.resource,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """失効要求を受けたら、そのクライアントのトークンをすべて無効にする。"""
        oauth_store.delete_client_tokens(token.client_id)

    def _issue_tokens(
        self, client_id: str, scopes: List[str], resource: Optional[str]
    ) -> OAuthToken:
        """アクセストークンとリフレッシュトークンを発行して保存する。"""
        now = int(self._clock())
        oauth_store.purge_expired_tokens(now)
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        oauth_store.store_token(
            access_token, "access", client_id, scopes, resource, now + ACCESS_TOKEN_TTL_SECONDS
        )
        oauth_store.store_token(
            refresh_token, "refresh", client_id, scopes, resource, now + REFRESH_TOKEN_TTL_SECONDS
        )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh_token,
            scope=" ".join(scopes),
        )


# --- 同意画面 --------------------------------------------------------------


def render_consent_page(
    request_id: str,
    pending: Optional[PendingAuthorization],
    error: Optional[str] = None,
    allow_direct_writes: bool = False,
) -> str:
    """同意画面の HTML を返す。クライアント名など外部由来の値はすべてエスケープする。"""
    style = (
        "body{font-family:-apple-system,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem;"
        "color:#222}input{width:100%;padding:.6rem;font-size:1rem;box-sizing:border-box}"
        "button{padding:.6rem 1.2rem;font-size:1rem;margin:.8rem .5rem 0 0}"
        ".error{color:#b00020}.muted{color:#666;font-size:.9rem}"
    )
    if pending is None:
        body = (
            "<h1>SymNote</h1><p class='error'>"
            "接続リクエストが見つからないか、有効期限が切れています。"
            "claude.ai から接続し直してください。</p>"
        )
    else:
        redirect_host = urlparse(str(pending.params.redirect_uri)).netloc
        error_html = f"<p class='error'>{html.escape(error)}</p>" if error else ""
        scope_text = (
            "参照・候補提案に加えて、<strong>タスクの直接追加・完了</strong>"
            if allow_direct_writes
            else "参照・候補提案"
        )
        body = (
            "<h1>SymNote への接続</h1>"
            f"<p><strong>{html.escape(pending.client_name)}</strong> が SymNote のタスクとメモの"
            f"{scope_text}を求めています。</p>"
            f"<p class='muted'>承認後の戻り先: {html.escape(redirect_host)}</p>"
            f"{error_html}"
            f"<form method='post' action='{CONSENT_PATH}'>"
            f"<input type='hidden' name='request' value='{html.escape(request_id, quote=True)}'>"
            "<label>パスフレーズ<input type='password' name='passphrase'"
            " autocomplete='current-password' autofocus></label>"
            "<button type='submit' name='action' value='approve'>承認する</button>"
            "<button type='submit' name='action' value='deny'>拒否する</button>"
            "</form>"
        )
    return (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>SymNote への接続</title><style>{style}</style></head><body>{body}</body></html>"
    )


def _html(content: str, status_code: int = 200) -> HTMLResponse:
    """セキュリティヘッダー付きの HTML レスポンスを返す。"""
    return HTMLResponse(content, status_code=status_code, headers=SECURITY_HEADERS)


def _client_address(request: Request) -> str:
    """送信元アドレスを返す。

    127.0.0.1 で待ち受けて Cloudflare Tunnel だけが届く前提のため、Cloudflare が上書きする
    CF-Connecting-IP を使う。トンネルを経由しない直結では偽装できるが、その場合も
    全体の失敗上限は効く。
    """
    forwarded = request.headers.get("cf-connecting-ip", "").strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else ""


async def _read_limited_form(request: Request) -> Optional[Dict[str, str]]:
    """URL エンコードのフォームを上限付きで読む。形式違い・上限超過なら None。"""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return None
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_CONSENT_BODY_BYTES:
        return None
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_CONSENT_BODY_BYTES:
            return None
    fields = parse_qs(body.decode("utf-8", errors="replace"), max_num_fields=10)
    return {key: values[0] for key, values in fields.items()}


def is_same_origin_post(request: Request, public_url: str) -> bool:
    """同意フォームの POST が自分の画面から送られたものかを Origin / Sec-Fetch-Site で判定する。"""
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    if origin is None or origin == public_url:
        return True
    # プライバシー設定や古い同意画面のキャッシュで Origin が "null" になるブラウザーがあるため、
    # その場合だけブラウザー自身が付ける Sec-Fetch-Site で同一オリジンかを確かめる。
    return origin == "null" and request.headers.get("sec-fetch-site") == "same-origin"


def make_consent_handler(
    provider: SymNoteOAuthProvider, settings: RemoteSettings
) -> Callable[[Request], Any]:
    """同意画面（GET で表示、POST で承認・拒否）の Starlette ハンドラーを作る。"""
    allowed_hosts = {
        settings.public_host,
        f"127.0.0.1:{settings.port}",
        f"localhost:{settings.port}",
    }

    async def handle(request: Request) -> Response:
        if request.headers.get("host", "") not in allowed_hosts:
            return Response("Invalid Host header", status_code=421)
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            pending = provider.pending_request(request_id)
            page = render_consent_page(
                request_id, pending, allow_direct_writes=settings.allow_direct_writes
            )
            return _html(page, 200 if pending else 404)

        # 承認にはパスフレーズが要るため CSRF だけでは突破されないが、別サイトからの送信は
        # 拒否・ロック誘発に悪用できるので、ブラウザーが付ける Origin で弾いておく。
        if not is_same_origin_post(request, settings.public_url):
            return Response("Invalid Origin", status_code=403)
        form = await _read_limited_form(request)
        if form is None:
            return Response("Invalid form", status_code=400)
        request_id = form.get("request", "")
        if form.get("action") == "deny":
            redirect_url = provider.deny(request_id)
            if redirect_url is None:
                return _html(render_consent_page(request_id, None), 404)
            return RedirectResponse(redirect_url, status_code=303, headers=SECURITY_HEADERS)

        outcome = provider.approve(
            request_id, form.get("passphrase", ""), address=_client_address(request)
        )
        if outcome.redirect_url is not None:
            return RedirectResponse(outcome.redirect_url, status_code=303, headers=SECURITY_HEADERS)
        pending = provider.pending_request(request_id)
        page = render_consent_page(
            request_id, pending, outcome.error, allow_direct_writes=settings.allow_direct_writes
        )
        return _html(page, 400 if pending else 404)

    return handle


# --- 起動 ------------------------------------------------------------------


def build_remote_server(
    settings: RemoteSettings, provider: Optional[SymNoteOAuthProvider] = None
) -> Any:
    """OAuth で保護した Streamable HTTP 用の FastMCP サーバーを生成する。"""
    provider = provider or SymNoteOAuthProvider(settings)
    auth = AuthSettings(
        issuer_url=settings.public_url,
        resource_server_url=settings.resource_url,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
        validate_token_resource=True,
    )
    # トンネル経由のリクエストは公開ホスト名の Host ヘッダーで届くため、
    # 既定の localhost 限定のままだと DNS リバインディング対策で 421 になる。
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            settings.public_host,
            f"127.0.0.1:{settings.port}",
            f"localhost:{settings.port}",
        ],
        allowed_origins=[settings.public_url, "https://claude.ai", "https://claude.com"],
    )
    server = mcp_server.build_server(
        allow_direct_writes=settings.allow_direct_writes,
        host=settings.host,
        port=settings.port,
        stateless_http=True,
        auth=auth,
        auth_server_provider=provider,
        transport_security=transport_security,
    )
    consent_handler = make_consent_handler(provider, settings)
    server.custom_route(CONSENT_PATH, methods=["GET", "POST"])(consent_handler)
    return server


def main(argv: Optional[List[str]] = None) -> None:
    """設定を検証してリモート MCP サーバーを起動する。"""
    parser = argparse.ArgumentParser(description="SymNote リモート MCP サーバー")
    parser.add_argument(
        "--revoke-all", action="store_true", help="登録済みの接続とトークンをすべて削除して終了する"
    )
    args = parser.parse_args(argv)

    mcp_server._ensure_absolute_db_path()
    db.init_db()
    if args.revoke_all:
        print(f"{oauth_store.revoke_all()} 件の接続を削除しました。")
        return
    try:
        settings = load_remote_settings()
    except ValueError as error:
        print(f"SymNote リモート MCP: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    if not _is_loopback(settings.host):
        print(
            f"SymNote リモート MCP: {HOST_ENV}={settings.host} で待ち受けます。"
            " トンネル以外から直接届かないようにしてください。",
            file=sys.stderr,
        )
    print(
        f"SymNote リモート MCP: {settings.resource_url} を"
        f" {settings.host}:{settings.port} で待ち受けます。",
        file=sys.stderr,
    )
    build_remote_server(settings).run(transport="streamable-http")


if __name__ == "__main__":
    main()
