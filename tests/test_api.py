"""SymNote Web API（Stage 1）とマルチユーザー完全分離の統合テスト。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse
import pytest
from starlette.testclient import TestClient

from symnote.api.app import create_app
from symnote.core import candidates as candidate_store
from symnote.core.accounts import authenticate_email, invite_user, user_scope

CSRF = {"X-SymNote": "1"}


@pytest.fixture()
def api_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "owner" / "symnote.db"))
    monkeypatch.setenv("SYMNOTE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SYMNOTE_OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("SYMNOTE_E2E", "1")
    monkeypatch.setenv("SYMNOTE_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("SYMNOTE_PUBLIC_ORIGIN", "")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    return tmp_path


def _login_client(app, email: str, *, name: str = "", is_owner: bool = False) -> TestClient:
    client = TestClient(app)
    res = client.post(
        "/api/auth/e2e-login",
        json={"email": email, "name": name, "is_owner": is_owner},
        headers=CSRF,
    )
    assert res.status_code == 200, res.text
    return client


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
