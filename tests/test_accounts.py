"""マルチテナント管理（accounts.py）・データ分離・ファイル権限のテスト。"""

from __future__ import annotations

from pathlib import Path
import stat
import pytest

from symnote import admin_cli
from symnote.core import db
from symnote.core.accounts import (
    InviteRequiredError,
    authenticate_email,
    complete_onboarding,
    create_session,
    get_user_by_session,
    invite_user,
    list_invites,
    revoke_invite,
    revoke_session,
    system_db_path,
    user_scope,
)


@pytest.fixture(autouse=True)
def isolated_multiuser_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    owner_db = tmp_path / "owner" / "symnote.db"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DB_PATH", str(owner_db))
    monkeypatch.setenv("SYMNOTE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SYMNOTE_OWNER_EMAIL", "owner@example.com")
    return tmp_path


def test_owner_maps_to_existing_db_path_and_invited_user_gets_isolated_db(tmp_path: Path) -> None:
    owner = authenticate_email("owner@example.com", name="永松 オーナー")
    assert owner.is_owner is True
    assert Path(owner.db_path) == (tmp_path / "owner" / "symnote.db")
    assert owner.onboarded_at is not None

    # 未招待ユーザーは拒否される
    with pytest.raises(InviteRequiredError):
        authenticate_email("stranger@example.com", name="Stranger")

    # 招待したユーザーは専用DBが自動生成される（権限 600）
    invite_user("friend@example.com", note="ベータ招待")
    friend = authenticate_email("friend@example.com", name="友人A")
    assert friend.is_owner is False
    assert friend.onboarded_at is None
    assert Path(friend.db_path) != Path(owner.db_path)
    assert Path(friend.db_path).exists()

    mode = stat.S_IMODE(Path(friend.db_path).stat().st_mode)
    assert mode == 0o600
    sys_mode = stat.S_IMODE(Path(system_db_path()).stat().st_mode)
    assert sys_mode == 0o600

    # user_scope でそれぞれの DB に完全分離されて読み書きされる
    with user_scope(owner):
        owner_tid = db.insert_task(raw_text="オーナーの秘密タスク", tags="オーナーの秘密タスク")
        assert len(db.fetch_tasks()) == 1

    with user_scope(friend):
        assert db.fetch_tasks() == []
        assert db.fetch_item(owner_tid) is None
        friend_tid = db.insert_task(raw_text="友人Aのタスク", tags="友人Aのタスク")
        assert len(db.fetch_tasks()) == 1
        assert db.fetch_tasks()[0]["id"] == friend_tid

    with user_scope(owner):
        tasks = db.fetch_tasks()
        assert len(tasks) == 1
        assert tasks[0]["tags"] == "オーナーの秘密タスク"


def test_session_lifecycle_and_invite_revocation() -> None:
    invite_user("beta@example.com")
    user = authenticate_email("beta@example.com", name="Beta User")
    token = create_session(user.id)

    loaded = get_user_by_session(token)
    assert loaded is not None
    assert loaded.id == user.id

    onboarded = complete_onboarding(user.id, name="ベータ太郎")
    assert onboarded.onboarded_at is not None
    assert onboarded.name == "ベータ太郎"

    revoke_session(token)
    assert get_user_by_session(token) is None

    # 再ログイン後に招待を取り消すとセッションも無効化される
    token2 = create_session(user.id)
    assert get_user_by_session(token2) is not None
    assert revoke_invite("beta@example.com") is True
    assert get_user_by_session(token2) is None


def test_admin_cli_invite_list_revoke(capsys: pytest.CaptureFixture[str]) -> None:
    assert admin_cli.main(["invite", "alice@example.com", "--note", "tester"]) == 0
    assert len(list_invites()) == 1
    assert admin_cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "alice@example.com" in out
    assert admin_cli.main(["revoke", "alice@example.com"]) == 0
    assert list_invites() == []


@pytest.mark.parametrize("invited_owner", [False, True])
def test_revoked_invite_rejects_relogin_and_preserves_user_data(invited_owner: bool) -> None:
    """招待取消は全セッションと再認証を拒否し、再招待時は既存データを保つ。"""
    email = "revoked@example.com"
    invite_user(email, is_owner=invited_owner)
    user = authenticate_email(email, name="Revoked User")
    tokens = [create_session(user.id), create_session(user.id)]
    with user_scope(user):
        task_id = db.insert_task(raw_text="合成の保存確認", tags="合成の保存確認")
    assert all(get_user_by_session(token) is not None for token in tokens)

    assert revoke_invite("  REVOKED@EXAMPLE.COM  ") is True
    assert all(get_user_by_session(token) is None for token in tokens)
    with pytest.raises(InviteRequiredError):
        authenticate_email("  REVOKED@EXAMPLE.COM  ", name="Rejected Change")

    invite_user(email, is_owner=invited_owner)
    restored = authenticate_email(email)
    assert restored.id == user.id
    assert restored.db_path == user.db_path
    assert restored.name == "Revoked User"
    with user_scope(restored):
        assert db.fetch_item(task_id) is not None
    assert get_user_by_session(create_session(restored.id)) is not None
    assert all(get_user_by_session(token) is None for token in tokens)


@pytest.mark.parametrize("invited_owner", [False, True])
def test_session_creation_rechecks_invite_after_authentication(invited_owner: bool) -> None:
    """認証とセッション発行の間で招待が取り消されてもアクセスを復活させない。"""
    invite_user("race@example.com", is_owner=invited_owner)
    user = authenticate_email("race@example.com")
    assert revoke_invite(user.email) is True
    with pytest.raises(InviteRequiredError):
        create_session(user.id)


def test_configured_owner_can_reauthenticate_without_invite() -> None:
    """環境設定で指定された本人オーナーの既存ログイン経路を保つ。"""
    owner = authenticate_email("owner@example.com")
    token = create_session(owner.id)
    assert revoke_invite(owner.email) is True
    assert get_user_by_session(token) is None
    assert authenticate_email(owner.email).id == owner.id
    assert get_user_by_session(create_session(owner.id)) is not None


def test_local_owner_session_does_not_require_an_invite(monkeypatch: pytest.MonkeyPatch) -> None:
    """オーナーメール未設定のローカル本人経路を維持する。"""
    from symnote.core.accounts import ensure_local_owner_user

    monkeypatch.delenv("SYMNOTE_OWNER_EMAIL", raising=False)
    owner = ensure_local_owner_user()
    assert list_invites() == []
    assert get_user_by_session(create_session(owner.id)) is not None


@pytest.mark.parametrize("invited_owner", [False, True])
def test_old_session_with_missing_invite_is_rejected(invited_owner: bool) -> None:
    """過去の不具合で残ったセッションも現在の招待がなければ利用させない。"""
    import sqlite3

    invite_user("stale@example.com", is_owner=invited_owner)
    user = authenticate_email("stale@example.com")
    token = create_session(user.id)
    with sqlite3.connect(system_db_path()) as conn:
        conn.execute("DELETE FROM invites WHERE email = ?", (user.email,))
    assert get_user_by_session(token) is None
    with pytest.raises(InviteRequiredError):
        authenticate_email(user.email)
    with pytest.raises(InviteRequiredError):
        create_session(user.id)
