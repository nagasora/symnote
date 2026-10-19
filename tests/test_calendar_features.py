"""Google API フェイクで接続設定・予定取得・個別同期の契約を検証する。"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest

from symnote.core.calendar_events import (
    fetch_events,
    list_calendars,
    propose_prep_tasks_from_events,
)
from symnote.core.calendar_settings import (
    effective_config,
    save_client_json,
    set_setting,
    validate_client_json,
)
from symnote.core.candidates import list_candidates, reject_candidate
from symnote.core.db import (
    SCHEMA_VERSION,
    _migrate_db,
    connection,
    delete_item,
    init_db,
    insert_task,
    update_item_fields,
)
from symnote.core.google_calendar import sync_pending_tasks
from symnote.views.agenda_view import build_today_agenda


@pytest.fixture(autouse=True)
def _task_export_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """個別予定の書き出しを既定 OFF にして、朝のまとめなど他の同期だけを検証できるようにする。"""
    import symnote.core.calendar_task_export as task_export

    monkeypatch.setattr(task_export, "DEFAULT_EXPORT_TASKS", "0")


class ApiError(RuntimeError):
    """HTTP ステータスを持つ API 例外を模倣する。"""

    def __init__(self, status: int) -> None:
        """ステータスを偽の応答に保存する。"""
        super().__init__(f"HTTP {status}")
        self.resp = type("Response", (), {"status": status})()


class Request:
    """execute 時の結果と通信失敗を注入する。"""

    def __init__(self, result: Dict[str, Any], error: Exception | None = None) -> None:
        """API 応答と任意の例外を保存する。"""
        self.result = result
        self.error = error

    def execute(self) -> Dict[str, Any]:
        """通信例外または API 結果を返す。"""
        if self.error:
            raise self.error
        return self.result


class Service:
    """予定のリモート状態と API 呼び出しを記録する。"""

    def __init__(self) -> None:
        """空の予定と障害設定で開始する。"""
        self.remote: Dict[str, Dict[str, Any]] = {}
        self.items: List[Dict[str, Any]] = []
        self.calls: List[Dict[str, Any]] = []
        self.error: Exception | None = None
        self.lose_insert = False

    def events(self) -> Service:
        """予定リソースを返す。"""
        return self

    def calendarList(self) -> Service:
        """カレンダー一覧リソースを返す。"""
        return self

    def list(self, **kwargs: Any) -> Request:
        """ページ番号を含む予定の一覧を返す。"""
        self.calls.append({"method": "list", **kwargs})
        if "calendarId" not in kwargs:
            return Request(
                {"items": [{"id": "work", "summary": "仕事", "accessRole": "owner"}]}, self.error
            )
        if kwargs.get("pageToken") == "next":
            return Request({"items": self.items[1:]}, self.error)
        return Request(
            {"items": self.items[:1], **({"nextPageToken": "next"} if len(self.items) > 1 else {})},
            self.error,
        )

    def insert(self, **kwargs: Any) -> Request:
        """固定 ID の二重作成と応答消失を再現する。"""
        self.calls.append({"method": "insert", **kwargs})
        if self.error:
            return Request({}, self.error)
        key = kwargs["calendarId"] + ":" + kwargs["body"]["id"]
        if key in self.remote:
            return Request({}, ApiError(409))
        self.remote[key] = kwargs["body"]
        if self.lose_insert:
            self.lose_insert = False
            return Request({}, RuntimeError("offline"))
        return Request({"id": kwargs["body"]["id"]})

    def update(self, **kwargs: Any) -> Request:
        """既存予定の更新を再現する。"""
        self.calls.append({"method": "update", **kwargs})
        if self.error:
            return Request({}, self.error)
        key = kwargs["calendarId"] + ":" + kwargs["eventId"]
        if key not in self.remote:
            return Request({}, ApiError(404))
        self.remote[key] = kwargs["body"]
        return Request({})

    def delete(self, **kwargs: Any) -> Request:
        """存在しない予定の削除は 404 を返す。"""
        self.calls.append({"method": "delete", **kwargs})
        if self.error:
            return Request({}, self.error)
        key = kwargs["calendarId"] + ":" + kwargs["eventId"]
        if self.remote.pop(key, None) is None:
            return Request({}, ApiError(404))
        return Request({})


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """ユーザー DB には触れず一時 DB を初期化する。"""
    path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_SECRET_PATH", raising=False)
    monkeypatch.setenv("GOOGLE_CALENDAR_ID", "primary")
    init_db()
    return path


def event(identity: str, title: str = "会議", hour: int = 10) -> Dict[str, Any]:
    """予定取得用の時刻付き予定を作る。"""
    return {
        "id": identity,
        "summary": title,
        "start": {"dateTime": f"2026-07-20T{hour:02d}:00:00+09:00"},
        "end": {"dateTime": f"2026-07-20T{hour + 1:02d}:00:00+09:00"},
    }


def test_client_json_validation_and_private_atomic_save(database: Path) -> None:
    """Desktop JSON の保存先と権限を保証し、不正な再アップロードでは壊さない。"""
    payload = json.dumps(
        {"installed": {"client_id": "test-id", "client_secret": "fake-secret"}}
    ).encode()
    destination = save_client_json(payload)
    assert destination == database.parent / "google_oauth_client.json"
    assert destination.stat().st_mode & 0o777 == 0o600
    assert validate_client_json(destination.read_bytes())["installed"]["client_id"] == "test-id"
    for invalid in (b"not-json", b"[]", b'{"web": {}}', b'{"installed": {"client_id": "id"}}'):
        with pytest.raises(ValueError):
            save_client_json(invalid)
    assert destination.read_bytes() == payload
    assert effective_config().google_calendar_client_secret_path == str(destination)


def test_event_pages_short_cache_and_offline_fallback(database: Path) -> None:
    """全ページを取得し、短時間の再取得を抑制して、通信失敗では最後の成功結果を返す。"""
    service = Service()
    service.items = [event("a"), event("b", hour=11)]
    start, end = dt.date(2026, 7, 20), dt.date(2026, 7, 21)
    now = dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc)
    first = fetch_events(start, end, service_factory=lambda: service, now=now)
    assert len(first.events) == 2 and not first.cached and first.fetched_at
    assert len(service.calls) == 2
    second = fetch_events(
        start, end, service_factory=lambda: service, now=now + dt.timedelta(seconds=60)
    )
    assert second.cached and len(service.calls) == 2
    service.error = RuntimeError("network offline")
    stale = fetch_events(
        start, end, service_factory=lambda: service, now=now + dt.timedelta(minutes=10)
    )
    assert stale.offline and stale.cached and stale.events == first.events
    assert stale.fetched_at == first.fetched_at
    set_setting("calendar_id", "another")
    missing = fetch_events(start, end, service_factory=lambda: service, now=now)
    assert missing.offline and not missing.events and missing.fetched_at is None


def test_calendar_selection_is_persistent_and_env_is_fallback(database: Path) -> None:
    """保存済み選択が環境変数の既定値に優先する。"""
    assert effective_config().google_calendar_id == "primary"
    assert list_calendars(lambda: Service())[0]["id"] == "work"
    set_setting("calendar_id", "work")
    assert effective_config().google_calendar_id == "work"


def test_prep_candidates_are_pending_and_event_id_deduplicated(database: Path) -> None:
    """予定の改題や却下後にも同じ ID を再提案しない。"""
    service = Service()
    service.items = [event("meeting"), event("walk", "散歩")]
    today = dt.date(2026, 7, 20)
    first = propose_prep_tasks_from_events(service_factory=lambda: service, today=today)
    assert len(first.created_ids) == 1
    candidate = list_candidates()[0]
    assert candidate["source"] == "calendar" and candidate["source_ref"] == "meeting"
    assert candidate["due_time"] == "10:00" and "準備" in candidate["title"]
    reject_candidate(first.created_ids[0])
    service.items[0]["summary"] = "改題後の発表"
    fetch_events(
        today, today + dt.timedelta(days=7), service_factory=lambda: service, force_refresh=True
    )
    second = propose_prep_tasks_from_events(service_factory=lambda: service, today=today)
    assert not second.created_ids and second.duplicate_count == 1
    with connection() as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0


def sync(service: Service) -> Any:
    """朝のまとめが無い日で個別同期を実行する。"""
    return sync_pending_tasks(service_factory=lambda: service, today=dt.date(2026, 7, 19))


def test_export_create_update_complete_are_idempotent(database: Path) -> None:
    """任意設定で作成し、二度目は通信せず、変更で更新し、完了で削除する。"""
    task_id = insert_task("提出", due_date="2026-07-20", due_time="10:00")
    service = Service()
    assert sync(service).synced == 0 and not service.calls
    set_setting("export_tasks", "1")
    assert sync(service).synced == 1 and len(service.remote) == 1
    assert sync(service).synced == 0 and len(service.calls) == 1
    update_item_fields(task_id, due_time="11:00")
    assert sync(service).synced == 1
    assert service.calls[-1]["method"] == "update"
    assert next(iter(service.remote.values()))["start"]["dateTime"].endswith("11:00:00")
    update_item_fields(task_id, status="done")
    assert sync(service).synced == 1 and not service.remote
    assert sync(service).synced == 0


def test_export_response_loss_retries_same_id_and_retains_outbox(database: Path) -> None:
    """挿入が到達して応答を失っても、再試行では同じ予定を更新する。"""
    insert_task("発表", due_date="2026-07-20", due_time="14:00")
    set_setting("export_tasks", "1")
    service = Service()
    service.lose_insert = True
    assert sync(service).failed == 1 and len(service.remote) == 1
    with connection() as conn:
        assert conn.execute("SELECT count(*) FROM calendar_sync_outbox").fetchone()[0] == 1
    assert sync(service).synced == 1 and len(service.remote) == 1
    assert [call["method"] for call in service.calls] == ["insert", "insert", "update"]


def test_export_calendar_move_failure_and_disable_cleanup(database: Path) -> None:
    """旧カレンダーの削除に失敗したら移動を保留し、OFF で個別予定のみ消す。"""
    task = insert_task("提出", due_date="2026-07-20", due_time="10:00")
    insert_task("日付なし")
    service = Service()
    set_setting("export_tasks", "1")
    assert sync(service).synced == 1
    set_setting("calendar_id", "work")
    service.error = RuntimeError("offline")
    assert sync(service).failed == 1
    assert all(key.startswith("primary:") for key in service.remote)
    service.error = None
    assert sync(service).synced == 1
    assert all(key.startswith("work:") for key in service.remote)
    set_setting("export_tasks", "0")
    assert sync(service).synced == 1 and not service.remote
    assert sync(service).synced == 0
    delete_item(task)


def test_agenda_sorts_tasks_and_merges_overlapping_busy_times() -> None:
    """期限と予定を同じ順序に置き、重複した予定は空き時間から二重に引かない。"""
    events = [event("a"), event("b"), event("c", hour=11)]
    tasks = [{"due_date": "2026-07-20", "due_time": "09:00", "tags": "提出", "status": "today"}]
    rows, free = build_today_agenda(events, tasks, dt.date(2026, 7, 20), "Asia/Tokyo")
    assert rows[0]["kind"] == "タスク期限"
    assert free == 22 * 60
    all_day = {"id": "all", "start": {"date": "2026-07-20"}, "end": {"date": "2026-07-21"}}
    assert build_today_agenda([all_day], [], dt.date(2026, 7, 20), "Asia/Tokyo")[1] == 0


def test_v12_to_v13_migration_is_additive_and_repeatable(tmp_path: Path) -> None:
    """v12 の対応表と認証データを保ち、v13 の設定とキャッシュを追加する。"""
    with sqlite3.connect(tmp_path / "v12.db") as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE items(id INTEGER PRIMARY KEY, kind TEXT, status TEXT);"
            "CREATE TABLE task_recurrence_rules (id INTEGER PRIMARY KEY, "
            "frequency TEXT NOT NULL CHECK(frequency IN ('daily', 'weekly')), "
            "interval_days INTEGER NOT NULL, end_date TEXT, active INTEGER NOT NULL DEFAULT 1, "
            "created_at TEXT NOT NULL);"
            "CREATE TABLE calendar_task_sync(task_id INTEGER PRIMARY KEY, event_id TEXT, last_synced_at TEXT);"
            "INSERT INTO calendar_task_sync VALUES(1, 'existing', '2026-07-20');"
            "CREATE TABLE oauth_tokens(token_hash TEXT, consumed_at INTEGER);"
            "INSERT INTO oauth_tokens VALUES('unchanged', NULL);"
            "PRAGMA user_version = 12;"
        )
        _migrate_db(conn)
        _migrate_db(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT event_id FROM calendar_task_sync").fetchone()[0] == "existing"
        assert conn.execute("SELECT token_hash FROM oauth_tokens").fetchone()[0] == "unchanged"
        assert conn.execute("SELECT count(*) FROM calendar_event_cache").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM calendar_settings").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        ("access_denied fake-secret", "テストユーザー"),
        ("test user is not registered", "テストユーザー"),
        ("accessNotConfigured fake-secret", "未有効化"),
        ("network timeout fake-secret", "ネットワーク"),
        ("insufficient authentication scopes", "再接続"),
    ],
)
def test_calendar_error_messages_hide_secrets(cause: str, expected: str) -> None:
    """原因別の案内を返し、例外に含まれた秘密文字列は表示しない。"""
    from symnote.core.calendar_settings import calendar_error_message

    message = calendar_error_message(RuntimeError(cause))
    assert expected in message
    assert "fake-secret" not in message


def test_export_deletion_failure_and_already_deleted_are_retryable(database: Path) -> None:
    """タスク削除の失敗を再試行し、リモート予定が既に無ければ成功扱いにする。"""
    task_id = insert_task("提出", due_date="2026-07-20", due_time="10:00")
    service = Service()
    set_setting("export_tasks", "1")
    assert sync(service).synced == 1
    delete_item(task_id)
    service.error = RuntimeError("offline")
    assert sync(service).failed == 1
    with connection() as conn:
        assert conn.execute("SELECT count(*) FROM calendar_task_sync").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM calendar_sync_outbox").fetchone()[0] == 1
    service.error = None
    service.remote.clear()
    assert sync(service).synced == 1
    assert sync(service).synced == 0
    with connection() as conn:
        assert conn.execute("SELECT count(*) FROM calendar_task_sync").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM calendar_sync_outbox").fetchone()[0] == 0


def test_removing_due_time_turns_event_into_all_day(database: Path) -> None:
    """期限時刻だけ外したタスクは、同じ予定を終日予定に更新する。"""
    task_id = insert_task("提出", due_date="2026-07-20", due_time="10:00")
    service = Service()
    set_setting("export_tasks", "1")
    assert sync(service).synced == 1
    update_item_fields(task_id, due_time=None)
    assert sync(service).synced == 1 and len(service.remote) == 1
    assert service.calls[-1]["method"] == "update"
    assert next(iter(service.remote.values()))["start"] == {"date": "2026-07-20"}


def test_removing_due_date_deletes_exported_event(database: Path) -> None:
    """期限日がなくなったタスクは個別予定から除く。"""
    task_id = insert_task("提出", due_date="2026-07-20")
    service = Service()
    set_setting("export_tasks", "1")
    assert sync(service).synced == 1
    update_item_fields(task_id, due_date=None)
    assert sync(service).synced == 1 and not service.remote


def test_failed_second_page_does_not_replace_complete_cache(database: Path) -> None:
    """ページ途中で取得に失敗しても最終成功スナップショットを部分データで壊さない。"""
    service = Service()
    service.items = [event("old")]
    begin, end = dt.date(2026, 7, 20), dt.date(2026, 7, 21)
    saved = fetch_events(begin, end, service_factory=lambda: service)
    service.items = [event("new-1"), event("new-2")]
    original_list = service.list

    def fail_second_page(**kwargs: Any) -> Request:
        """二ページ目だけで通信失敗を返す。"""
        if kwargs.get("pageToken"):
            return Request({}, RuntimeError("offline"))
        return original_list(**kwargs)

    service.list = fail_second_page
    fallback = fetch_events(begin, end, service_factory=lambda: service, force_refresh=True)
    assert fallback.offline and fallback.events == saved.events
    assert fallback.fetched_at == saved.fetched_at


@pytest.mark.parametrize("mutation", ["complete", "delete", "disable", "remove_date"])
def test_lost_create_response_still_allows_remote_cleanup(database: Path, mutation: str) -> None:
    """作成応答を失った未確定予定も、タスクを消す変更があればリモートから削除する。"""
    task_id = insert_task("提出", due_date="2026-07-20", due_time="10:00")
    set_setting("export_tasks", "1")
    service = Service()
    service.lose_insert = True
    assert sync(service).failed == 1 and len(service.remote) == 1
    remote_key = next(iter(service.remote))
    with connection() as conn:
        pending = conn.execute(
            "SELECT event_id, calendar_id, last_synced_at, content_hash "
            "FROM calendar_task_sync WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert pending["calendar_id"] + ":" + pending["event_id"] == remote_key
        assert pending["last_synced_at"] is None and pending["content_hash"] is None
    if mutation == "complete":
        update_item_fields(task_id, status="done")
    elif mutation == "delete":
        delete_item(task_id)
    elif mutation == "disable":
        set_setting("export_tasks", "0")
    else:
        update_item_fields(task_id, due_date=None, due_time=None)
    assert sync(service).synced == 1 and not service.remote
    assert service.calls[-1]["method"] == "delete"
    assert sync(service).synced == 0
    with connection() as conn:
        assert conn.execute("SELECT count(*) FROM calendar_task_sync").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM calendar_sync_outbox").fetchone()[0] == 0


def test_lost_create_response_then_move_cleans_old_calendar(database: Path) -> None:
    """応答未確定の予定も旧カレンダーから削除してから新しい送信先へ移す。"""
    insert_task("提出", due_date="2026-07-20", due_time="10:00")
    set_setting("export_tasks", "1")
    service = Service()
    service.lose_insert = True
    assert sync(service).failed == 1
    set_setting("calendar_id", "work")
    assert sync(service).synced == 1 and len(service.remote) == 1
    assert next(iter(service.remote)).startswith("work:")
    assert [call["method"] for call in service.calls] == ["insert", "delete", "insert"]


def test_legacy_oauth_scope_survives_load_refresh_and_event_operations(
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧 scope を追加権限へ上書きせず、更新後も予定取得と既存の書き出しを継続する。"""
    import google.oauth2.credentials
    import googleapiclient.discovery
    import symnote.core.google_calendar as calendar

    legacy_scopes = ["https://www.googleapis.com/auth/calendar.events"]

    class SavedKeyring:
        """実 OS の資格情報を使わず、旧 token の復元と更新を記録する。"""

        def __init__(self) -> None:
            """期限切れの旧 scope のみを承認した token を用意する。"""
            self.payload = json.dumps(
                {
                    "token": "fake-access",
                    "refresh_token": "fake-refresh",
                    "client_id": "fake-client",
                    "client_secret": "fake-secret",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "scopes": legacy_scopes,
                    "expiry": "2020-01-01T00:00:00Z",
                }
            )

        def get_password(self, service: str, username: str) -> str:
            """テスト専用 token を返す。"""
            return self.payload

        def set_password(self, service: str, username: str, payload: str) -> None:
            """更新後の token を記録する。"""
            self.payload = payload

    keyring = SavedKeyring()
    service = Service()
    service.items = [event("meeting")]
    refresh_scopes: List[List[str]] = []

    def refresh(credentials: Any, request: Any) -> None:
        """旧 scope のままで更新されることを検証し、通信せずに有効化する。"""
        refresh_scopes.append(list(credentials.scopes))
        assert credentials.scopes == legacy_scopes
        credentials.token = "fake-updated"
        credentials.expiry = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(
            hours=1
        )

    def build(*args: Any, **kwargs: Any) -> Service:
        """旧 scope の認証情報でフェイク予定 API を作る。"""
        assert kwargs["credentials"].scopes == legacy_scopes
        return service

    monkeypatch.setattr(calendar, "_keyring", lambda: keyring)
    monkeypatch.setattr(google.oauth2.credentials.Credentials, "refresh", refresh)
    monkeypatch.setattr(googleapiclient.discovery, "build", build)
    assert calendar._load_credentials().scopes == legacy_scopes
    assert calendar._calendar_service() is service
    assert refresh_scopes == [legacy_scopes]
    assert json.loads(keyring.payload)["scopes"] == legacy_scopes
    fetched = fetch_events(dt.date(2026, 7, 20), dt.date(2026, 7, 21))
    assert not fetched.offline and fetched.events[0]["id"] == "meeting"
    insert_task("旧接続でのまとめ", due_date="2026-07-20")
    assert calendar.sync_pending_tasks(today=dt.date(2026, 7, 20)).synced == 1


def test_calendar_list_permission_failure_does_not_disable_event_operations(database: Path) -> None:
    """一覧権限不足だけを再接続案内へ変換し、イベント用の機能は遮断しない。"""
    from symnote.core.calendar_events import CalendarListPermissionError

    service = Service()
    error = ApiError(403)
    error.args = ("Request had insufficient authentication scopes.",)
    service.error = error
    with pytest.raises(CalendarListPermissionError, match="再接続（権限の更新）"):
        list_calendars(lambda: service)
    service.error = None
    service.items = [event("readable")]
    fetched = fetch_events(
        dt.date(2026, 7, 20), dt.date(2026, 7, 21), service_factory=lambda: service
    )
    assert fetched.events[0]["id"] == "readable" and not fetched.offline
    insert_task("提出", due_date="2026-07-20", due_time="10:00")
    set_setting("export_tasks", "1")
    assert sync(service).synced == 1


def test_exported_task_events_do_not_count_as_busy_time() -> None:
    """SymNote が書き出した ToDo の予定は、空き時間・予定一覧の集計から除く。"""
    from symnote.core.calendar_events import is_symnote_generated
    from symnote.core.timeblock import normalize_calendar_events

    task_event = {
        "id": "t1",
        "summary": "提出",
        "start": {"date": "2026-07-20"},
        "end": {"date": "2026-07-21"},
        "extendedProperties": {"private": {"symnote_task_id": "7"}},
    }
    meeting = event("m1")

    assert is_symnote_generated(task_event)
    assert not is_symnote_generated(meeting)
    normalized = normalize_calendar_events([task_event, meeting], dt.date(2026, 7, 20), "Asia/Tokyo")
    assert [e["id"] for e in normalized] == ["m1"]


def test_date_only_task_is_exported_by_default(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """設定を触っていなければ、期限日のある ToDo は自動で終日予定として登録される。"""
    import symnote.core.calendar_task_export as task_export

    monkeypatch.setattr(task_export, "DEFAULT_EXPORT_TASKS", "1")
    insert_task("請求書を送る", due_date="2026-07-20")
    insert_task("いつかやる")
    service = Service()

    assert sync(service).synced == 1
    assert next(iter(service.remote.values()))["start"] == {"date": "2026-07-20"}
