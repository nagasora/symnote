"""招待取消後の既存Cookie・Google再認証を一時DBで検証する。"""

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.testclient import TestClient

from symnote.api import auth
from symnote.api.app import create_app
from symnote.core.accounts import invite_user, revoke_invite


@pytest.mark.parametrize("invited_owner", [False, True])
def test_revoked_invite_denies_existing_cookies_and_google_relogin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invited_owner: bool,
) -> None:
    """取消後は両Cookieを拒否し、再招待後も元データとユーザーを維持する。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "owner.db"))
    monkeypatch.setenv("SYMNOTE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("SYMNOTE_SYSTEM_DB_PATH", raising=False)
    monkeypatch.setenv("SYMNOTE_OWNER_EMAIL", "owner@example.test")
    monkeypatch.setenv("SYMNOTE_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("SYMNOTE_PUBLIC_ORIGIN", "")
    monkeypatch.setenv("SYMNOTE_E2E", "0")
    monkeypatch.setenv("SYMNOTE_GOOGLE_CLIENT_ID", "synthetic-client")
    monkeypatch.setenv("SYMNOTE_GOOGLE_CLIENT_SECRET", "synthetic-secret")
    monkeypatch.setattr(
        auth, "exchange_google_code",
        lambda code, pending: {"email": "beta@example.test", "email_verified": True},
    )
    invite_user("beta@example.test", is_owner=invited_owner)
    app = create_app()
    browser = TestClient(app, base_url="https://testserver")
    second_browser = TestClient(app, base_url="https://testserver")

    def google_login(client: TestClient) -> httpx.Response:
        """外部通信なしで通常のstate付きコールバックを実行する。"""
        start = client.get("/api/auth/google/start", follow_redirects=False)
        assert start.status_code == 302
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        return client.get(
            f"/api/auth/google/callback?state={state}&code=synthetic-code",
            follow_redirects=False,
        )

    for client in (browser, second_browser):
        assert google_login(client).headers["location"] == "/"
        assert client.get("/api/auth/me").json()["authenticated"] is True
    user_id = browser.get("/api/auth/me").json()["user"]["id"]
    saved = browser.post(
        "/api/tasks", json={"title": "合成の再招待保持確認", "status": "inbox"},
        headers={"X-SymNote": "1"},
    )
    assert saved.status_code == 200
    item_id = saved.json()["task"]["id"]

    assert revoke_invite("beta@example.test") is True
    for client in (browser, second_browser):
        assert client.get("/api/auth/me").json()["authenticated"] is False
        assert client.get("/api/tasks").status_code == 401
    denied = google_login(browser)
    assert denied.headers["location"] == "/?auth_error=not_invited"
    assert browser.get("/api/auth/me").json()["authenticated"] is False
    assert browser.get("/api/tasks").status_code == 401

    invite_user("beta@example.test", is_owner=invited_owner)
    assert google_login(browser).headers["location"] == "/"
    assert browser.get("/api/auth/me").json()["user"]["id"] == user_id
    tasks = browser.get("/api/tasks?status=all").json()["tasks"]
    assert any(task["id"] == item_id for task in tasks)
    assert second_browser.get("/api/tasks").status_code == 401
