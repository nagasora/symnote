"""テスト環境（本番 DB の複製・パスコード・カレンダー読み取り専用）の動作を保証する。"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any, List

import pytest
from streamlit.testing.v1 import AppTest

from symnote.core import google_calendar
from symnote.core.calendar_settings import calendar_error_message
from symnote.core.runtime import (
    ReadOnlyCalendarService,
    CalendarWriteBlocked,
    is_test_mode,
    passcode_matches,
)

ROOT = Path(__file__).resolve().parents[1]


def _load_prepare_module() -> Any:
    spec = importlib.util.spec_from_file_location("prepare_test_env", ROOT / "scripts/prepare_test_env.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["prepare_test_env"] = module
    spec.loader.exec_module(module)
    return module


class _Request:
    def __init__(self, name: str, calls: List[str]) -> None:
        self.name, self.calls = name, calls

    def execute(self) -> dict:
        self.calls.append(self.name)
        return {}


class _Events:
    def __init__(self, calls: List[str]) -> None:
        self.calls = calls

    def list(self, **_: Any) -> _Request:
        return _Request("list", self.calls)

    def insert(self, **_: Any) -> _Request:
        return _Request("insert", self.calls)

    def delete(self, **_: Any) -> _Request:
        return _Request("delete", self.calls)


class _Service:
    def __init__(self) -> None:
        self.calls: List[str] = []

    def events(self) -> _Events:
        return _Events(self.calls)


def test_test_mode_is_off_unless_explicitly_enabled(monkeypatch) -> None:
    monkeypatch.delenv("SYMNOTE_TEST_MODE", raising=False)
    assert not is_test_mode()
    monkeypatch.setenv("SYMNOTE_TEST_MODE", "1")
    assert is_test_mode()


def test_passcode_requires_exact_match_and_empty_means_unlocked() -> None:
    assert passcode_matches(" 123456 ", "123456")
    assert not passcode_matches("12345", "123456")
    assert passcode_matches("anything", "")


def test_read_only_calendar_allows_reads_and_blocks_writes() -> None:
    service = _Service()
    wrapped = ReadOnlyCalendarService(service)

    wrapped.events().list(calendarId="primary").execute()
    with pytest.raises(CalendarWriteBlocked):
        wrapped.events().insert(calendarId="primary", body={})
    with pytest.raises(CalendarWriteBlocked):
        wrapped.events().delete(calendarId="primary", eventId="x")

    assert service.calls == ["list"]


def test_test_mode_never_changes_shared_google_credentials_or_syncs(monkeypatch) -> None:
    monkeypatch.setenv("SYMNOTE_TEST_MODE", "1")
    monkeypatch.setattr(google_calendar, "_keyring", lambda: pytest.fail("keyring に触れてはいけない"))

    with pytest.raises(CalendarWriteBlocked) as disconnect_error:
        google_calendar.disconnect()
    with pytest.raises(CalendarWriteBlocked):
        google_calendar.connect()
    result = google_calendar.sync_pending_tasks(service_factory=lambda: pytest.fail("同期してはいけない"))

    assert "テスト環境" in result.message
    assert "テスト環境" in calendar_error_message(disconnect_error.value)


def test_copy_database_makes_consistent_copy_without_touching_source(tmp_path) -> None:
    prepare = _load_prepare_module()
    source = tmp_path / "prod.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, raw_text TEXT)")
        conn.execute("INSERT INTO items (raw_text) VALUES ('本番のタスク')")
    before = source.read_bytes()
    target = tmp_path / "test" / "copy.db"

    prepare.copy_database(source, target)
    with sqlite3.connect(target) as conn:
        conn.execute("INSERT INTO items (raw_text) VALUES ('テストで追加')")

    assert source.read_bytes() == before
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 2


def test_prepare_refuses_when_target_is_the_production_db(tmp_path) -> None:
    prepare = _load_prepare_module()
    source = tmp_path / "prod.db"
    sqlite3.connect(source).close()

    code = prepare.main(
        ["--source", str(source), "--target", str(source), "--passcode-file", str(tmp_path / "p"),
         "--port", "8520", "--reset"]
    )

    assert code == 1


def test_passcode_is_generated_once_and_kept_private(tmp_path) -> None:
    prepare = _load_prepare_module()
    path = tmp_path / "passcode"

    first = prepare.ensure_passcode(path)

    assert first.isdigit() and len(first) == 6
    assert prepare.ensure_passcode(path) == first
    assert path.stat().st_mode & 0o777 == 0o600


def test_app_asks_for_passcode_before_showing_data(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "ui.db"))
    monkeypatch.setenv("SYMNOTE_APP_PASSCODE", "246810")
    monkeypatch.setenv("SYMNOTE_TEST_MODE", "1")
    app = AppTest.from_file(str(ROOT / "src/symnote/app.py"), default_timeout=15)
    app.run()

    assert not app.exception
    assert not app.sidebar.radio
    assert not (tmp_path / "ui.db").exists()

    app.text_input(key="passcode_input").input("000000")
    app.button[0].click().run()
    assert any("違います" in e.value for e in app.error)
    assert not app.sidebar.radio

    app.text_input(key="passcode_input").input("246810")
    app.button[0].click().run()
    assert not app.exception
    assert app.sidebar.radio[0].value == "今日"
    assert any("テスト環境" in block.value for block in app.markdown)


def test_public_bind_detection(monkeypatch) -> None:
    """明示的に外部へ公開する待ち受けだけを公開とみなし、Docker 用の明示的な許可を尊重する。"""
    from symnote.core.runtime import is_public_bind

    monkeypatch.delenv("SYMNOTE_ALLOW_NO_PASSCODE", raising=False)
    assert [is_public_bind(a) for a in ["", "localhost", "127.0.0.1", "::1"]] == [False] * 4
    assert [is_public_bind(a) for a in ["0.0.0.0", "192.168.1.2", "::"]] == [True] * 3
    monkeypatch.setenv("SYMNOTE_ALLOW_NO_PASSCODE", "1")
    assert not is_public_bind("0.0.0.0")


def test_passcode_limiter_locks_after_repeated_failures_and_recovers() -> None:
    """続けて間違えると一定時間ロックし、時間がたつか成功すると解除される。"""
    from symnote.core.runtime import PasscodeLimiter

    limiter = PasscodeLimiter(max_failures=3, window=60)
    for second in range(3):
        assert limiter.locked_for(now=1000 + second) == 0
        limiter.record_failure(now=1000 + second)
    assert limiter.locked_for(now=1003) > 0
    assert limiter.locked_for(now=1061) == 0
    limiter.record_failure(now=1062)
    limiter.reset()
    assert limiter.locked_for(now=1062) == 0


def test_public_app_without_passcode_shows_no_data(monkeypatch, tmp_path) -> None:
    """外部公開の待ち受けでパスコード未設定なら、データを表示しない。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "ui.db"))
    monkeypatch.delenv("SYMNOTE_APP_PASSCODE", raising=False)
    monkeypatch.delenv("SYMNOTE_ALLOW_NO_PASSCODE", raising=False)
    app = AppTest.from_file(str(ROOT / "src/symnote/app.py"), default_timeout=15)
    import streamlit as st

    monkeypatch.setattr(st, "get_option", lambda name: "0.0.0.0" if name == "server.address" else None)
    app.run()

    assert not app.exception
    assert not app.sidebar.radio
    assert any("パスコードが未設定" in e.value for e in app.error)


def test_tunnel_url_is_read_from_cloudflared_log() -> None:
    prepare_module = _load_prepare_module()
    log = (
        "INF Requesting new quick Tunnel on trycloudflare.com...\n"
        "INF |  https://brave-sample-words-here.trycloudflare.com  |\n"
    )

    assert prepare_module.parse_tunnel_url(log) == "https://brave-sample-words-here.trycloudflare.com"
    assert prepare_module.parse_tunnel_url("INF Requesting new quick Tunnel on trycloudflare.com...") is None


def test_remote_url_is_shown_instead_of_lan_address(tmp_path, capsys) -> None:
    prepare_module = _load_prepare_module()
    passcode = tmp_path / "passcode"

    prepare_module.main([
        "--source", str(tmp_path / "prod.db"), "--target", str(tmp_path / "test.db"),
        "--passcode-file", str(passcode), "--port", "8520", "--show-only",
        "--remote-url", "https://brave-sample-words-here.trycloudflare.com",
    ])

    out = capsys.readouterr().out
    assert "https://brave-sample-words-here.trycloudflare.com" in out
    assert "同じ Wi-Fi" not in out


def test_tunnel_removed_by_cloudflare_is_detected_from_log() -> None:
    """登録後に Cloudflare 側で消されたトンネルは「切れている」と判定する。"""
    prepare_module = _load_prepare_module()
    registered = "INF Registered tunnel connection connIndex=0\n"
    removed = 'ERR Register tunnel error from server side error="Unauthorized: Tunnel not found"\n'

    assert prepare_module.tunnel_is_registered(registered)
    assert not prepare_module.tunnel_is_registered(registered + removed)
    assert not prepare_module.tunnel_is_registered("INF Requesting new quick Tunnel\n")
    assert prepare_module.tunnel_is_registered(removed + registered)


def test_passcode_attempts_stop_being_checked_once_locked() -> None:
    """上限まで間違えると、正しいパスコードでもロック中は通さない。成功すると失敗回数は消える。"""
    from symnote.core.runtime import PasscodeLimiter

    limiter = PasscodeLimiter(max_failures=2, window=60)
    assert limiter.attempt("111111", "135790", now=1000) is False
    assert limiter.attempt("135790", "135790", now=1001) is True
    assert limiter.attempt("111111", "135790", now=1002) is False
    assert limiter.attempt("222222", "135790", now=1003) is False
    assert limiter.attempt("135790", "135790", now=1004) is False
    assert limiter.attempt("135790", "135790", now=1063) is True


def test_concurrent_passcode_attempts_cannot_exceed_the_limit(monkeypatch) -> None:
    """複数のセッションから同時に送っても、照合されるのは上限回数まで。"""
    import threading

    from symnote.core import runtime

    limiter = runtime.PasscodeLimiter(max_failures=5, window=60)
    checked: list = []
    original = runtime.passcode_matches

    def counting(entered: str, expected: str) -> bool:
        checked.append(entered)
        return original(entered, expected)

    monkeypatch.setattr(runtime, "passcode_matches", counting)
    threads = [threading.Thread(target=limiter.attempt, args=(f"{i:06d}", "999999")) for i in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(checked) == 5


def test_passcode_lockout_of_one_source_does_not_lock_out_another() -> None:
    """ある送信元が上限まで間違えても、別の送信元（本人）は引き続き入力できる。"""
    from symnote.core.runtime import PasscodeLimiter

    limiter = PasscodeLimiter(max_failures=3, window=60, global_max_failures=10)
    for second in range(3):
        assert limiter.attempt("000000", "135790", now=1000 + second, source="attacker") is False
    assert limiter.locked_for(now=1003, source="attacker") > 0
    assert limiter.locked_for(now=1003, source="owner") == 0
    assert limiter.attempt("135790", "135790", now=1003, source="owner") is True


def test_passcode_global_cap_stops_guessing_spread_over_many_sources() -> None:
    """送信元を変えながら試しても、全体の上限に達したら誰も照合できない。"""
    from symnote.core.runtime import PasscodeLimiter

    limiter = PasscodeLimiter(max_failures=3, window=60, global_max_failures=5)
    for index in range(5):
        limiter.attempt("000000", "135790", now=1000 + index, source=f"ip-{index}")
    assert limiter.attempt("135790", "135790", now=1006, source="fresh") is False
    assert limiter.attempt("135790", "135790", now=1061, source="fresh") is True


def test_passcode_limiter_bounds_tracked_sources(monkeypatch) -> None:
    """記録する送信元の数に上限があり、送信元を変え続けてもメモリが増え続けない。"""
    from symnote.core import runtime

    monkeypatch.setattr(runtime, "MAX_TRACKED_PASSCODE_SOURCES", 4)
    limiter = runtime.PasscodeLimiter(max_failures=3, window=600, global_max_failures=1000)
    for index in range(20):
        limiter.record_failure(now=1000 + index, source=f"ip-{index}")
    assert len(limiter._failures) == 4


def test_passcode_source_uses_cloudflare_client_ip_or_direct() -> None:
    """Cloudflare 経由なら利用者の IP を、直接接続なら "direct" を送信元にする。"""
    from symnote.core.runtime import passcode_source

    assert passcode_source({"Cf-Connecting-Ip": " 203.0.113.5 "}) == "203.0.113.5"
    assert passcode_source({"Host": "localhost:8520"}) == "direct"
    assert passcode_source({}) == "direct"
