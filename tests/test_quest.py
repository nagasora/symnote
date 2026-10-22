"""一時DBでクエスト付与・継続・実績・交換と演出の安全性を保証する。"""

from __future__ import annotations

import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

with patch("dotenv.load_dotenv", return_value=False):
    from symnote.core import db, quest


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """すべての操作をテスト専用DBへ向ける。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "quest.db"))
    db.init_db()


def _award(task_id: int, completed: str = "2026-10-16T12:00:00", **fields: Any) -> Optional[quest.QuestReward]:
    """確定した日時と入力で付与APIを呼ぶ。"""
    task: Dict[str, Any] = {"id": task_id, "kind": "task", "raw_text": f"冒険{task_id}",
                            "importance": 1, "completed_at": completed}
    task.update(fields)
    with db.connection() as conn, conn:
        return quest.award_for_completion_in_transaction(conn, task)


@pytest.mark.parametrize("fields,xp,gold", [
    ({}, 10, 5),
    ({"importance": 5}, 18, 9),
    ({"effort": "medium"}, 15, 8),
    ({"effort": "long"}, 20, 10),
    ({"effort": "short"}, 10, 5),
    ({"estimate_minutes": 60, "effort": "long"}, 22, 11),
    ({"estimate_minutes": 10000}, 40, 20),
    ({"due_date": "2026-10-16"}, 13, 7),
    ({"due_date": "2026-10-17"}, 13, 7),
    ({"due_date": "2026-10-15"}, 10, 5),
    ({"due_date": "invalid"}, 10, 5),
    ({"effort": "long", "due_date": "2026-10-16"}, 26, 13),
])
def test_reward_calculation(fields: Dict[str, Any], xp: int, gold: int) -> None:
    """重要度・労力・推定時間・期限内加点は決定論的に計算される。"""
    reward = _award(1, **fields)
    assert reward is not None
    assert (reward.xp, reward.gold) == (xp, gold)
    if fields.get("due_date") in ("2026-10-16", "2026-10-17"):
        assert "期限内クリア +30%" in reward.bonuses


def test_award_is_idempotent_and_non_tasks_are_ignored() -> None:
    """同じタスクは一度だけ付与し、メモは付与対象外になる。"""
    assert _award(1) is not None
    assert _award(1) is None
    assert _award(2, kind="memo") is None
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM quest_log").fetchone()[0] == 1


def test_complete_task_records_timestamp_and_reward() -> None:
    """通常の完了APIから完了日時・報酬が保存され、再完了しても増えない。"""
    task_id = db.insert_item(kind="task", raw_text="実際のクエスト", importance=3)
    db.complete_task(task_id)
    db.complete_task(task_id)
    with db.connection() as conn:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (task_id,)).fetchone()
        log = conn.execute("SELECT * FROM quest_log WHERE task_id = ?", (task_id,)).fetchone()
        assert item["status"] == "done"
        assert datetime.fromisoformat(item["completed_at"])
        assert item["completed_at"] == log["completed_at"]
        assert log["xp"] == 14
        assert conn.execute("SELECT COUNT(*) FROM quest_log").fetchone()[0] == 1
    db.update_item_fields(task_id, status="inbox")
    db.complete_task(task_id)
    assert quest.get_player_stats().xp_total == 14


def test_complete_rolls_back_when_award_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """報酬処理に失敗すると完了更新も取り消される。"""
    task_id = db.insert_item(kind="task", raw_text="原子的な完了")

    def fail(conn: sqlite3.Connection, task: Any) -> None:
        """報酬書き込み後の例外を再現する。"""
        conn.execute("INSERT INTO quest_log(task_id,title,xp,gold,completed_at) VALUES (?, '失敗',10,5,'2026-10-16')", (task_id,))
        raise RuntimeError("報酬失敗")

    monkeypatch.setattr(quest, "award_for_completion_in_transaction", fail)
    with pytest.raises(RuntimeError, match="報酬失敗"):
        db.complete_task(task_id)
    with db.connection() as conn:
        row = conn.execute("SELECT status, completed_at FROM items WHERE id = ?", (task_id,)).fetchone()
        assert tuple(row) == ("inbox", None)
        assert conn.execute("SELECT COUNT(*) FROM quest_log").fetchone()[0] == 0


def test_recurring_tasks_award_each_occurrence() -> None:
    """繰り返しタスクは元タスクと次回分それぞれに一度ずつ付与される。"""
    task_id = db.create_recurring_task("毎日の冒険", due_date="2026-10-16", frequency="daily")
    next_id = db.complete_task(task_id)
    assert next_id is not None
    db.complete_task(next_id)
    db.complete_task(next_id)
    with db.connection() as conn:
        assert {row[0] for row in conn.execute("SELECT task_id FROM quest_log")} == {task_id, next_id}


@pytest.mark.parametrize(
    "offsets,expected,rest_used",
    [
        ([], 0, 0),
        ([0], 1, 0),
        ([1, 2, 3], 3, 0),
        ([0, 1, 2], 3, 0),
        ([2, 3], 3, 1),
        ([0, 2], 3, 1),
        ([0, 0, 1], 2, 0),
        ([1, 3], 3, 1),
        ([0, 1, 3], 4, 1),
        ([0, 3, 4], 1, 1),
        ([0, 1, 3, 5], 2, 1),
        ([0, 1, 3, 4, 5, 7], 8, 1),
    ],
)
def test_streak_including_weekly_rest(
    offsets: List[int], expected: int, rest_used: int
) -> None:
    """An isolated blank is covered once per ISO week; two blanks still stop it."""
    today = date(2026, 10, 16)
    for task_id, offset in enumerate(offsets, 1):
        _award(task_id, f"{today - timedelta(days=offset)}T12:00:00")
    stats = quest.get_player_stats(today)
    assert stats.streak_days == expected
    assert stats.completed_today == offsets.count(0)
    assert quest.streak_info(today) == quest.StreakInfo(expected, rest_used)


def test_quest_preview_is_read_only_and_uses_effort_rules() -> None:
    """Preview matches completion rewards and maps minutes/effort to one to three stars."""
    now = datetime(2026, 10, 16, 12)
    short = quest.quest_preview({"importance": 5, "estimate_minutes": 15}, now)
    medium = quest.quest_preview({"effort": "medium"}, now)
    boss = quest.quest_preview({"estimate_minutes": 61, "due_date": "2026-10-16"}, now)
    assert short == quest.QuestPreview(21, 11, 1, False)
    assert medium == quest.QuestPreview(15, 8, 2, False)
    assert boss == quest.QuestPreview(29, 15, 3, True)
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM quest_log").fetchone()[0] == 0


def test_reward_lookup_and_seen_claim_are_separate_and_atomic() -> None:
    """Reading leaves unseen intact and exactly one caller can claim the animation."""
    reward = _award(7, tags="Visible title", raw_text="Body\nMore")
    assert reward is not None and reward.title == "Visible title"
    assert quest.reward_for_task(7) == reward
    assert quest.reward_for_task(999) is None
    with db.connection() as conn:
        assert conn.execute("SELECT seen FROM quest_log WHERE task_id = 7").fetchone()[0] == 0
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(lambda _: quest.mark_reward_seen(7), range(2))) == 1
    assert quest.reward_for_task(7) == reward


def test_daily_clear_is_eligible_once_and_updates_player_totals() -> None:
    """The final due/today task grants one chest whose XP and gold join totals."""
    today = date.today()
    task_id = db.insert_item(
        kind="task", raw_text="Last quest", status="today", due_date=today.isoformat()
    )
    assert quest.award_daily_clear_if_eligible(today) is None
    db.complete_task(task_id)
    bonus = quest.award_daily_clear_if_eligible(today)
    assert bonus is not None
    assert (bonus.kind, bonus.ref, bonus.xp, bonus.gold) == (
        "daily_clear", today.isoformat(), 20, 10
    )
    assert quest.award_daily_clear_if_eligible(today) is None
    stats = quest.get_player_stats(today)
    assert (stats.xp_total, stats.gold) == (33, 17)


def test_daily_clear_requires_no_overdue_or_today_tasks() -> None:
    """A clear today does not open the chest while another eligible task remains."""
    today = date.today()
    _award(1, f"{today.isoformat()}T12:00:00")
    db.insert_item(
        kind="task",
        raw_text="Still waiting",
        status="inbox",
        due_date=(today - timedelta(days=1)).isoformat(),
    )
    assert quest.award_daily_clear_if_eligible(today) is None


def test_concurrent_daily_clear_is_awarded_once() -> None:
    """Concurrent final-clear checks cannot duplicate the chest."""
    today = date.today()
    _award(1, f"{today.isoformat()}T12:00:00")
    with ThreadPoolExecutor(max_workers=2) as executor:
        bonuses = list(executor.map(lambda _: quest.award_daily_clear_if_eligible(today), range(2)))
    assert sum(bonus is not None for bonus in bonuses) == 1


def test_triage_is_idempotent_and_capped_at_twenty_per_day() -> None:
    """Only twenty unique board decisions earn XP on one day."""
    now = datetime(2026, 10, 16, 12)
    assert quest.award_triage("candidate:1", now=now) is not None
    assert quest.award_triage("candidate:1", now=now) is None
    for item_id in range(2, 21):
        assert quest.award_triage(f"candidate:{item_id}", now=now) is not None
    assert quest.award_triage("candidate:21", now=now) is None
    stats = quest.get_player_stats(now.date())
    assert (stats.xp_total, stats.gold) == (40, 0)


def test_concurrent_triage_respects_daily_cap() -> None:
    """The read-count-insert sequence is serialized under concurrent decisions."""
    now = datetime(2026, 10, 16, 12)
    with ThreadPoolExecutor(max_workers=25) as executor:
        bonuses = list(
            executor.map(
                lambda item_id: quest.award_triage(f"report:{item_id}", now=now),
                range(25),
            )
        )
    assert sum(bonus is not None for bonus in bonuses) == 20


def test_quest_history_includes_zero_days_and_inferred_rest() -> None:
    """Footprints expose every requested date plus the isolated covered blank."""
    today = date(2026, 10, 16)
    _award(1, "2026-10-16T12:00:00")
    _award(2, "2026-10-14T12:00:00")
    history = quest.quest_history(4, today=today)
    assert history == [
        {"day": "2026-10-13", "count": 0, "rest_day": False},
        {"day": "2026-10-14", "count": 1, "rest_day": False},
        {"day": "2026-10-15", "count": 0, "rest_day": True},
        {"day": "2026-10-16", "count": 1, "rest_day": False},
    ]


def test_quest_history_keeps_first_weekly_rest_reserved_as_time_advances() -> None:
    """Later completions and later history windows cannot relocate a week's coupon."""
    _award(1, "2026-10-12T12:00:00")
    _award(2, "2026-10-14T12:00:00")
    _award(3, "2026-10-16T12:00:00")

    on_wednesday = {
        row["day"]: row for row in quest.quest_history(3, today=date(2026, 10, 14))
    }
    on_friday = {
        row["day"]: row for row in quest.quest_history(5, today=date(2026, 10, 16))
    }
    on_tuesday = {
        row["day"]: row for row in quest.quest_history(9, today=date(2026, 10, 20))
    }

    assert on_wednesday["2026-10-13"]["rest_day"] is True
    assert on_friday["2026-10-13"]["rest_day"] is True
    assert on_friday["2026-10-15"]["rest_day"] is False
    assert on_tuesday["2026-10-13"]["rest_day"] is True
    assert on_tuesday["2026-10-15"]["rest_day"] is False


@pytest.mark.parametrize("xp,expected", [(0, (1, 0, 50)), (49, (1, 49, 50)),
                                                  (50, (2, 0, 75)), (124, (2, 74, 75)), (125, (3, 0, 100))])
def test_level_thresholds(xp: int, expected: Tuple[int, int, int]) -> None:
    """累積経験値が境界に達した時だけレベルアップする。"""
    assert quest._level_progress(xp) == expected


def test_stats_gold_and_titles() -> None:
    """レベル称号と獲得額は保存履歴から算出される。"""
    for task_id in range(1, 6):
        _award(task_id)
    stats = quest.get_player_stats(date(2026, 10, 16))
    assert (stats.level, stats.xp_total, stats.xp_into_level, stats.gold) == (2, 50, 0, 25)
    assert quest._title_for_level(1) == "見習い冒険者"
    assert quest._title_for_level(10) == "熟練の旅人"
    assert quest._title_for_level(30) == "伝説の勇者"


def test_achievements_and_original_reward_metadata() -> None:
    """全実績が条件通り一度だけ解除され、完了時のレベルと実績を復元できる。"""
    assert all(item.unlocked_at is None for item in quest.list_achievements())
    first = _award(1, "2026-09-17T08:59:00", due_date="2026-09-17")
    assert first is not None and set(first.new_achievements) == {"first_quest", "early_bird"}
    for task_id in range(2, 31):
        day = date(2026, 9, 17) + timedelta(days=task_id - 1)
        _award(task_id, f"{day}T12:00:00", due_date=day.isoformat())
    for task_id in range(31, 101):
        _award(task_id, "2026-10-16T12:00:00")
    assert all(item.unlocked_at is not None for item in quest.list_achievements())
    rewards = quest.pop_unseen_rewards()
    assert rewards[0] == first
    assert "連続3日" in rewards[2].bonuses
    assert any(item.level_after > item.level_before for item in rewards)
    assert len(rewards) == 100
    assert quest.pop_unseen_rewards() == []
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM achievements").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM quest_log WHERE seen = 0").fetchone()[0] == 0


def test_early_bird_cutoff() -> None:
    """午前9時ちょうどの完了では早起き実績は解除されない。"""
    reward = _award(1, "2026-10-16T09:00:00")
    assert reward is not None and "early_bird" not in reward.new_achievements


def test_reward_shop_add_redeem_and_insufficient_balance() -> None:
    """ご褒美の追加と交換は残高および交換時点の情報を保存する。"""
    assert quest.list_rewards() == []
    reward_id = quest.add_reward(" スイーツ ", 5, "🍰")
    reward = quest.list_rewards()[0]
    assert (reward.id, reward.title, reward.cost, reward.icon) == (reward_id, "スイーツ", 5, "🍰")
    with pytest.raises(quest.InsufficientGoldError):
        quest.redeem_reward(reward_id)
    _award(1)
    assert quest.redeem_reward(reward_id) == 0
    assert quest.get_player_stats().gold == 0
    with pytest.raises(quest.InsufficientGoldError):
        quest.redeem_reward(reward_id)
    with db.connection() as conn:
        assert tuple(conn.execute("SELECT title,cost FROM reward_redemptions").fetchone()) == ("スイーツ", 5)
        assert conn.execute("SELECT COUNT(*) FROM reward_redemptions").fetchone()[0] == 1
    with pytest.raises(ValueError, match="見つかりません"):
        quest.redeem_reward(9999)


@pytest.mark.parametrize("title,cost", [("", 5), ("  ", 5), ("a", 0), ("a", -1), ("a", 1.5), ("a", True)])
def test_invalid_reward_is_rejected(title: str, cost: Any) -> None:
    """不正な名前や価格は保存前に拒否する。"""
    with pytest.raises(ValueError):
        quest.add_reward(title, cost)
    assert quest.list_rewards() == []


def test_concurrent_redemptions_do_not_overspend() -> None:
    """同時交換でも残高以上にご褒美を交換できない。"""
    _award(1)
    reward_id = quest.add_reward("休憩", 5)

    def redeem(_: int) -> bool:
        """交換成功の可否を収集する。"""
        try:
            quest.redeem_reward(reward_id)
            return True
        except quest.InsufficientGoldError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(redeem, range(2))) == 1
    assert quest.get_player_stats().gold == 0


def test_concurrent_pop_delivers_once() -> None:
    """同時に未表示報酬を取得しても演出は一回分だけ返す。"""
    _award(1)

    def pop(_: int) -> int:
        """取得件数を収集する。"""
        return len(quest.pop_unseen_rewards())

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(pop, range(2))) == 1


def test_celebration_encodes_untrusted_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """演出は任意のタスク名をHTMLへ直接展開せず、複数報酬を合算する。"""
    from symnote.views import quest_view

    documents: List[str] = []

    def capture(document: str, **kwargs: Any) -> None:
        """生成した埋め込みHTMLを収集する。"""
        documents.append(document)
        assert kwargs["height"] == 300

    monkeypatch.setattr(quest_view.components, "html", capture)
    title = '</script><script>alert("危険")</script>'
    reward = quest.QuestReward(1, title, 10, 5, [], 1, 2, ["first_quest"])
    quest_view.render_celebration([], sound=False)
    assert documents == []
    quest_view.render_celebration([reward, reward], sound=False)
    document = documents[0]
    assert title not in document
    assert "innerHTML" not in document
    encoded = document.split("atob('", 1)[1].split("')", 1)[0]
    payload = json.loads(base64.b64decode(encoded))
    assert payload["titles"] == [title, title]
    assert (payload["xp"], payload["gold"], payload["sound"]) == (20, 10, False)
    assert len(payload["achievements"]) == 1
    assert "prefers-reduced-motion" in document


def test_sound_setting_is_persistent_and_off_by_default() -> None:
    """効果音設定は初期状態で無音になり、変更はローカルDBに保存される。"""
    from symnote.views import quest_view

    assert quest_view._sound_enabled() is False
    quest_view._save_sound(True)
    assert quest_view._sound_enabled() is True
    quest_view._save_sound(False)
    assert quest_view._sound_enabled() is False


def test_rewards_page_does_not_insert_suggestions_automatically() -> None:
    """初回画面表示だけではおすすめを挿入せず、ボタン操作で一件だけ登録する。"""
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        "from symnote.views.quest_view import render_rewards_page\nrender_rewards_page()",
        default_timeout=10,
    ).run()
    assert not app.exception
    assert quest.list_rewards() == []
    app.button(key="quest_suggest_100").click().run()
    assert not app.exception
    assert [reward.title for reward in quest.list_rewards()] == ["コンビニスイーツ"]
    app.toggle(key="quest_sound_toggle").set_value(True).run()
    assert not app.exception
    from symnote.views import quest_view

    assert quest_view._sound_enabled() is True


def test_rewards_page_exchange_updates_balance() -> None:
    """交換画面は不足時に案内し、成功時には最新残高と通知を表示する。"""
    from streamlit.testing.v1 import AppTest

    reward_id = quest.add_reward("好きなお茶", 5)
    app = AppTest.from_string(
        "from symnote.views.quest_view import render_rewards_page\nrender_rewards_page()",
        default_timeout=10,
    ).run()
    app.button(key=f"quest_redeem_{reward_id}").click().run()
    assert not app.exception and len(app.warning) == 1
    _award(1)
    app.button(key=f"quest_redeem_{reward_id}").click().run()
    assert not app.exception
    assert quest.get_player_stats().gold == 0
    assert any("交換しました" in message.value for message in app.success)


def test_reused_task_id_after_delete_still_earns_rewards(monkeypatch, tmp_path) -> None:
    """削除で再利用された ID の新しいタスクも報酬を得られ、過去の獲得分は残る。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "reuse.db"))
    from symnote.core import db, quest

    db.init_db()
    first = db.insert_task("最初")
    db.complete_task(first)
    db.delete_item(first)
    second = db.insert_task("二番目")
    assert second == first
    db.complete_task(second)
    with db.connection() as conn:
        rows = conn.execute("SELECT task_id, title FROM quest_log ORDER BY id").fetchall()
    assert [row["title"] for row in rows] == ["最初", "二番目"]
    assert rows[0]["task_id"] < 0 and rows[1]["task_id"] == second
    assert quest.get_player_stats().xp_total > 0


def test_quest_title_uses_task_title_not_whole_body(tmp_path, monkeypatch) -> None:
    """クエスト履歴の題名はタイトル（なければ本文 1 行目）で、追記された本文は含まない。"""
    from symnote.core import db, quest

    monkeypatch.setenv("DB_PATH", str(tmp_path / "q.db"))
    db.init_db()
    titled = db.insert_item(kind="task", raw_text="本文\n\n[進捗] メモ", tags="請求書チェック")
    untitled = db.insert_item(kind="task", raw_text="一行目\n二行目")
    db.complete_task(titled)
    db.complete_task(untitled)
    titles = {reward.task_id: reward.title for reward in quest.pop_unseen_rewards()}
    assert titles == {titled: "請求書チェック", untitled: "一行目"}
