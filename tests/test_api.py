"""SymNote Web API（Stage 1）とマルチユーザー完全分離の統合テスト。"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from symnote.api.app import create_app
from symnote.core import candidates as candidate_store
from symnote.core.accounts import (
    authenticate_email,
    invite_user,
    revoke_invite,
    user_scope,
)

CSRF = {"X-SymNote": "1"}


def test_trash_requires_approval_scope_and_restores_once(api_env: Path) -> None:
    app = create_app()
    owner = _login_client(app, "owner@example.com", is_owner=True)
    invite_user("trash-member@example.com")
    member = _login_client(app, "trash-member@example.com")
    anonymous = TestClient(app)
    assert anonymous.get("/api/trash").status_code == 401
    task = owner.post("/api/tasks", json={"title": "復元する用事"}, headers=CSRF).json()["task"]
    from symnote.core import db
    with user_scope(authenticate_email("owner@example.com")):
        with db.connection() as conn, conn:
            conn.execute("UPDATE items SET embedding = ? WHERE id = ?", (b"\x00\xff", task["id"]))
    assert owner.delete(f"/api/tasks/{task['id']}", headers=CSRF).status_code == 200
    assert owner.get(f"/api/tasks/{task['id']}").status_code == 404
    rows = owner.get("/api/trash").json()["items"]
    assert len(rows) == 1
    assert set(rows[0]) == {"id", "kind", "title", "deleted_at"}
    trash_id = rows[0]["id"]
    assert member.get("/api/trash").json()["items"] == []
    endpoint = f"/api/trash/{trash_id}/restore"
    assert owner.post(endpoint).status_code == 403
    assert member.post(endpoint, headers=CSRF).status_code == 404
    restored = owner.post(endpoint, headers=CSRF)
    assert restored.status_code == 200, restored.text
    new_id = restored.json()["restored_id"]
    assert owner.get(f"/api/tasks/{new_id}").json()["task"]["title"] == "復元する用事"
    assert owner.post(endpoint, headers=CSRF).status_code == 404
    assert owner.get("/api/trash").json()["items"] == []


@pytest.fixture(params=["sqlite", "postgres"] if os.getenv("SYMNOTE_TEST_POSTGRES_DSN") else ["sqlite"])
def api_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "owner" / "symnote.db"))
    monkeypatch.setenv("SYMNOTE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SYMNOTE_OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("SYMNOTE_E2E", "1")
    monkeypatch.setenv("SYMNOTE_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("SYMNOTE_PUBLIC_ORIGIN", "")
    monkeypatch.setenv("SYMNOTE_TEST_MODE", "1")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("SYMNOTE_APP_PASSCODE", raising=False)
    monkeypatch.delenv("SYMNOTE_ALLOW_NO_PASSCODE", raising=False)
    monkeypatch.delenv("SYMNOTE_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("SYMNOTE_GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    if request.param == "postgres":
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        admin_dsn = os.environ["SYMNOTE_TEST_POSTGRES_DSN"]
        database = "symnote_web_test_" + uuid.uuid4().hex
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        parameters = conninfo_to_dict(admin_dsn)
        parameters["dbname"] = database
        monkeypatch.setenv("DATABASE_URL", make_conninfo(**parameters))
        monkeypatch.setenv("SYMNOTE_STORAGE_BACKEND", "postgres")
        try:
            yield tmp_path
        finally:
            with psycopg.connect(admin_dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
    else:
        yield tmp_path


def _login_client(app, email: str, *, name: str = "", is_owner: bool = False) -> TestClient:
    client = TestClient(app)
    res = client.post(
        "/api/auth/e2e-login",
        json={"email": email, "name": name, "is_owner": is_owner},
        headers=CSRF,
    )
    assert res.status_code == 200, res.text
    return client


def test_static_files_cannot_escape_dist_directory(
    api_env: Path, tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    private = tmp_path / "dist-private"
    dist.mkdir()
    private.mkdir()
    index = dist / "index.html"
    index.write_text("<h1>SymNote test frontend</h1>", encoding="utf-8")
    (dist / "app.js").write_text("window.SYMNOTE_TEST = true;", encoding="utf-8")
    secret = private / "secret.txt"
    secret.write_text("TOP-SECRET-STATIC-CONTENT", encoding="utf-8")
    (dist / "linked-secret.txt").symlink_to(secret)

    app = create_app(static_dir=dist)
    client = TestClient(
        app,
        base_url="http://testserver",
        client=("203.0.113.10", 43123),
    )

    valid = client.get("/app.js")
    assert valid.status_code == 200
    assert valid.text == "window.SYMNOTE_TEST = true;"

    traversal = client.get("/%2e%2e/dist-private/secret.txt")
    assert traversal.status_code == 404
    assert "TOP-SECRET-STATIC-CONTENT" not in traversal.text

    symlink = client.get("/linked-secret.txt")
    assert symlink.status_code == 404
    assert "TOP-SECRET-STATIC-CONTENT" not in symlink.text

    index.unlink()
    index.symlink_to(secret)
    for path in ("/", "/unknown-spa-path"):
        fallback = client.get(path)
        assert fallback.status_code == 404
        assert "TOP-SECRET-STATIC-CONTENT" not in fallback.text


def test_default_allowed_hosts_reject_host_header_injection(
    api_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SYMNOTE_ALLOWED_HOSTS", raising=False)
    app = create_app()
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 43124),
    )

    rejected = client.get("/api/tasks", headers={"Host": "attacker.example"})
    assert rejected.status_code == 400
    accepted = client.get("/api/tasks", headers={"Host": "127.0.0.1"})
    assert accepted.status_code == 401
    assert client.get("/api/health", headers={"Host": "127.0.0.1"}).status_code == 200


def test_explicit_allowed_hosts_still_accepts_testserver(api_env: Path) -> None:
    client = TestClient(create_app(), base_url="http://testserver")
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health", headers={"Host": "attacker.example"}).status_code == 400


def test_google_mode_without_passcode_does_not_grant_implicit_owner(
    api_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SYMNOTE_E2E", raising=False)
    monkeypatch.delenv("SYMNOTE_APP_PASSCODE", raising=False)
    monkeypatch.setenv("SYMNOTE_GOOGLE_CLIENT_ID", "synthetic-client")
    monkeypatch.setenv("SYMNOTE_GOOGLE_CLIENT_SECRET", "synthetic-secret")
    client = TestClient(create_app(), base_url="http://testserver")

    auth_state = client.get("/api/auth/me").json()
    assert auth_state["authenticated"] is False
    assert auth_state["mobile_enabled"] is True
    response = client.post("/api/auth/login", json={"passcode": ""}, headers=CSRF)
    assert response.status_code == 401
    assert client.get("/api/auth/me").json()["authenticated"] is False


def test_local_mode_without_credentials_keeps_loopback_auto_login(
    api_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SYMNOTE_E2E", raising=False)
    monkeypatch.delenv("SYMNOTE_APP_PASSCODE", raising=False)
    monkeypatch.delenv("SYMNOTE_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("SYMNOTE_GOOGLE_CLIENT_SECRET", raising=False)
    client = TestClient(create_app(), base_url="http://testserver")

    auth = client.get("/api/auth/me")
    assert auth.status_code == 200
    assert auth.json()["mode"] == "local"
    assert auth.json()["mobile_enabled"] is False
    from symnote.core import postgres
    if postgres.enabled():
        assert auth.json()["authenticated"] is False
        assert client.get("/api/tasks").status_code == 401
    else:
        assert auth.json()["authenticated"] is True
        assert client.get("/api/tasks").status_code == 200


def test_csrf_header_and_origin_enforcement(api_env: Path) -> None:
    app = create_app()
    client = _login_client(app, "owner@example.com", is_owner=True)

    # X-SymNote ヘッダー無しの POST は 403
    res_no_csrf = client.post("/api/tasks", json={"title": "CSRFテスト"})
    assert res_no_csrf.status_code == 403

    # 不正な Origin からの POST は 403
    res_bad_origin = client.post(
        "/api/tasks",
        json={"title": "Originテスト"},
        headers={"X-SymNote": "1", "Origin": "https://evil.example.org"},
    )
    assert res_bad_origin.status_code == 403

    # 正しいヘッダー付きなら 200
    res_ok = client.post(
        "/api/tasks",
        json={"title": "正常タスク"},
        headers={"X-SymNote": "1", "Origin": "http://testserver"},
    )
    assert res_ok.status_code == 200


def test_google_callback_requires_starting_browser_and_cannot_be_replayed(
    api_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from symnote.api import auth

    monkeypatch.setenv("SYMNOTE_GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("SYMNOTE_GOOGLE_CLIENT_SECRET", "test-secret")
    invite_user("owner@example.com", is_owner=True)
    exchanges = []

    def exchange(code, pending):
        exchanges.append(code)
        return {"email": "owner@example.com", "email_verified": True}

    monkeypatch.setattr(auth, "exchange_google_code", exchange)
    app = create_app()
    browser = TestClient(app, base_url="https://testserver")
    other_browser = TestClient(app, base_url="https://testserver")
    start = browser.get("/api/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    from symnote.core import postgres
    if postgres.enabled():
        auth._oauth_states.clear()
    cookie = start.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    callback = f"/api/auth/google/callback?state={state}&code=test-code"

    missing = other_browser.get(callback, follow_redirects=False)
    assert missing.headers["location"] == "/?auth_error=failed"
    other_browser.cookies.set(auth.OAUTH_STATE_COOKIE_NAME, "wrong-state")
    mismatched = other_browser.get(callback, follow_redirects=False)
    assert mismatched.headers["location"] == "/?auth_error=failed"
    assert exchanges == []
    assert not other_browser.get("/api/auth/me").json()["authenticated"]

    accepted = browser.get(callback, follow_redirects=False)
    assert accepted.headers["location"] == "/"
    assert browser.get("/api/auth/me").json()["authenticated"]
    assert auth.OAUTH_STATE_COOKIE_NAME not in browser.cookies
    other_browser.cookies.set(auth.OAUTH_STATE_COOKIE_NAME, state)
    replayed = other_browser.get(callback, follow_redirects=False)
    assert replayed.headers["location"] == "/?auth_error=failed"
    assert exchanges == ["test-code"]


def _begin_mobile_google_login(monkeypatch, app, *, email: str = "owner@example.com"):
    from symnote.api import auth
    from symnote.core.mobile_auth import verifier_challenge

    monkeypatch.setenv("SYMNOTE_GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("SYMNOTE_GOOGLE_CLIENT_SECRET", "test-secret")
    verifier = "v" * 64
    app_state = "native-state-0123456789"
    exchanges = []

    def exchange(code, pending):
        exchanges.append(code)
        return {"email": email, "email_verified": True}

    monkeypatch.setattr(auth, "exchange_google_code", exchange)
    browser = TestClient(app, base_url="https://testserver")
    start = browser.get(
        "/api/auth/mobile/start",
        params={"challenge": verifier_challenge(verifier), "app_state": app_state},
        follow_redirects=False,
    )
    assert start.status_code == 302
    assert start.headers["cache-control"] == "no-store"
    assert start.headers["pragma"] == "no-cache"
    assert start.headers["location"].startswith("https://accounts.google.com/")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    assert auth.OAUTH_STATE_COOKIE_NAME in browser.cookies
    return browser, state, verifier, app_state, exchanges


def _complete_mobile_google_login(browser, state: str, *, google_code: str = "google-code"):
    callback = browser.get(
        f"/api/auth/google/callback?state={state}&code={google_code}",
        follow_redirects=False,
    )
    assert callback.status_code == 302
    assert callback.headers["cache-control"] == "no-store"
    parsed = urlparse(callback.headers["location"])
    assert parsed.scheme == "app.symnote.mobile"
    assert parsed.netloc == "auth"
    assert parsed.path == "/callback"
    return callback, parse_qs(parsed.query)


def test_mobile_google_login_pkce_replay_bearer_and_logout(
    api_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    invite_user("owner@example.com", is_owner=True)
    app = create_app()
    browser, state, verifier, app_state, exchanges = _begin_mobile_google_login(monkeypatch, app)

    other_browser = TestClient(app, base_url="https://testserver")
    wrong_browser = other_browser.get(
        f"/api/auth/google/callback?state={state}&code=attacker-code",
        follow_redirects=False,
    )
    assert wrong_browser.headers["location"] == "/?auth_error=failed"
    assert exchanges == []

    callback, callback_params = _complete_mobile_google_login(browser, state)
    assert set(callback_params) == {"code", "app_state"}
    assert callback_params["app_state"] == [app_state]
    assert "session" not in callback.headers["location"]
    assert "set-cookie" in callback.headers
    assert "symnote_session" not in callback.headers["set-cookie"]
    assert exchanges == ["google-code"]
    code = callback_params["code"][0]

    native = TestClient(app, base_url="https://testserver")
    blocked_browser = native.post(
        "/api/auth/mobile/exchange",
        json={"code": code, "verifier": verifier},
        headers={"X-SymNote": "1", "Origin": "https://testserver"},
    )
    assert blocked_browser.status_code == 403

    wrong_verifier = native.post(
        "/api/auth/mobile/exchange",
        json={"code": code, "verifier": "w" * 64},
        headers=CSRF,
    )
    assert wrong_verifier.status_code == 401
    assert wrong_verifier.json()["error"]["code"] == "invalid_exchange"

    exchanged = native.post(
        "/api/auth/mobile/exchange",
        json={"code": code, "verifier": verifier},
        headers=CSRF,
    )
    assert exchanged.status_code == 200
    assert exchanged.headers["cache-control"] == "no-store"
    assert "set-cookie" not in exchanged.headers
    payload = exchanged.json()
    assert set(payload) == {"session_token", "token_type", "expires_in"}
    assert payload["token_type"] == "Bearer"
    token = payload["session_token"]

    replay = native.post(
        "/api/auth/mobile/exchange",
        json={"code": code, "verifier": verifier},
        headers=CSRF,
    )
    assert replay.status_code == 401

    bearer = {"Authorization": f"Bearer {token}", "X-SymNote": "1"}
    assert native.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()[
        "authenticated"
    ] is False
    assert native.get("/api/auth/me", headers=bearer).json()["authenticated"] is True
    assert native.get("/api/tasks", headers=bearer).status_code == 200
    assert native.post("/api/auth/logout", headers=bearer).json() == {"ok": True}
    assert native.get("/api/auth/me", headers=bearer).json()["authenticated"] is False


def test_mobile_exchange_code_expires_without_issuing_session(
    api_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    from symnote.core.mobile_auth import MOBILE_CODE_TTL_SECONDS, exchange_mobile_code

    invite_user("owner@example.com", is_owner=True)
    app = create_app()
    browser, state, verifier, _app_state, _exchanges = _begin_mobile_google_login(monkeypatch, app)
    _callback, callback_params = _complete_mobile_google_login(browser, state)
    code = callback_params["code"][0]
    assert exchange_mobile_code(
        code,
        verifier,
        now=time.time() + MOBILE_CODE_TTL_SECONDS + 1,
    ) is None
    response = TestClient(app, base_url="https://testserver").post(
        "/api/auth/mobile/exchange",
        json={"code": code, "verifier": verifier},
        headers=CSRF,
    )
    assert response.status_code == 401
    assert "session_token" not in response.text


def test_mobile_exchange_rechecks_invite_and_consumes_rejected_code(
    api_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    invite_user("member@example.com")
    app = create_app()
    browser, state, verifier, _app_state, _exchanges = _begin_mobile_google_login(
        monkeypatch, app, email="member@example.com"
    )
    _callback, callback_params = _complete_mobile_google_login(browser, state)
    code = callback_params["code"][0]
    assert revoke_invite("member@example.com") is True

    native = TestClient(app, base_url="https://testserver")
    rejected = native.post(
        "/api/auth/mobile/exchange",
        json={"code": code, "verifier": verifier},
        headers=CSRF,
    )
    assert rejected.status_code == 401
    invite_user("member@example.com")
    replay = native.post(
        "/api/auth/mobile/exchange",
        json={"code": code, "verifier": verifier},
        headers=CSRF,
    )
    assert replay.status_code == 401


@pytest.mark.parametrize(
    ("challenge", "app_state"),
    [
        ("short", "native-state-0123456789"),
        ("!" * 43, "native-state-0123456789"),
        ("a" * 43, "short"),
        ("a" * 43, "invalid+state-value"),
    ],
)
def test_mobile_start_rejects_invalid_base64url_inputs(
    api_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    challenge: str,
    app_state: str,
) -> None:
    monkeypatch.setenv("SYMNOTE_GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("SYMNOTE_GOOGLE_CLIENT_SECRET", "test-secret")
    response = TestClient(create_app(), base_url="https://testserver").get(
        "/api/auth/mobile/start",
        params={"challenge": challenge, "app_state": app_state},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_mobile_request"
    assert "accounts.google.com" not in response.text


def test_mobile_google_cancel_uses_fixed_callback_and_retains_mobile_marker(
    api_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from symnote.core.mobile_auth import get_mobile_flow

    invite_user("owner@example.com", is_owner=True)
    app = create_app()
    browser, state, _verifier, app_state, exchanges = _begin_mobile_google_login(monkeypatch, app)
    cancelled = browser.get(
        f"/api/auth/google/callback?state={state}&error=access_denied",
        follow_redirects=False,
    )
    parsed = urlparse(cancelled.headers["location"])
    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        "app.symnote.mobile",
        "auth",
        "/callback",
    )
    assert parse_qs(parsed.query) == {"error": ["cancelled"], "app_state": [app_state]}
    assert exchanges == []
    assert get_mobile_flow(state) is not None
    replay = browser.get(
        f"/api/auth/google/callback?state={state}&error=access_denied",
        follow_redirects=False,
    )
    assert replay.headers["location"] == "/?auth_error=cancelled"


def test_task_lifecycle_complete_reopen_and_today_view(api_env: Path) -> None:
    app = create_app()
    client = _login_client(app, "owner@example.com", is_owner=True)

    # 1. クイックプレビュー & クイック追加
    prev = client.post(
        "/api/capture/preview",
        json={"text": "明日 14時 企画書をレビューする"},
        headers=CSRF,
    ).json()
    assert prev["kind"] == "task"
    assert prev["clean_title"] == "企画書をレビューする"
    assert prev["due_time"] == "14:00"
    assert {chip["kind"]: chip["label"] for chip in prev["chips"]} == {
        "kind": "タスク", "date": prev["due_date"], "time": "14:00",
    }

    cap = client.post(
        "/api/capture",
        json={"text": "今日 16:00 請求書を送る", "status": "today"},
        headers=CSRF,
    ).json()
    task_id = cap["task"]["id"]
    assert cap["task"]["title"] == "請求書を送る"

    # 2. 今日画面の取得
    today_res = client.get("/api/today").json()
    assert any(t["id"] == task_id for t in today_res["due_today"])
    assert today_res["next_action"]["task_id"] == task_id
    assert today_res["next_action"]["title"] == "請求書を送る"
    assert today_res["next_action"]["due_time"] == "16:00"
    assert today_res["next_action"]["due_date"] == cap["task"]["due_date"]
    assert today_res["next_action"]["estimate_minutes"] is None

    # 3. 完了 → 今日完了セクションへ移動
    comp = client.post(f"/api/tasks/{task_id}/complete", headers=CSRF).json()
    assert comp["task"]["status"] == "done"
    assert comp["task"]["completed_at"] is not None

    today_after = client.get("/api/today").json()
    assert any(t["id"] == task_id for t in today_after["completed_today"])

    # 4. Undo（reopen）で元に戻せる
    reopen = client.post(
        f"/api/tasks/{task_id}/reopen",
        json={"status": "today"},
        headers=CSRF,
    ).json()
    assert reopen["task"]["status"] == "today"
    assert reopen["task"]["completed_at"] is None


@pytest.mark.parametrize("text, expected_kind, recurrence_label", [
    ("毎週月曜 9:00 ゴミ出し", "task", "毎週月曜"),
    ("メモ: 新しいアイデア", "memo", None),
])
def test_capture_preview_chips_support_recurrence_and_memos(
    api_env: Path, text: str, expected_kind: str, recurrence_label: str | None,
) -> None:
    client = _login_client(create_app(), "owner@example.com", is_owner=True)
    response = client.post("/api/capture/preview", json={"text": text}, headers=CSRF)
    assert response.status_code == 200
    preview = response.json()
    assert preview["kind"] == expected_kind
    chips = {chip["kind"]: chip["label"] for chip in preview["chips"]}
    assert chips["kind"] == ("タスク" if expected_kind == "task" else "メモ")
    assert chips.get("recurrence") == recurrence_label


@pytest.mark.parametrize("endpoint, payload", [
    ("/api/capture", {"text": "毎週月曜 9:00 ゴミ出し", "status": "today"}),
    ("/api/tasks", {"title": "ゴミ出し", "due_date": "2026-10-19", "due_time": "09:00",
                    "recurrence": {"frequency": "weekly", "interval": 2, "weekdays": [0]}}),
])
def test_recurring_task_creation_and_completion_preserve_schedule(
    api_env: Path, endpoint: str, payload: dict,
) -> None:
    import datetime as dt

    client = _login_client(create_app(), "owner@example.com", is_owner=True)
    response = client.post(endpoint, json=payload, headers=CSRF)
    assert response.status_code == 200
    task = response.json()["task"]
    rule = task["recurrence"]
    assert rule["frequency"] == "weekly"
    assert rule["weekdays"] == [0]
    assert rule["interval"] == (1 if endpoint == "/api/capture" else 2)
    assert rule["label"]
    completed = client.post(f"/api/tasks/{task['id']}/complete", headers=CSRF)
    assert completed.status_code == 200
    next_task = completed.json()["next_task"]
    expected_date = dt.date.fromisoformat(task["due_date"]) + dt.timedelta(weeks=rule["interval"])
    assert next_task["due_date"] == expected_date.isoformat()
    assert next_task["due_time"] == "09:00"
    assert next_task["recurrence"] == rule


def test_brain_dump_split_and_commit(api_env: Path) -> None:
    app = create_app()
    client = _login_client(app, "owner@example.com", is_owner=True)

    split_res = client.post(
        "/api/capture/split",
        json={"text": "・明日 10時 歯医者に電話する\n・金曜 17時 経費精算を出す\n・牛乳を買う"},
        headers=CSRF,
    )
    assert split_res.status_code == 200
    items = split_res.json()["items"]
    assert len(items) == 3
    assert items[0]["title"] == "歯医者に電話する"
    assert items[0]["due_time"] == "10:00"

    commit_res = client.post(
        "/api/capture/commit-split",
        json={
            "items": items,
            "source_text": "頭の中メモ原文",
            "save_source_memo": True,
        },
        headers=CSRF,
    )
    assert commit_res.status_code == 200
    data = commit_res.json()
    assert len(data["tasks"]) == 3
    assert data["memo_id"] is not None


def test_inbox_candidates_and_unsorted_flow(api_env: Path) -> None:
    app = create_app()
    client = _login_client(app, "owner@example.com", is_owner=True)
    owner = authenticate_email("owner@example.com")

    with user_scope(owner):
        cand = candidate_store.validate_candidate(
            source="gmail",
            source_ref="msg-101",
            title="契約書ドラフトを返信する",
            excerpt="金曜日までにご確認をお願いします",
            confidence=0.9,
        )
        res = candidate_store.propose_candidates([cand])
        cand_id = res.created_ids[0]

    # 未仕分けも1件追加
    cap_unsorted = client.post(
        "/api/capture",
        json={"text": "あとで整理したいアイデアメモ", "kind": "unsorted"},
        headers=CSRF,
    ).json()
    unsorted_id = cap_unsorted["unsorted"]["id"]

    inbox = client.get("/api/inbox").json()
    assert inbox["badge_count"] == 2
    assert len(inbox["pending_candidates"]) == 1
    assert len(inbox["unsorted_items"]) == 1

    # 候補を一度却下 → 復元 → 承認
    rej = client.post(f"/api/candidates/{cand_id}/reject", headers=CSRF)
    assert rej.status_code == 200
    # 二重却下は 409 Conflict
    rej_again = client.post(f"/api/candidates/{cand_id}/reject", headers=CSRF)
    assert rej_again.status_code == 409

    rest = client.post(f"/api/candidates/{cand_id}/restore", headers=CSRF)
    assert rest.status_code == 200
    assert rest.json()["candidate"]["status"] == "pending"

    app_res = client.post(
        f"/api/candidates/{cand_id}/approve",
        json={"title": "契約書ドラフトを確認して返信する", "status": "today"},
        headers=CSRF,
    )
    assert app_res.status_code == 200
    created_task = app_res.json()["task"]
    assert created_task["title"] == "契約書ドラフトを確認して返信する"
    assert created_task["source"]["source"] == "gmail"

    # 未仕分けをタスクに仕分け
    sort_res = client.post(
        f"/api/unsorted/{unsorted_id}/sort",
        json={"as_kind": "task", "title": "アイデアを企画書にまとめる", "status": "today"},
        headers=CSRF,
    )
    assert sort_res.status_code == 200
    assert sort_res.json()["task"]["title"] == "アイデアを企画書にまとめる"


def test_multiuser_api_isolation_between_owner_and_invited_member(api_env: Path) -> None:
    app = create_app()
    owner_client = _login_client(app, "owner@example.com", name="Owner", is_owner=True)

    # 未招待ユーザーは E2E ログインでも 403
    uninvited_client = TestClient(app)
    denied = uninvited_client.post(
        "/api/auth/e2e-login",
        json={"email": "uninvited@example.com"},
        headers=CSRF,
    )
    assert denied.status_code == 403

    # オーナーが設定APIから友人Bを招待
    inv_res = owner_client.post(
        "/api/settings/invites",
        json={"email": "member_b@example.com", "note": "友人B"},
        headers=CSRF,
    )
    assert inv_res.status_code == 200

    member_client = _login_client(app, "member_b@example.com", name="Member B")

    # オーナーがタスクを作成
    owner_task = owner_client.post(
        "/api/tasks",
        json={"title": "オーナー専用タスク", "status": "today"},
        headers=CSRF,
    ).json()["task"]

    # 友人Bからはオーナーのタスクが一切見えない・操作できない
    member_tasks = member_client.get("/api/tasks").json()["tasks"]
    assert member_tasks == []
    assert member_client.get(f"/api/tasks/{owner_task['id']}").status_code == 404
    assert (
        member_client.patch(
            f"/api/tasks/{owner_task['id']}",
            json={"title": "書き換え試行"},
            headers=CSRF,
        ).status_code
        == 404
    )
    # 友人Bは招待APIを叩けない（403）
    assert (
        member_client.post(
            "/api/settings/invites",
            json={"email": "other@example.com"},
            headers=CSRF,
        ).status_code
        == 403
    )


@pytest.mark.parametrize("memo_date", [None, "2026-10-20"])
def test_memo_creation_preserves_optional_date_and_is_listed(api_env: Path, memo_date: str | None) -> None:
    """日付あり・なしのメモを保存し、一覧から同じ内容を読み出せる。"""
    client = _login_client(create_app(), "owner@example.com", is_owner=True)
    response = client.post(
        "/api/memos", json={"text": "合成例のメモ", "tags": "検証", "date": memo_date}, headers=CSRF,
    )
    assert response.status_code == 200
    memo = response.json()["memo"]
    assert memo["text"] == "合成例のメモ"
    listed = client.get("/api/memos").json()["memos"]
    assert any(row["id"] == memo["id"] and row["text"] == "合成例のメモ" for row in listed)
    from symnote.core import db
    user = authenticate_email("owner@example.com")
    with user_scope(user):
        saved = db.fetch_item(memo["id"])
        assert saved["date"] == (memo_date or __import__("datetime").date.today().isoformat())


def test_goal_web_flow_requires_confirmation_and_updates_ancestor_progress(api_env, monkeypatch):
    from symnote.core import goals

    client = _login_client(create_app(), "owner@example.com", is_owner=True)
    root = client.post('/api/goals', json={
        'title': '公開', 'why': '使ってもらう', 'success_criteria': '公開できた',
    }, headers=CSRF).json()['goal']['id']
    child = client.post('/api/goals', json={
        'title': '準備', 'parent_id': root,
    }, headers=CSRF).json()['goal']['id']
    edited = client.patch(f'/api/goals/{child}', json={
        'why': '品質を確かめる', 'success_criteria': '検証済み', 'target_date': '2026-12-31',
    }, headers=CSRF)
    assert edited.status_code == 200
    detail = client.get(f'/api/goals/{child}').json()['goal']
    assert detail['success_criteria'] == '検証済み'
    assert detail['ancestors'][0]['id'] == root
    assert detail['target_date'] == '2026-12-31'
    monkeypatch.setattr(goals, 'propose_goal_breakdown', lambda _id: goals.GoalBreakdown(
        [goals.ProposedGoal('子の案'), goals.ProposedGoal('未選択の子')],
        [goals.ProposedTask('選ぶ', '理由', '', 15), goals.ProposedTask('選ばない')],
    ))
    draft = client.post(f'/api/goals/{child}/breakdown', headers=CSRF).json()
    assert draft['ai_used']
    assert client.get(f'/api/goals/{child}').json()['goal']['open_tasks'] == []
    assert len(client.get('/api/goals').json()['goals']) == 2
    draft['tasks'][0]['title'] = '編集した一歩'
    saved = client.post(f'/api/goals/{child}/breakdown/commit', json={
        'subgoals': draft['subgoals'], 'tasks': draft['tasks'],
        'selected_subgoals': [0], 'selected_tasks': [0],
    }, headers=CSRF)
    assert saved.status_code == 200, saved.text
    task_id = saved.json()['task_ids'][0]
    detail = client.get(f'/api/goals/{child}').json()['goal']
    assert [t['title'] for t in detail['open_tasks']] == ['編集した一歩']
    assert [g['title'] for g in detail['subgoals']] == ['子の案']
    original = client.get(f'/api/tasks/{task_id}').json()['task']
    assert client.patch(f'/api/tasks/{task_id}', json={'status': 'today'}, headers=CSRF).status_code == 200
    updated = client.get(f'/api/tasks/{task_id}').json()['task']
    assert updated['due_date'] == original['due_date']
    assert len(client.get('/api/tasks?status=all').json()['tasks']) == 1
    assert client.post(f'/api/tasks/{task_id}/complete', headers=CSRF).status_code == 200
    root_summary = next(g for g in client.get('/api/goals').json()['goals'] if g['id'] == root)
    assert root_summary['done_tasks'] == 1
    assert root_summary['progress_percent'] == 100
    assert root_summary['next_task'] is None


@pytest.mark.parametrize('failure', ['invalid_selection', 'mid_transaction'])
def test_goal_commit_is_atomic(api_env, monkeypatch, failure):
    from symnote.core import db

    client = _login_client(create_app(), 'owner@example.com', is_owner=True)
    gid = client.post('/api/goals', json={'title': '目標'}, headers=CSRF).json()['goal']['id']
    payload = {'subgoals': [{'title': '子'}], 'tasks': [{'title': '一歩'}],
               'selected_subgoals': [0], 'selected_tasks': [0]}
    if failure == 'invalid_selection':
        payload['selected_tasks'] = [1]
    else:
        def fail(*args, **kwargs):
            raise ValueError('保存失敗')
        monkeypatch.setattr(db, 'insert_item_in_transaction', fail)
    response = client.post(f'/api/goals/{gid}/breakdown/commit', json=payload, headers=CSRF)
    assert response.status_code == 400
    detail = client.get(f'/api/goals/{gid}').json()['goal']
    assert detail['subgoals'] == detail['open_tasks'] == []


def test_goal_endpoints_preserve_user_isolation_auth_and_fallback(api_env, monkeypatch):
    from symnote.core import goals

    app = create_app()
    owner = _login_client(app, 'owner@example.com', is_owner=True)
    gid = owner.post('/api/goals', json={'title': '秘密'}, headers=CSRF).json()['goal']['id']
    invite_user('guest@example.com')
    guest = _login_client(app, 'guest@example.com')
    assert guest.get(f'/api/goals/{gid}').status_code == 404
    assert guest.post(f'/api/goals/{gid}/breakdown', headers=CSRF).status_code == 404
    payload = {'tasks': [{'title': '勝手に追加'}], 'selected_tasks': [0]}
    assert guest.post(f'/api/goals/{gid}/breakdown/commit', json=payload, headers=CSRF).status_code == 404
    assert TestClient(app).get(f'/api/goals/{gid}').status_code == 401
    assert owner.post(f'/api/goals/{gid}/breakdown/commit', json=payload).status_code == 403
    def unavailable(_id):
        raise RuntimeError('offline')
    monkeypatch.setattr(goals, 'propose_goal_breakdown', unavailable)
    fallback = owner.post(f'/api/goals/{gid}/breakdown', headers=CSRF).json()
    assert fallback['ai_used'] is False
    assert fallback['tasks']
    assert owner.get(f'/api/goals/{gid}').json()['goal']['open_tasks'] == []
    assert owner.patch(f'/api/goals/{gid}', json={'why': '手動編集'}, headers=CSRF).status_code == 200


@pytest.mark.parametrize('indices', [[True], [0, 0], [-1]])
def test_goal_commit_rejects_invalid_selection_without_writes(api_env, indices):
    client = _login_client(create_app(), 'owner@example.com', is_owner=True)
    gid = client.post('/api/goals', json={'title': '確認'}, headers=CSRF).json()['goal']['id']
    response = client.post(f'/api/goals/{gid}/breakdown/commit', json={
        'tasks': [{'title': '一歩'}, {'title': '二歩'}], 'selected_tasks': indices,
    }, headers=CSRF)
    assert response.status_code == 400
    assert client.get(f'/api/goals/{gid}').json()['goal']['open_tasks'] == []


def test_onboarding_saves_profile_and_allows_goal_workflow(api_env):
    client = _login_client(create_app(), 'owner@example.com', is_owner=True)
    response = client.post('/api/auth/onboarding', json={
        'name': '検証ユーザー', 'seed_sample_tasks': False,
    }, headers=CSRF)
    assert response.status_code == 200
    user = client.get('/api/auth/me').json()['user']
    assert user['onboarded'] is True
    assert user['name'] == '検証ユーザー'
    assert client.get('/api/tasks').json()['tasks'] == []
    assert client.post('/api/goals', json={'title': '最初の目標'}, headers=CSRF).status_code == 200


def test_ai_limit_is_atomic_across_connections(api_env: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from symnote.core import usage_limit
    from symnote.core.accounts import system_db_path

    invite_user("owner@example.com", is_owner=True)
    target = system_db_path()
    limits = usage_limit.UsageLimits(per_minute=2, daily_per_user=10, daily_global=10)

    def attempt(_):
        with usage_limit.use_usage_scope("concurrent-user", system_db_path=target):
            try:
                usage_limit.acquire(now=1792500000.0, limits=limits)
                return True
            except usage_limit.UsageLimitExceeded:
                return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        accepted = list(pool.map(attempt, range(8)))
    assert sum(accepted) == 2


def test_postgres_import_export_preserves_data_and_refuses_overwrite(api_env: Path, monkeypatch) -> None:
    import hashlib
    import sqlite3
    from symnote.config import use_db_path
    from symnote.core import db, postgres
    from symnote.postgres_cli import export_sqlite, import_sqlite

    if not postgres.enabled():
        pytest.skip("PostgreSQL migration requires the real PostgreSQL test backend")
    source = api_env / "local-source.db"
    with monkeypatch.context() as local:
        local.setenv("SYMNOTE_STORAGE_BACKEND", "sqlite")
        with use_db_path(str(source)):
            db.init_db()
            item_id = db.insert_item("memo", "合成データ：日本語と NULL とバイナリ")
            with db.connection() as conn, conn:
                conn.execute("UPDATE items SET embedding = ? WHERE id = ?", (b"\x00\x01\xff", item_id))
                conn.execute("CREATE TABLE gmail_import_processed(source_ref TEXT PRIMARY KEY, processed_at TEXT NOT NULL)")
                conn.execute("INSERT INTO gmail_import_processed VALUES ('synthetic-ref', '2026-10-21')")
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    result = import_sqlite(source, "owner")
    assert result["tables"]["items"] == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    with postgres.user_schema("owner"):
        with db.connection() as conn:
            assert conn.execute("SELECT embedding FROM items WHERE id = ?", (item_id,)).fetchone()[0] == b"\x00\x01\xff"
        assert db.insert_item("memo", "移行後の新規データ") > item_id
    with pytest.raises(ValueError, match="must be empty"):
        import_sqlite(source, "owner")
    output = api_env / "export.db"
    exported = export_sqlite(output, "owner")
    assert exported["tables"]["items"] == 2
    assert exported["tables"]["gmail_import_processed"] == 1
    with sqlite3.connect(output) as conn:
        row = conn.execute("SELECT raw_text, embedding FROM items WHERE id = ?", (item_id,)).fetchone()
        assert row == ("合成データ：日本語と NULL とバイナリ", b"\x00\x01\xff")
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    with pytest.raises(FileExistsError):
        export_sqlite(output, "owner")


def test_oauth_start_hides_internal_errors(api_env, monkeypatch):
    def fail(_request):
        raise RuntimeError("private-db-host password=private-value")
    monkeypatch.setattr("symnote.api.app.build_google_authorization_url", fail)
    response = TestClient(create_app()).get("/api/auth/google/start")
    assert response.status_code == 503
    assert "private" not in response.text
    assert "oauth_unavailable" in response.text


def test_postgres_health_fails_closed(api_env, monkeypatch):
    monkeypatch.setenv("SYMNOTE_STORAGE_BACKEND", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = TestClient(create_app()).get("/api/health")
    assert response.status_code == 503
    assert response.json() == {"ok": False, "storage": "unavailable"}


@pytest.mark.parametrize("e2e", [False, True])
def test_postgres_server_requires_google_and_disables_e2e(monkeypatch, e2e):
    from symnote.web_server import main
    monkeypatch.setenv("SYMNOTE_STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("SYMNOTE_E2E", "1" if e2e else "0")
    monkeypatch.setenv("SYMNOTE_GOOGLE_CLIENT_ID", "synthetic-client" if e2e else "")
    monkeypatch.setenv("SYMNOTE_GOOGLE_CLIENT_SECRET", "synthetic-secret" if e2e else "")
    with pytest.raises(SystemExit) as exc:
        main(["--host", "127.0.0.1"])
    assert exc.value.code == 2


def test_postgres_rejects_inherited_owner_passcode(api_env, monkeypatch):
    from symnote.core import postgres
    if not postgres.enabled():
        pytest.skip("PostgreSQL-only authentication policy")
    monkeypatch.setenv("SYMNOTE_APP_PASSCODE", "synthetic-local-passcode")
    client = TestClient(create_app())
    assert client.get("/api/auth/me").json()["passcode_enabled"] is False
    response = client.post("/api/auth/login", json={"passcode": "synthetic-local-passcode"}, headers=CSRF)
    assert response.status_code == 401


@pytest.mark.parametrize("finish_via_focus", [False, True])
def test_game_loop_rewards_focus_shop_and_buddy(api_env: Path, finish_via_focus: bool) -> None:
    client = _login_client(create_app(), "owner@example.com", is_owner=True)
    initial = client.get("/api/player").json()
    assert initial["buddy"]["name"] == "ミナ"
    patched = client.patch("/api/settings/buddy", json={"name": "ソラ", "user_name": "そら", "sound_enabled": False}, headers=CSRF)
    assert patched.status_code == 200
    assert client.get("/api/player").json()["buddy"]["name"] == "ソラ"
    task = client.post("/api/tasks", json={"title": "一歩進む", "estimate_minutes": 15}, headers=CSRF).json()["task"]
    assert task["quest"]["stars"] == 1
    assert task["quest"]["xp"] > 0
    today = client.get("/api/today").json()
    assert today["daily_clear"]["target"] == 1
    assert today["capacity"]["planned_minutes"] == 15
    session = client.post("/api/focus", json={"task_id": task["id"], "minutes": 15}, headers=CSRF).json()["session"]
    stopped = client.post(f"/api/focus/{session['id']}/stop", headers=CSRF)
    assert stopped.status_code == 200
    assert client.get("/api/focus").json()["session"] is None
    if finish_via_focus:
        session = client.post("/api/focus", json={"task_id": task["id"], "minutes": 15}, headers=CSRF).json()["session"]
        complete = client.post(f"/api/focus/{session['id']}/finish", json={"complete_task": True}, headers=CSRF).json()
    else:
        complete = client.post(f"/api/tasks/{task['id']}/complete", headers=CSRF).json()
    assert complete["reward"]["xp"] == task["quest"]["xp"]
    assert complete["daily_clear"]["xp"] == 20
    assert complete["reward"]["new_achievements"][0]["code"] == "first_quest"
    xp = complete["player"]["xp_total"]
    assert client.post(f"/api/tasks/{task['id']}/reopen", json={}, headers=CSRF).status_code == 200
    again = client.post(f"/api/tasks/{task['id']}/complete", headers=CSRF).json()
    assert again["reward"] is None and again["daily_clear"] is None
    assert again["player"]["xp_total"] == xp
    reward_id = client.post("/api/rewards", json={"title": "お茶", "cost": 1}, headers=CSRF).json()["id"]
    gold = again["player"]["gold"]
    assert client.post(f"/api/rewards/{reward_id}/redeem", headers=CSRF).json()["gold"] == gold - 1
    expensive = client.post("/api/rewards", json={"title": "旅", "cost": 100000}, headers=CSRF).json()["id"]
    no_gold = client.post(f"/api/rewards/{expensive}/redeem", headers=CSRF)
    assert no_gold.status_code == 409 and no_gold.json()["code"] == "insufficient_gold"
    assert client.delete(f"/api/rewards/{reward_id}", headers=CSRF).status_code == 200
    assert len(client.get("/api/quests/history?days=28").json()["days"]) == 28
    assert client.get("/api/review/weekly").status_code == 200


def test_seed_preview_adoption_reports_and_game_user_boundaries(api_env: Path) -> None:
    from symnote.core import db, delegation
    owner = _login_client(create_app(), "owner@example.com", is_owner=True)
    invite_user("game-member@example.com")
    member = _login_client(create_app(), "game-member@example.com")
    seed = owner.post("/api/seeds", json={"text": "小さなアプリを作る"}, headers=CSRF).json()["seed"]
    seed_id = seed["id"]
    preview = owner.post(f"/api/seeds/{seed_id}/plan", json={"why": "毎日を助ける", "done_when": "一人が使える"}, headers=CSRF)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["ai_used"] is False and len(plan["stages"]) == 3
    assert owner.get("/api/maps").json()["maps"] == []
    assert member.get("/api/seeds").json()["seeds"] == []
    assert member.post(f"/api/seeds/{seed_id}/plan", json={}, headers=CSRF).status_code == 404
    adopted = owner.post(f"/api/seeds/{seed_id}/adopt", json={"plan": plan, "today_first": True}, headers=CSRF)
    assert adopted.status_code == 200, adopted.text
    result = adopted.json()
    assert owner.post(f"/api/seeds/{seed_id}/adopt", json={"plan": plan, "today_first": True}, headers=CSRF).json() == result
    assert owner.get("/api/seeds").json()["seeds"] == []
    view = owner.get(f"/api/maps/{result['goal_id']}").json()
    assert view["current_task_id"] == result["task_ids"][0]
    assert member.get(f"/api/maps/{result['goal_id']}").status_code in (400, 404)
    with user_scope(authenticate_email("owner@example.com")):
        report_id = delegation.record_progress(result["task_ids"][0], None, "一歩進みました")
    assert len(owner.get("/api/reports").json()["reports"]) == 1
    assert member.post(f"/api/reports/{report_id}/approve", headers=CSRF).status_code == 404
    decided = owner.post(f"/api/reports/{report_id}/approve", headers=CSRF)
    assert decided.status_code == 200 and decided.json()["triage_bonus"]["xp"] == 2
    assert owner.post(f"/api/reports/{report_id}/approve", headers=CSRF).status_code == 409
    assert member.get("/api/player").json()["xp_total"] == 0
    assert owner.get("/api/player").json()["xp_total"] == 2
    assert owner.patch("/api/settings/buddy", json={"name": "test"}).status_code == 403
    assert TestClient(create_app()).get("/api/player").status_code == 401


def test_today_capacity_is_unknown_without_calendar_and_merges_busy_intervals(api_env, monkeypatch):
    import datetime as dt
    from symnote.api import game
    from symnote.core import calendar_events, google_calendar, runtime
    client = _login_client(create_app(), "owner@example.com", is_owner=True)
    assert client.get("/api/today").json()["capacity"]["free_minutes"] is None
    monkeypatch.setattr(runtime, "is_test_mode", lambda: False)
    monkeypatch.setattr(google_calendar, "is_connected", lambda: True)
    day = dt.date.today().isoformat()
    events = [
        {"id": "a", "summary": "会議A", "start": {"dateTime": f"{day}T10:00:00+09:00"}, "end": {"dateTime": f"{day}T11:00:00+09:00"}},
        {"id": "b", "summary": "会議B", "start": {"dateTime": f"{day}T10:30:00+09:00"}, "end": {"dateTime": f"{day}T12:00:00+09:00"}},
    ]
    monkeypatch.setattr(calendar_events, "fetch_events", lambda *args, **kwargs: calendar_events.EventFetchResult(events, fetched_at=day))
    with user_scope(authenticate_email("owner@example.com")):
        agenda, free = game.calendar_payload(authenticate_email("owner@example.com"), dt.date.today())
    assert len(agenda) == 2
    assert free == 22 * 60


def test_capture_classifies_mixed_items_and_commits_atomically(api_env):
    client = _login_client(create_app(), "owner@example.com", is_owner=True)
    preview = client.post("/api/capture/split", json={"text": "資料を送る\nメモ：好きな本\nたね：新しいアプリを作りたい"}, headers=CSRF).json()
    assert [item["kind"] for item in preview["items"]] == ["task", "memo", "seed"]
    bad_items = [*preview["items"], {"title": "不正な日付", "due_date": "bad-date"}]
    bad = client.post("/api/capture/commit-split", json={"items": bad_items, "source_text": "原文", "save_source_memo": True}, headers=CSRF)
    assert bad.status_code == 400
    assert client.get("/api/tasks?status=all").json()["tasks"] == []
    assert client.get("/api/memos").json()["memos"] == []
    committed = client.post("/api/capture/commit-split", json={"items": preview["items"]}, headers=CSRF)
    assert committed.status_code == 200, committed.text
    result = committed.json()
    assert [len(result[key]) for key in ("tasks", "memos", "seeds")] == [1, 1, 1]
    assert len(client.get("/api/seeds").json()["seeds"]) == 1
    goal_id = client.post("/api/goals", json={"title": "既存の冒険"}, headers=CSRF).json()["goal"]["id"]
    hidden = client.post("/api/capture/commit-split", json={"items": [{"kind": "seed", "title": "見えなくなるたね", "goal_id": goal_id}]}, headers=CSRF)
    assert hidden.status_code == 400
    assert len(client.get("/api/seeds").json()["seeds"]) == 1


def test_quest_detail_recurrence_edits_and_rollback(api_env):
    client = _login_client(create_app(), "owner@example.com", is_owner=True)
    task = client.post("/api/tasks", json={"title": "習慣", "due_date": "2026-10-23"}, headers=CSRF).json()["task"]
    endpoint = f"/api/tasks/{task['id']}"
    added = client.patch(endpoint, json={"importance": 5, "effort": "long", "energy": "high", "recurrence": {"frequency": "weekly", "weekdays": [4]}}, headers=CSRF)
    assert added.status_code == 200, added.text
    edited = added.json()["task"]
    assert edited["recurrence"]["frequency"] == "weekly"
    assert edited["importance"] == 5 and edited["effort"] == "long" and edited["energy"] == "high"
    failed = client.patch(endpoint, json={"title": "保存されてはいけない", "recurrence": {"frequency": "daily", "end_date": "2020-01-01"}}, headers=CSRF)
    assert failed.status_code == 400
    assert client.get(endpoint).json()["task"]["title"] == "習慣"
    assert client.get(endpoint).json()["task"]["recurrence"]["frequency"] == "weekly"
    stopped = client.patch(endpoint, json={"stop_recurrence": True}, headers=CSRF)
    assert stopped.status_code == 200
    assert "停止" in stopped.json()["task"]["recurrence"]["label"]
