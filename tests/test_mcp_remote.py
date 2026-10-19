"""リモート MCP サーバーが、パスフレーズ承認を経た OAuth トークンでのみ使えることの保証。"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from typing import Any, Dict, Iterator, List, Tuple
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("mcp")

from starlette.testclient import TestClient  # noqa: E402

from symnote import mcp_remote  # noqa: E402
from symnote.core import oauth_store  # noqa: E402
from symnote.core.db import init_db  # noqa: E402

PUBLIC_URL = "https://symnote.example.com"
PASSPHRASE = "correct horse battery staple"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64


class FakeClock:
    """テストから進められる時計。"""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def database(monkeypatch, tmp_path):
    """一時 DB を初期化して返す。"""
    path = tmp_path / "remote.db"
    monkeypatch.setenv("DB_PATH", str(path))
    init_db()
    return path


@pytest.fixture()
def clock() -> FakeClock:
    """固定時刻の時計。"""
    return FakeClock()


@pytest.fixture()
def remote(database, clock) -> Iterator[Tuple[TestClient, mcp_remote.SymNoteOAuthProvider]]:
    """公開 URL 名義で動くリモートサーバーのテストクライアントを返す。"""
    settings = mcp_remote.RemoteSettings(public_url=PUBLIC_URL, passphrase=PASSPHRASE)
    provider = mcp_remote.SymNoteOAuthProvider(settings, clock=clock)
    app = mcp_remote.build_remote_server(settings, provider).streamable_http_app()
    with TestClient(app, base_url=PUBLIC_URL) as client:
        yield client, provider


def _challenge(verifier: str = VERIFIER) -> str:
    """PKCE S256 のコードチャレンジを計算する。"""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _register(client: TestClient, redirect_uri: str = CLAUDE_CALLBACK) -> Dict[str, Any]:
    """クライアントを動的登録する。"""
    response = client.post(
        "/register",
        json={
            "client_name": "Claude",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    return {"status": response.status_code, **response.json()}


def _start_authorization(client: TestClient, client_id: str, **extra: str) -> str:
    """認可リクエストを送り、同意画面の request ID を返す。"""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CLAUDE_CALLBACK,
        "code_challenge": _challenge(),
        "code_challenge_method": "S256",
        "state": "xyz",
        "resource": f"{PUBLIC_URL}/mcp",
        **extra,
    }
    response = client.get("/authorize", params=params, follow_redirects=False)
    assert response.status_code == 302, response.text
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{PUBLIC_URL}/oauth/consent"
    return parse_qs(location.query)["request"][0]


def _approve(
    client: TestClient, request_id: str, passphrase: str = PASSPHRASE, address: str = "198.51.100.1"
):
    """同意画面でパスフレーズを送信する。"""
    return client.post(
        "/oauth/consent",
        data={"request": request_id, "passphrase": passphrase, "action": "approve"},
        headers={"CF-Connecting-IP": address},
        follow_redirects=False,
    )


def _authorize_and_get_tokens(client: TestClient) -> Tuple[str, Dict[str, Any]]:
    """登録から承認、トークン交換までを通し、client_id とトークン応答を返す。"""
    client_id = _register(client)["client_id"]
    response = _approve(client, _start_authorization(client, client_id))
    assert response.status_code == 303
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["state"] == ["xyz"]
    token = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": CLAUDE_CALLBACK,
            "client_id": client_id,
            "code_verifier": VERIFIER,
            "resource": f"{PUBLIC_URL}/mcp",
        },
    )
    assert token.status_code == 200, token.text
    return client_id, token.json()


def _list_tools(client: TestClient, access_token: str | None):
    """MCP の tools/list を送る。"""
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )


def _tool_names(response) -> List[str]:
    """SSE または JSON の tools/list 応答からツール名を取り出す。"""
    if response.headers["content-type"].startswith("text/event-stream"):
        payload = next(
            line[len("data:"):] for line in response.text.splitlines() if line.startswith("data:")
        )
    else:
        payload = response.text
    return [tool["name"] for tool in json.loads(payload)["result"]["tools"]]


# --- 設定 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "passphrase", "message"),
    [
        ("", PASSPHRASE, "SYMNOTE_MCP_PUBLIC_URL"),
        ("http://symnote.example.com", PASSPHRASE, "https://"),
        ("https://symnote.example.com/mcp", PASSPHRASE, "パス"),
        (PUBLIC_URL, "short", "12 文字以上"),
    ],
)
def test_unsafe_settings_are_refused(monkeypatch, url, passphrase, message) -> None:
    monkeypatch.setenv("SYMNOTE_MCP_PUBLIC_URL", url)
    monkeypatch.setenv("SYMNOTE_MCP_PASSPHRASE", passphrase)
    with pytest.raises(ValueError, match=message):
        mcp_remote.load_remote_settings()


def test_settings_accept_https_and_loopback_http(monkeypatch) -> None:
    monkeypatch.setenv("SYMNOTE_MCP_PASSPHRASE", PASSPHRASE)
    monkeypatch.setenv("SYMNOTE_MCP_PUBLIC_URL", f"{PUBLIC_URL}/")
    assert mcp_remote.load_remote_settings().resource_url == f"{PUBLIC_URL}/mcp"
    monkeypatch.setenv("SYMNOTE_MCP_PUBLIC_URL", "http://127.0.0.1:8765")
    assert mcp_remote.load_remote_settings().public_host == "127.0.0.1:8765"


def test_redirect_allowlist_accepts_claude_and_loopback_only() -> None:
    allowed = mcp_remote.DEFAULT_REDIRECT_URIS
    assert mcp_remote.is_allowed_redirect_uri(CLAUDE_CALLBACK, allowed)
    assert mcp_remote.is_allowed_redirect_uri("http://localhost:53123/callback", allowed)
    assert mcp_remote.is_allowed_redirect_uri("http://127.0.0.1:9/cb", allowed)
    assert not mcp_remote.is_allowed_redirect_uri("https://evil.example/cb", allowed)
    assert not mcp_remote.is_allowed_redirect_uri("https://localhost/cb", allowed)
    assert not mcp_remote.is_allowed_redirect_uri("http://claude.ai/api/mcp/auth_callback", allowed)


def test_redirect_allowlist_accepts_chatgpt_callbacks_only() -> None:
    allowed = mcp_remote.DEFAULT_REDIRECT_URIS
    assert mcp_remote.is_allowed_redirect_uri(
        "https://chatgpt.com/connector_platform_oauth_redirect", allowed
    )
    assert mcp_remote.is_allowed_redirect_uri(
        "https://chatgpt.com/connector/oauth/abc_DEF-123", allowed
    )
    for uri in (
        "https://chatgpt.com/connector/oauth/",
        "https://chatgpt.com/connector/oauth/a/b",
        "https://chatgpt.com/connector/oauth/../evil",
        "https://chatgpt.com/connector/oauth/abc?next=https://evil.example",
        "https://chatgpt.com/connector/oauth/abc#x",
        "http://chatgpt.com/connector/oauth/abc",
        "https://chatgpt.com.evil.example/connector/oauth/abc",
    ):
        assert not mcp_remote.is_allowed_redirect_uri(uri, allowed), uri


# --- OAuth フロー ----------------------------------------------------------------


def test_mcp_endpoint_requires_bearer_token(remote) -> None:
    client, _ = remote
    response = _list_tools(client, None)
    assert response.status_code == 401
    assert "oauth-protected-resource/mcp" in response.headers["www-authenticate"]


def test_metadata_advertises_registration_and_pkce(remote) -> None:
    client, _ = remote
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["registration_endpoint"] == f"{PUBLIC_URL}/register"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == f"{PUBLIC_URL}/mcp"


def test_registration_rejects_untrusted_redirect(remote) -> None:
    client, _ = remote
    result = _register(client, "https://evil.example/callback")
    assert result["status"] == 400
    assert result["error"] == "invalid_redirect_uri"


def test_full_flow_grants_read_and_propose_tools_only(remote) -> None:
    client, _ = remote
    _, tokens = _authorize_and_get_tokens(client)
    response = _list_tools(client, tokens["access_token"])
    assert response.status_code == 200, response.text
    names = set(_tool_names(response))
    assert {"list_tasks", "propose_tasks", "search_notes"} <= names
    assert "add_task" not in names and "complete_task" not in names


def test_wrong_passphrase_issues_no_code(remote) -> None:
    client, _ = remote
    client_id = _register(client)["client_id"]
    request_id = _start_authorization(client, client_id)
    response = _approve(client, request_id, "wrong passphrase!!")
    assert response.status_code == 400
    assert "パスフレーズが違います" in response.text
    assert _approve(client, request_id).status_code == 303


def test_repeated_failures_lock_consent_temporarily(remote, clock) -> None:
    client, _ = remote
    client_id = _register(client)["client_id"]
    for _ in range(mcp_remote.MAX_FAILED_ATTEMPTS):
        request_id = _start_authorization(client, client_id)
        _approve(client, request_id, "wrong passphrase!!")
    request_id = _start_authorization(client, client_id)
    assert "ロック" in _approve(client, request_id).text
    clock.now += mcp_remote.LOCKOUT_SECONDS + 1
    request_id = _start_authorization(client, client_id)
    assert _approve(client, request_id).status_code == 303


def test_deny_redirects_with_access_denied(remote) -> None:
    client, _ = remote
    client_id = _register(client)["client_id"]
    request_id = _start_authorization(client, client_id)
    response = client.post(
        "/oauth/consent", data={"request": request_id, "action": "deny"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert parse_qs(urlparse(response.headers["location"]).query)["error"] == ["access_denied"]


def test_consent_page_escapes_client_name(remote) -> None:
    client, _ = remote
    response = client.post(
        "/register",
        json={"client_name": "<script>x</script>", "redirect_uris": [CLAUDE_CALLBACK],
              "token_endpoint_auth_method": "none"},
    )
    request_id = _start_authorization(client, response.json()["client_id"])
    page = client.get("/oauth/consent", params={"request": request_id})
    assert "<script>x</script>" not in page.text
    assert "&lt;script&gt;" in page.text
    assert page.headers["x-frame-options"] == "DENY"


def test_expired_consent_request_is_rejected(remote, clock) -> None:
    client, _ = remote
    client_id = _register(client)["client_id"]
    request_id = _start_authorization(client, client_id)
    clock.now += mcp_remote.PENDING_TTL_SECONDS + 1
    assert _approve(client, request_id).status_code == 404


def test_foreign_resource_is_rejected(remote) -> None:
    client, _ = remote
    client_id = _register(client)["client_id"]
    response = client.get(
        "/authorize",
        params={
            "response_type": "code", "client_id": client_id, "redirect_uri": CLAUDE_CALLBACK,
            "code_challenge": _challenge(), "code_challenge_method": "S256", "state": "s",
            "resource": "https://other.example/mcp",
        },
        follow_redirects=False,
    )
    assert "invalid_request" in response.headers.get("location", response.text)


def test_authorization_code_is_single_use(remote) -> None:
    client, _ = remote
    client_id = _register(client)["client_id"]
    location = _approve(client, _start_authorization(client, client_id)).headers["location"]
    code = parse_qs(urlparse(location).query)["code"][0]
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": CLAUDE_CALLBACK,
            "client_id": client_id, "code_verifier": VERIFIER}
    assert client.post("/token", data=form).status_code == 200
    assert client.post("/token", data=form).status_code == 400


def test_refresh_rotates_and_old_refresh_token_dies(remote) -> None:
    client, _ = remote
    client_id, tokens = _authorize_and_get_tokens(client)
    form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
            "client_id": client_id}
    refreshed = client.post("/token", data=form)
    assert refreshed.status_code == 200
    assert _list_tools(client, refreshed.json()["access_token"]).status_code == 200
    assert client.post("/token", data=form).status_code == 400


def test_access_token_expires(remote, clock) -> None:
    client, _ = remote
    _, tokens = _authorize_and_get_tokens(client)
    clock.now += mcp_remote.ACCESS_TOKEN_TTL_SECONDS + 1
    assert oauth_store.load_token(tokens["access_token"], "access", clock.now) is None


def test_tokens_are_stored_only_as_hashes(remote, database) -> None:
    client, _ = remote
    _, tokens = _authorize_and_get_tokens(client)
    with sqlite3.connect(database) as conn:
        stored = {row[0] for row in conn.execute("SELECT token_hash FROM oauth_tokens")}
    assert tokens["access_token"] not in stored
    assert oauth_store.hash_token(tokens["access_token"]) in stored


def test_revoke_all_invalidates_existing_tokens(remote) -> None:
    client, _ = remote
    _, tokens = _authorize_and_get_tokens(client)
    assert oauth_store.revoke_all() == 1
    assert _list_tools(client, tokens["access_token"]).status_code == 401


def test_reused_refresh_token_revokes_the_whole_connection(remote) -> None:
    client, _ = remote
    client_id, tokens = _authorize_and_get_tokens(client)
    form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
            "client_id": client_id}
    successor = client.post("/token", data=form).json()
    assert client.post("/token", data=form).status_code == 400
    assert _list_tools(client, successor["access_token"]).status_code == 401
    successor_form = {**form, "refresh_token": successor["refresh_token"]}
    assert client.post("/token", data=successor_form).status_code == 400


def test_registration_flood_evicts_unapproved_clients_only(remote, monkeypatch) -> None:
    client, _ = remote
    monkeypatch.setattr(mcp_remote, "MAX_CLIENTS", 3)
    approved_id, tokens = _authorize_and_get_tokens(client)
    for _ in range(5):
        assert _register(client)["status"] == 201
    assert oauth_store.count_clients() <= 3
    assert oauth_store.load_client(approved_id) is not None
    assert _list_tools(client, tokens["access_token"]).status_code == 200


def test_lockout_is_per_address(remote) -> None:
    client, _ = remote
    client_id = _register(client)["client_id"]
    for _ in range(mcp_remote.MAX_FAILED_ATTEMPTS):
        request_id = _start_authorization(client, client_id)
        _approve(client, request_id, "wrong passphrase!!", "203.0.113.9")
    locked = _approve(client, _start_authorization(client, client_id), address="203.0.113.9")
    assert "ロック" in locked.text
    owner = _approve(client, _start_authorization(client, client_id), address="198.51.100.7")
    assert owner.status_code == 303


def test_global_failure_ceiling_stops_distributed_guessing(remote) -> None:
    client, _ = remote
    client_id = _register(client)["client_id"]
    for index in range(mcp_remote.GLOBAL_MAX_FAILED_ATTEMPTS):
        _approve(client, _start_authorization(client, client_id), "wrong passphrase!!",
                 f"192.0.2.{index}")
    response = _approve(client, _start_authorization(client, client_id), address="198.51.100.7")
    assert "ロック" in response.text


def test_consent_rejects_oversized_or_non_form_bodies(remote) -> None:
    client, _ = remote
    client_id = _register(client)["client_id"]
    request_id = _start_authorization(client, client_id)
    oversized = client.post(
        "/oauth/consent",
        data={"request": request_id, "passphrase": "x" * 10_000, "action": "approve"},
    )
    assert oversized.status_code == 400
    multipart = client.post(
        "/oauth/consent", data={"request": request_id}, files={"f": ("a.txt", b"abc")}
    )
    assert multipart.status_code == 400
    assert _approve(client, request_id).status_code == 303


def test_consent_rejects_foreign_origin_and_host(remote) -> None:
    client, _ = remote
    client_id = _register(client)["client_id"]
    request_id = _start_authorization(client, client_id)
    foreign = client.post(
        "/oauth/consent",
        data={"request": request_id, "action": "deny"},
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert foreign.status_code == 403
    rebinding = client.get(
        "/oauth/consent", params={"request": request_id}, headers={"Host": "evil.example"}
    )
    assert rebinding.status_code == 421
    same_origin = client.post(
        "/oauth/consent",
        data={"request": request_id, "action": "deny"},
        headers={"Origin": PUBLIC_URL},
        follow_redirects=False,
    )
    assert same_origin.status_code == 303


def test_consent_accepts_null_origin_only_from_same_site_fetch(remote) -> None:
    """Origin が null でも、ブラウザーが same-origin と申告した送信だけは受け付ける。"""
    client, _ = remote
    client_id = _register(client)["client_id"]
    request_id = _start_authorization(client, client_id)
    cross = client.post(
        "/oauth/consent",
        data={"request": request_id, "action": "deny"},
        headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )
    assert cross.status_code == 403
    same = client.post(
        "/oauth/consent",
        data={"request": request_id, "action": "deny"},
        headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert same.status_code == 303


def test_consent_page_keeps_origin_on_form_post(remote) -> None:
    """同意画面は no-referrer を使わず、フォーム送信で Origin が送られるようにする。"""
    client, _ = remote
    client_id = _register(client)["client_id"]
    request_id = _start_authorization(client, client_id)
    page = client.get("/oauth/consent", params={"request": request_id})
    assert page.headers["referrer-policy"] == "same-origin"


def test_consent_rejects_cross_site_fetch_without_origin(remote) -> None:
    """Origin が無くても、ブラウザーが cross-site と申告した送信は拒否する。"""
    client, _ = remote
    client_id = _register(client)["client_id"]
    request_id = _start_authorization(client, client_id)
    response = client.post(
        "/oauth/consent",
        data={"request": request_id, "action": "deny"},
        headers={"Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_registration_rejects_oversized_client_metadata(remote) -> None:
    """巨大な名前や多数のリダイレクト先を持つ登録は保存しない。"""
    client, _ = remote
    response = client.post(
        "/register",
        json={
            "client_name": "x" * (mcp_remote.MAX_CLIENT_NAME_CHARS + 1),
            "redirect_uris": [CLAUDE_CALLBACK],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code == 400
    assert oauth_store.count_clients() == 0


def test_registration_burst_is_rate_limited(remote, clock, monkeypatch) -> None:
    """1 分あたりの登録数を超えたら拒否し、時間がたてば再び登録できる。"""
    client, _ = remote
    monkeypatch.setattr(mcp_remote, "REGISTRATIONS_PER_MINUTE", 2)
    assert _register(client)["status"] == 201
    assert _register(client)["status"] == 201
    assert _register(client)["status"] == 400
    clock.now += 61
    assert _register(client)["status"] == 201


def test_remote_ignores_local_direct_write_setting(monkeypatch) -> None:
    """ローカル接続用の直接書き込み設定は、公開する接続には効かない。"""
    monkeypatch.setenv("SYMNOTE_MCP_PUBLIC_URL", PUBLIC_URL)
    monkeypatch.setenv("SYMNOTE_MCP_PASSPHRASE", PASSPHRASE)
    monkeypatch.setenv("SYMNOTE_MCP_DIRECT_WRITES", "1")
    monkeypatch.delenv(mcp_remote.REMOTE_DIRECT_WRITES_ENV, raising=False)
    assert mcp_remote.load_remote_settings().allow_direct_writes is False
    monkeypatch.setenv(mcp_remote.REMOTE_DIRECT_WRITES_ENV, "1")
    assert mcp_remote.load_remote_settings().allow_direct_writes is True


def test_authorization_rejects_oversized_state_and_malformed_challenge(remote) -> None:
    """巨大な state や S256 の形でない code_challenge は保留せずに拒否する。"""
    client, provider = remote
    client_id = _register(client)["client_id"]
    base = {
        "response_type": "code", "client_id": client_id, "redirect_uri": CLAUDE_CALLBACK,
        "code_challenge": _challenge(), "code_challenge_method": "S256",
        "resource": f"{PUBLIC_URL}/mcp",
    }

    for override in ({"state": "x" * 513}, {"code_challenge": "a" * 4096}):
        response = client.get("/authorize", params={**base, **override}, follow_redirects=False)
        assert "error=invalid_request" in response.headers.get("location", "")
    assert provider._pending == {}


def test_one_client_cannot_push_out_another_clients_consent(remote, monkeypatch) -> None:
    """1 つのクライアントが連投しても、別のクライアントの同意待ちは消えない。"""
    client, provider = remote
    monkeypatch.setattr(mcp_remote, "MAX_PENDING_REQUESTS", 6)
    monkeypatch.setattr(mcp_remote, "MAX_PENDING_PER_CLIENT", 4)
    owner = _register(client)["client_id"]
    attacker = _register(client)["client_id"]
    owner_request = _start_authorization(client, owner)

    for _ in range(20):
        _start_authorization(client, attacker)

    assert provider.pending_request(owner_request) is not None
    assert sum(item.client_id == attacker for item in provider._pending.values()) == 4


def test_consent_page_mentions_direct_writes_when_enabled() -> None:
    """直接書き込みを有効にしている場合は、同意画面にその権限を表示する。"""
    from types import SimpleNamespace

    params = SimpleNamespace(redirect_uri=CLAUDE_CALLBACK)
    pending = mcp_remote.PendingAuthorization(
        client_id="c", client_name="Claude", params=params, expires_at=0  # type: ignore[arg-type]
    )

    assert "直接追加・完了" in mcp_remote.render_consent_page("r", pending, allow_direct_writes=True)
    assert "直接追加・完了" not in mcp_remote.render_consent_page("r", pending)
