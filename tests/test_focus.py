"""一時DBで集中セッションの排他・原子性・報酬・日別集計と表示を保証する。"""

from __future__ import annotations

import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, List, Tuple
from unittest.mock import patch

import pytest

with patch("dotenv.load_dotenv", return_value=False):
    from symnote.core import db, focus, quest

START = datetime(2026, 10, 16, 12)


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ユーザーDBに接続せず、各テスト専用のスキーマを生成する。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "focus.db"))
    db.init_db()


def _start(minutes: int = 25, *, now: datetime = START) -> Tuple[int, int]:
    """新しい未完了タスクとその集中を作る。"""
    task_id = db.insert_item(kind="task", raw_text="今これだけ")
    return task_id, focus.start_session(task_id, minutes, now=now)


def test_start_and_active_session() -> None:
    """開始時点の名前・予定時間・時刻を保存し、終了前の一件を取得する。"""
    assert focus.active_session() is None
    task_id, session_id = _start()
    session = focus.active_session()
    assert session is not None
    assert (session.id, session.task_id, session.title) == (session_id, task_id, "今これだけ")
    assert session.started_at == START and session.ended_at is None
    assert session.planned_minutes == 25 and session.outcome is None
    with pytest.raises(focus.FocusSessionActiveError):
        focus.start_session(task_id, 15, now=START)


@pytest.mark.parametrize("minutes", [0, -1, 1, 20, 25.0, True, "25"])
def test_invalid_duration(minutes: Any) -> None:
    """15・25・50以外の値や整数でない値は保存しない。"""
    task_id = db.insert_task("集中候補")
    with pytest.raises(ValueError):
        focus.start_session(task_id, minutes, now=START)
    assert focus.active_session() is None


@pytest.mark.parametrize("kind,status", [("memo", "inbox"), ("task", "done")])
def test_non_active_task_cannot_start(kind: str, status: str) -> None:
    """メモと完了済みタスクは集中対象にならない。"""
    task_id = db.insert_item(kind=kind, raw_text="対象外", status=status)
    with pytest.raises(ValueError):
        focus.start_session(task_id, 25, now=START)
    with pytest.raises(ValueError):
        focus.start_session(99999, 25, now=START)


def test_concurrent_starts_create_only_one() -> None:
    """同時開始を直列化して進行中の記録を一件に限定する。"""
    task_id = db.insert_task("並列開始")

    def start(_: int) -> bool:
        """開始の成否をスレッドから返す。"""
        try:
            focus.start_session(task_id, 15, now=START)
            return True
        except focus.FocusSessionActiveError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(start, range(2))) == 1
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM focus_sessions").fetchone()[0] == 1


@pytest.mark.parametrize("minutes", [15, 25, 50])
@pytest.mark.parametrize("relative_seconds,eligible", [(-1, False), (0, True), (60, True)])
def test_bonus_threshold(minutes: int, relative_seconds: int, eligible: bool) -> None:
    """予定時間80%の境界からボーナスを付け、それ未満には付けない。"""
    task_id, session_id = _start(minutes)
    end = START + timedelta(seconds=minutes * 60 * focus.BONUS_REQUIRED_RATIO + relative_seconds)
    result = focus.finish_session(session_id, complete_task=False, now=end)
    assert result.session.outcome == "done"
    assert result.session.ended_at == end
    assert focus.active_session() is None
    assert (result.bonus_reward is not None) == eligible
    expected = (focus.FOCUS_BONUS_XP, focus.FOCUS_BONUS_GOLD) if eligible else (0, 0)
    assert (result.session.bonus_xp, result.session.bonus_gold) == expected
    stats = quest.get_player_stats(START.date())
    assert (stats.xp_total, stats.gold) == expected
    with db.connection() as conn:
        assert (
            conn.execute("SELECT status FROM items WHERE id = ?", (task_id,)).fetchone()[0]
            == "inbox"
        )
    assert quest.pop_unseen_rewards() == []


def test_task_completion_awards_base_and_bonus_once() -> None:
    """通常報酬と集中報酬を別々に計上し、追加分も祝賀と交換に利用できる。"""
    task_id, session_id = _start()
    result = focus.finish_session(session_id, complete_task=True, now=START + timedelta(minutes=25))
    base = quest.pop_unseen_rewards()
    assert len(base) == 1 and base[0].task_id == task_id
    assert result.bonus_reward is not None
    assert "フォーカスボーナス" in result.bonus_reward.bonuses[0]
    stats = quest.get_player_stats()
    assert stats.xp_total == base[0].xp + focus.FOCUS_BONUS_XP
    assert stats.gold == base[0].gold + focus.FOCUS_BONUS_GOLD
    with db.connection() as conn:
        assert (
            conn.execute("SELECT status FROM items WHERE id = ?", (task_id,)).fetchone()[0]
            == "done"
        )
        assert conn.execute("SELECT COUNT(*) FROM quest_log").fetchone()[0] == 1
    db.complete_task(task_id)
    assert quest.pop_unseen_rewards() == []
    reward_id = quest.add_reward("集中後のお茶", stats.gold)
    assert quest.redeem_reward(reward_id) == 0
    with pytest.raises(quest.InsufficientGoldError):
        quest.redeem_reward(reward_id)


def test_short_completion_still_awards_normal_quest() -> None:
    """早めのタスク完了でも通常報酬は付き、集中ボーナスだけを付けない。"""
    _, session_id = _start()
    result = focus.finish_session(session_id, complete_task=True, now=START + timedelta(seconds=10))
    assert result.session.outcome == "done" and result.bonus_reward is None
    base = quest.pop_unseen_rewards()
    assert len(base) == 1
    assert quest.get_player_stats().xp_total == base[0].xp


def test_stop_has_no_bonus_or_task_completion() -> None:
    """予定時間を過ぎて中断しても通常報酬・追加報酬を発生させない。"""
    task_id, session_id = _start()
    result = focus.stop_session(session_id, now=START + timedelta(minutes=40))
    assert result.session.outcome == "stopped" and result.bonus_reward is None
    assert (result.session.bonus_xp, result.session.bonus_gold) == (0, 0)
    with db.connection() as conn:
        assert (
            conn.execute("SELECT status FROM items WHERE id = ?", (task_id,)).fetchone()[0]
            == "inbox"
        )
    assert quest.get_player_stats().xp_total == 0
    assert focus.active_session() is None
    focus.start_session(task_id, 50, now=START + timedelta(minutes=40))


@pytest.mark.parametrize("first_stopped", [True, False])
def test_double_end_is_rejected(first_stopped: bool) -> None:
    """完了後・中断後の再操作が報酬と結果を書き換えない。"""
    _, session_id = _start()
    end = START + timedelta(minutes=25)
    if first_stopped:
        focus.stop_session(session_id, now=end)
    else:
        focus.finish_session(session_id, complete_task=True, now=end)
    before = quest.get_player_stats()
    with pytest.raises(focus.FocusSessionEndedError):
        focus.finish_session(session_id, complete_task=True, now=end)
    with pytest.raises(focus.FocusSessionEndedError):
        focus.stop_session(session_id, now=end)
    assert quest.get_player_stats() == before


def test_concurrent_finish_delivers_bonus_once() -> None:
    """同時終了で成功する操作と追加報酬は一回だけになる。"""
    _, session_id = _start()

    def finish(_: int) -> bool:
        """並列終了の勝者だけを数える。"""
        try:
            focus.finish_session(session_id, complete_task=True, now=START + timedelta(minutes=25))
            return True
        except focus.FocusSessionEndedError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(finish, range(2))) == 1
    with db.connection() as conn:
        assert (
            conn.execute("SELECT SUM(bonus_xp) FROM focus_sessions").fetchone()[0]
            == focus.FOCUS_BONUS_XP
        )
        assert conn.execute("SELECT COUNT(*) FROM quest_log").fetchone()[0] == 1


def test_completion_failure_rolls_back_session_and_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """通常報酬に失敗すると集中終了・タスク完了・報酬すべてが取り消される。"""
    task_id, session_id = _start()

    def fail(conn: sqlite3.Connection, task: Any) -> None:
        """通常報酬処理の失敗を再現する。"""
        raise RuntimeError("報酬エラー")

    monkeypatch.setattr(quest, "award_for_completion_in_transaction", fail)
    with pytest.raises(RuntimeError, match="報酬エラー"):
        focus.finish_session(session_id, complete_task=True, now=START + timedelta(minutes=25))
    assert focus.active_session() is not None
    assert quest.get_player_stats().xp_total == 0
    with db.connection() as conn:
        row = conn.execute(
            "SELECT status, completed_at FROM items WHERE id = ?", (task_id,)
        ).fetchone()
        assert tuple(row) == ("inbox", None)


def test_recurring_completion_uses_existing_path() -> None:
    """集中からの完了も繰り返しタスクの次回分を一度だけ生成する。"""
    task_id = db.create_recurring_task("毎日集中", due_date="2026-10-16", frequency="daily")
    session_id = focus.start_session(task_id, 15, now=START)
    result = focus.finish_session(session_id, complete_task=True, now=START + timedelta(minutes=15))
    assert result.next_task_id is not None
    with db.connection() as conn:
        assert (
            conn.execute(
                "SELECT due_date FROM items WHERE id = ?", (result.next_task_id,)
            ).fetchone()[0]
            == "2026-10-17"
        )


def test_deleting_task_ends_its_focus_session() -> None:
    """集中中のタスクを削除すると、集中は中断扱いで終わり、後から完了にできない。"""
    task_id, session_id = _start()
    db.delete_item(task_id)
    assert focus.active_session() is None
    with pytest.raises(ValueError):
        focus.finish_session(session_id, complete_task=True, now=START + timedelta(minutes=25))


def test_invalid_end_time_and_unknown_session() -> None:
    """未知のセッションや開始より前の終了を拒否して進行中記録を維持する。"""
    _, session_id = _start()
    with pytest.raises(ValueError):
        focus.finish_session(99999, complete_task=False, now=START)
    with pytest.raises(ValueError):
        focus.stop_session(session_id, now=START - timedelta(seconds=1))
    assert focus.active_session() is not None


def test_summary_counts_actual_time_and_consecutive_sessions() -> None:
    """完了・中断・進行中の実測時間を数え、当日直近の中断で連続数を区切る。"""
    assert focus.today_focus_summary(START.date(), now=START) == focus.FocusSummary(0, 0, 0)
    _, first = _start(15)
    focus.finish_session(first, complete_task=False, now=START + timedelta(minutes=15))
    _, second = _start(15, now=START + timedelta(minutes=20))
    focus.stop_session(second, now=START + timedelta(minutes=25))
    _, third = _start(15, now=START + timedelta(minutes=30))
    focus.finish_session(third, complete_task=False, now=START + timedelta(minutes=45))
    _start(25, now=START + timedelta(minutes=50))
    assert focus.today_focus_summary(
        START.date(), now=START + timedelta(minutes=55)
    ) == focus.FocusSummary(40, 2, 1)


def test_summary_splits_midnight_and_caps_idle_time() -> None:
    """日跨ぎは各日の秒数だけを集計し、放置しても予定時間を超えて数えない。"""
    late = datetime(2026, 10, 15, 23, 50)
    _, session_id = _start(25, now=late)
    focus.finish_session(session_id, complete_task=False, now=START)
    assert focus.today_focus_summary(date(2026, 10, 15), now=START) == focus.FocusSummary(10, 0, 0)
    assert focus.today_focus_summary(START.date(), now=START) == focus.FocusSummary(15, 1, 1)
    assert focus.today_focus_summary(date(2026, 10, 17), now=START).total_minutes == 0


def test_bonus_level_up_does_not_change_quest_count() -> None:
    """追加報酬でレベルアップできても通常タスクの完了件数は水増ししない。"""
    for offset in range(5):
        start = START + timedelta(minutes=30 * offset)
        _, session_id = _start(25, now=start)
        result = focus.finish_session(
            session_id, complete_task=False, now=start + timedelta(minutes=25)
        )
    assert result.bonus_reward is not None
    assert (result.bonus_reward.level_before, result.bonus_reward.level_after) == (1, 2)
    stats = quest.get_player_stats()
    assert stats.level == 2 and stats.completed_today == 0


def test_timer_payload_and_title_are_not_html(monkeypatch: pytest.MonkeyPatch) -> None:
    """タイマー設定はJSONをbase64で渡し、タイトルをHTMLへ埋め込まない。"""
    from symnote.views import focus_view

    documents: List[str] = []

    def capture(document: str, **kwargs: Any) -> None:
        """埋め込むHTMLを収集する。"""
        documents.append(document)

    monkeypatch.setattr(focus_view.components, "html", capture)
    task_id = db.insert_task('</script><script>alert("危険")</script>')
    focus.start_session(task_id, 25, now=START)
    session = focus.active_session()
    assert session is not None
    focus_view._render_timer(session)
    document = documents[0]
    assert session.title not in document and "innerHTML" not in document
    encoded = document.split("atob('", 1)[1].split("')", 1)[0]
    payload = json.loads(base64.b64decode(encoded))
    assert payload["plannedMinutes"] == 25
    assert datetime.fromisoformat(payload["startedAt"]).replace(tzinfo=None) == START
    assert "prefers-reduced-motion" in document and "forced-colors" in document


def test_focus_view_launch_and_stop() -> None:
    """ホームから集中を開始して専用画面に移り、中断後に通常画面へ戻る。"""
    from streamlit.testing.v1 import AppTest

    task_id = db.insert_task("一つに集中")
    app = AppTest.from_string(
        "from symnote.core.db import connection\n"
        "from symnote.views.focus_view import render_focus_launcher, render_focus_mode\n"
        "if not render_focus_mode():\n"
        "    with connection() as conn:\n"
        '        tasks = [dict(row) for row in conn.execute("SELECT * FROM items")]\n'
        "    render_focus_launcher(tasks)\n"
        "    import streamlit as st\n"
        "    st.text('通常画面')\n",
        default_timeout=10,
    ).run()
    assert not app.exception
    app.button[0].click().run()
    assert not app.exception
    session = focus.active_session()
    assert session is not None and session.task_id == task_id
    assert not any(text.value == "通常画面" for text in app.text)
    app.button(key=f"focus_stop_{session.id}").click().run()
    assert not app.exception and focus.active_session() is None
    assert any("ここまでの集中" in message.value for message in app.info)
    assert any(text.value == "通常画面" for text in app.text)


def test_bonus_celebration_is_shown_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """完了後の追加祝賀は一度だけ表示し、通常報酬は既存の未表示経路に残す。"""
    from streamlit.testing.v1 import AppTest

    from symnote.views import focus_view

    celebrations: List[quest.QuestReward] = []

    def capture(rewards: List[quest.QuestReward]) -> None:
        """フォーカスビューが渡す追加報酬だけを収集する。"""
        celebrations.extend(rewards)

    monkeypatch.setattr(focus_view, "render_celebration", capture)
    _, session_id = _start(now=datetime.now() - timedelta(minutes=25))
    app = AppTest.from_string(
        "from symnote.views.focus_view import render_focus_mode\nrender_focus_mode()",
        default_timeout=10,
    ).run()
    assert not app.exception
    app.button(key=f"focus_done_{session_id}").click().run()
    assert not app.exception
    assert len(celebrations) == 1 and celebrations[0].xp == focus.FOCUS_BONUS_XP
    assert len(quest.pop_unseen_rewards()) == 1
    app.run()
    assert not app.exception and len(celebrations) == 1
