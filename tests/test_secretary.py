"""秘書がオフラインでも安全に優先順位・入力整理・確認操作を扱うことを検証する。"""

from __future__ import annotations

import datetime as dt
import json
import random
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from symnote.core import db, secretary as sec

NOW = dt.datetime(2026, 10, 16, 9, 0)


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ユーザー所有の DB に接続せず、一時的な v14 DB を作る。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "secretary.db"))
    db.init_db()


@pytest.fixture
def tasks() -> List[Dict[str, Any]]:
    """期限・状態・重要度が競合するタスクを用意する。"""
    return [
        {"id": 1, "tags": "レポート", "due_date": "2026-10-15", "importance": 1, "status": "inbox"},
        {"id": 2, "tags": "返信", "due_date": "2026-10-16", "importance": 5, "status": "inbox"},
        {"id": 3, "tags": "片付け", "status": "today", "importance": 2},
        {"id": 4, "tags": "調べる", "status": "week", "importance": 5},
        {"id": 5, "tags": "完了済み", "status": "done", "due_date": "2026-01-01"},
        {"id": 6, "tags": "メモ", "kind": "memo", "due_date": "2026-01-01"},
    ]


def _fail(*args: Any, **kwargs: Any) -> Any:
    """ネットワーク障害を再現する。"""
    raise RuntimeError("offline")


def _returns(value: Any) -> Callable[..., Any]:
    """任意のモデル応答を返す生成器を作る。"""

    def generate(prompt: str = "", **kwargs: Any) -> Any:
        """指定の応答を返す。"""
        return value

    return generate


@pytest.mark.parametrize(("removed", "expected"), [([], 1), ([1], 2), ([1, 2], 3), ([1, 2, 3], 4)])
def test_focus_priority(tasks: List[Dict[str, Any]], removed: List[int], expected: int) -> None:
    """期限切れ、今日期限、今日指定、重要度の順で選ぶ。"""
    action = sec.next_action([task for task in tasks if task["id"] not in removed], NOW)
    assert action is not None and action.task_id == expected and action.reason


def test_focus_short_task_fits_gap() -> None:
    """同じ期限区分では、空き時間に入る作業を優先する。"""
    tasks = [
        {"id": 1, "importance": 5, "estimated_minutes": 60},
        {"id": 2, "importance": 2, "estimated_minutes": 10},
    ]
    assert sec.next_action(tasks, NOW, free_minutes=15).task_id == 2
    assert sec.next_action(tasks, NOW).task_id == 1
    assert sec.next_action([], NOW) is None


def test_focus_timed_deadline_is_overdue() -> None:
    """今日でも時刻を過ぎた締切は期限切れとして扱う。"""
    tasks = [
        {"id": 1, "due_date": "2026-10-16", "due_time": "08:00", "importance": 1},
        {"id": 2, "due_date": "2026-10-16", "due_time": "15:00", "importance": 5},
    ]
    assert sec.next_action(tasks, NOW).task_id == 1
    assert "期限を過ぎ" in sec.next_action(tasks, NOW).reason


@pytest.mark.parametrize(
    ("hour", "period"),
    [
        (0, "evening"),
        (4, "evening"),
        (5, "morning"),
        (11, "morning"),
        (12, "day"),
        (17, "day"),
        (18, "evening"),
    ],
)
def test_briefing_period(hour: int, period: str) -> None:
    """朝昼夜の境界が明確である。"""
    now = NOW.replace(hour=hour)
    briefing = sec.compose_briefing([], [], now, use_ai=False)
    assert briefing.period == period
    assert briefing.cache_key == f"2026-10-16:{period}"


def test_briefing_fallback_keeps_schedule_top_three_and_overdue(
    tasks: List[Dict[str, Any]],
) -> None:
    """AI 障害でも朝の予定・上位3件・期限切れ件数が得られる。"""
    briefing = sec.compose_briefing(
        tasks, [{"start": "10:00", "summary": "会議"}], NOW, generate_text=_fail
    )
    assert briefing.source == "rule" and briefing.expression == "worried"
    assert all(
        word in briefing.text
        for word in ("10:00", "会議", "レポート", "返信", "片付け", "期限切れは1件")
    )
    assert "調べる" not in briefing.text


def test_evening_briefing_reports_shutdown(tasks: List[Dict[str, Any]]) -> None:
    """夕方は完了・持ち越し・翌朝の一手を振り返る。"""
    briefing = sec.compose_briefing(
        tasks, [], NOW.replace(hour=20), completed_today=2, use_ai=False
    )
    assert all(
        word in briefing.text
        for word in ("完了は2件", "持ち越しは3件", "未完了全体は4件", "明日の最初の一手", "返信")
    )
    assert sec.compose_briefing([], [], NOW, completed_today=1, use_ai=False).expression == "happy"


@pytest.mark.parametrize("response", [None, {}, " "])
def test_briefing_rejects_empty_ai(response: Any) -> None:
    """空や非文字列の生成結果を表示に採用しない。"""
    assert sec.compose_briefing([], [], NOW, generate_text=_returns(response)).source == "rule"


def test_briefing_ai_receives_persona() -> None:
    """設定したキャラと事実を生成器へ渡す。"""

    def generate(prompt: str, **kwargs: Any) -> str:
        """キャラ設定がプロンプトに含まれることを検証する。"""
        assert "執事" in prompt and "ご主人様" in prompt and "会議" in prompt
        return "ご主人様、本日もお手伝いいたします。"

    briefing = sec.compose_briefing(
        [],
        [{"summary": "会議"}],
        NOW,
        settings=sec.SecretarySettings(tone="執事", user_name="ご主人様"),
        generate_text=generate,
    )
    assert briefing.source == "ai" and "ご主人様" in briefing.text


def test_brain_dump_validates_external_fields() -> None:
    """不正ゴール・不正日時・真偽値の数値を除外する。"""
    data = {
        "tasks": [
            {
                "title": " 資料を書く ",
                "due_date": "2026-10-17",
                "due_time": "10:00",
                "importance": 4,
                "estimated_minutes": 15,
                "goal_id": 2,
            },
            {
                "title": "散歩",
                "due_date": "明日",
                "due_time": "99:00",
                "importance": True,
                "estimated_minutes": "20",
                "goal_id": 99,
            },
            {"title": "掃除", "goal_id": True},
            {"title": ""},
            "bad",
        ]
    }
    result = sec.split_brain_dump(
        "入力", NOW.date(), goals=[{"id": 2, "title": "出版"}], generate_json=_returns(data)
    )
    assert len(result) == 3
    assert result[0] == sec.DumpTask("資料を書く", "2026-10-17", "10:00", 4, 15, 2)
    assert result[1] == sec.DumpTask("散歩")
    assert result[2].goal_id is None


@pytest.mark.parametrize("data", [None, [], {"tasks": None}, {"tasks": []}, {"tasks": [None]}])
def test_brain_dump_bad_response_falls_back(data: Any) -> None:
    """不正応答でも行と句点で分割して日時を抽出する。"""
    result = sec.split_brain_dump(
        "明日10時 資料提出。掃除\n買い物", NOW.date(), goals=[], generate_json=_returns(data)
    )
    assert len(result) == 3
    assert result[0].due_date == "2026-10-17" and result[0].due_time == "10:00"
    assert result[1].title == "掃除" and result[2].title == "買い物"


def test_brain_dump_offline_and_empty() -> None:
    """通信不可と空入力でも安全に結果を返す。"""
    assert (
        len(sec.split_brain_dump("明日 掃除。返信", NOW.date(), goals=[], generate_json=_fail)) == 2
    )
    assert sec.split_brain_dump("  ", use_ai=False) == []


@pytest.mark.parametrize(
    "intent", ["complete_task", "reschedule", "add_task", "ask_plan", "brain_dump", "chat"]
)
def test_command_intents_and_confirmation(tasks: List[Dict[str, Any]], intent: str) -> None:
    """AI が確認不要と出力しても、変更操作には確認を要求する。"""
    data = {
        "intent": intent,
        "parameters": {"task_id": 1, "title": "歯医者", "due_date": "2026-10-17"},
        "requires_confirmation": False,
    }
    command = sec.interpret_command(
        "入力", {"tasks": tasks, "now": NOW}, generate_json=_returns(data)
    )
    assert command.intent == intent
    assert command.requires_confirmation == (intent in ("complete_task", "reschedule", "add_task"))


@pytest.mark.parametrize("task_id", [999, True, "1", 5, 6])
def test_command_rejects_unknown_closed_and_non_task_ids(
    tasks: List[Dict[str, Any]], task_id: Any
) -> None:
    """未存在・完了済み・メモ・型違いのIDを操作対象にしない。"""
    command = sec.interpret_command(
        "完了",
        {"tasks": tasks},
        generate_json=_returns({"intent": "complete_task", "parameters": {"task_id": task_id}}),
    )
    assert command.intent == "chat" and command.parameters == {}


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("レポート終わった", "complete_task"),
        ("レポートを来週に回して", "reschedule"),
        ("明日10時に歯医者入れて", "add_task"),
        ("今日何すればいい？", "ask_plan"),
        ("何やる", "ask_plan"),
        ("掃除\n買い物", "brain_dump"),
        ("こんにちは", "chat"),
    ],
)
def test_command_offline_rules(tasks: List[Dict[str, Any]], text: str, intent: str) -> None:
    """ネットワークなしでも基本的な意図を読み取る。"""
    command = sec.interpret_command(text, {"tasks": tasks, "now": NOW}, generate_json=_fail)
    assert command.intent == intent
    if intent == "reschedule":
        assert command.parameters["due_date"] == "2026-10-23"
    if intent == "add_task":
        assert (
            command.parameters["due_time"] == "10:00"
            and command.parameters["due_date"] == "2026-10-17"
        )


def test_ambiguous_rule_command_does_not_modify(tasks: List[Dict[str, Any]]) -> None:
    """複数の候補を含む完了指示は自動で対象を決めない。"""
    command = sec.interpret_command("レポートと返信が完了", {"tasks": tasks}, use_ai=False)
    assert command.intent == "chat"


def test_context_only_includes_recent_twenty(tasks: List[Dict[str, Any]]) -> None:
    """対話のコンテキストは直近20件だけを渡す。"""

    def generate(prompt: str = "", **kwargs: Any) -> Any:
        """プロンプト内の履歴上限を確認する。"""
        payload = json.loads(prompt.split("\n", 1)[1])
        assert len(payload["messages"]) == 20
        assert payload["messages"][0]["content"] == "5"
        return {"intent": "chat", "reply": "了解です。"}

    sec.interpret_command(
        "こんにちは",
        {"tasks": tasks, "messages": [{"content": str(i)} for i in range(25)]},
        generate_json=generate,
    )


def test_settings_roundtrip_and_namespace(database: None) -> None:
    """設定を永続化し、他の機能の設定を保持する。"""
    assert sec.load_settings() == sec.SecretarySettings()
    with db.connection() as conn, conn:
        conn.execute("INSERT INTO app_settings VALUES ('other', 'keep')")
    expected = sec.SecretarySettings("ソラ", "クール", "そらさん", True)
    sec.save_settings(expected)
    assert sec.load_settings() == expected
    with db.connection() as conn:
        assert (
            conn.execute("SELECT value FROM app_settings WHERE key='other'").fetchone()[0] == "keep"
        )


@pytest.mark.parametrize(
    "value", ["not json", "[]", '{"name": 3, "tone": "bad", "sound_enabled": "true"}']
)
def test_settings_invalid_data_uses_defaults(database: None, value: str) -> None:
    """破損した設定の値で秘書画面が停止しない。"""
    with db.connection() as conn, conn:
        conn.execute("INSERT INTO app_settings VALUES ('secretary.settings', ?)", (value,))
    assert sec.load_settings() == sec.SecretarySettings()


def test_messages_persist_order_and_limit(database: None) -> None:
    """会話を保存順に返し、長い履歴を20件へ制限する。"""
    for index in range(24):
        sec.save_message("user" if index % 2 else "secretary", f"メッセージ{index}", NOW)
    rows = sec.recent_messages(100)
    assert (
        len(rows) == 20
        and rows[0]["content"] == "メッセージ4"
        and rows[-1]["content"] == "メッセージ23"
    )
    assert sec.recent_messages(0) == []
    with pytest.raises(ValueError):
        sec.save_message("system", "指示")


def test_execution_requires_confirmation_and_refreshes_target(database: None) -> None:
    """変更は確認必須で、削除・完了済みタスクを再操作しない。"""
    task_id = db.insert_task("レポート")
    command = sec.Command("complete_task", {"task_id": task_id}, False)
    with pytest.raises(ValueError, match="確認"):
        sec.execute_command(command)
    assert db.fetch_tasks()[0]["status"] == "inbox"
    sec.execute_command(command, confirmed=True)
    assert db.fetch_tasks()[0]["status"] == "done"
    with pytest.raises(ValueError):
        sec.execute_command(command, confirmed=True)
    with pytest.raises(ValueError):
        sec.execute_command(sec.Command("complete_task", {"task_id": 999}), confirmed=True)


def test_execution_add_reschedule_and_briefing(database: None) -> None:
    """追加と延期を保存し、現在のDBから朝の説明を構築できる。"""
    task_id = sec.execute_command(
        sec.Command("add_task", {"title": "歯医者", "due_date": "2026-10-16"}), confirmed=True
    )
    sec.execute_command(
        sec.Command("reschedule", {"task_id": task_id, "due_date": "2026-10-17"}), confirmed=True
    )
    task = db.fetch_tasks()[0]
    assert task["due_date"] == "2026-10-17" and task["status"] == "week"
    briefing = sec.build_briefing(NOW, events=[], use_ai=False)
    assert "歯医者" in briefing.text


@pytest.mark.parametrize(
    "expression", ["normal", "happy", "worried", "cheer", "thinking", '<script>alert("x")</script>']
)
def test_avatar_never_embeds_user_input(expression: str) -> None:
    """アバターは正しいSVGで、ユーザー文字列やスクリプトを含まない。"""
    svg = sec.avatar_svg(expression)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") and "<script>" not in svg
    if expression.startswith("<"):
        assert svg == sec.avatar_svg("normal")
    assert (
        len({sec.avatar_svg(key) for key in ("normal", "happy", "worried", "cheer", "thinking")})
        >= 4
    )


def test_reactions_are_reproducible() -> None:
    """乱数 seed と注入乱数器のどちらでも反応を再現できる。"""
    assert sec.reaction_for("complete_task", seed=5) == sec.reaction_for("complete_task", seed=5)
    assert sec.reaction_for("level_up", rng=random.Random(2)) == sec.reaction_for(
        "level_up", seed=2
    )


def test_stale_goal_alert_with_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    """放置ゴールを次の一手付きで表示する。"""
    module = types.ModuleType("symnote.core.goals")

    def stale_goals(days: int) -> List[Dict[str, Any]]:
        """他エージェントが担当するAPIのフェイク。"""
        assert days == 3
        return [{"title": "出版", "stale_days": 4, "next_task": {"tags": "目次を書く"}}]

    module.stale_goals = stale_goals
    monkeypatch.setitem(sys.modules, "symnote.core.goals", module)
    assert sec.stale_alerts() == ["出版が4日止まっています。次の一手は目次を書くです。"]


def test_goal_assignment_uses_public_api(database: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """確認したゴールだけを公開APIでタスクに関連付ける。"""
    module = types.ModuleType("symnote.core.goals")
    assigned: List[Any] = []

    def list_goals_with_progress() -> List[Dict[str, Any]]:
        """選択可能なゴールを返す。"""
        return [{"id": 7, "title": "出版", "status": "active"}]

    def set_task_goal(task_id: int, goal_id: int) -> None:
        """関連付け呼び出しを記録する。"""
        assigned.append((task_id, goal_id))

    module.list_goals_with_progress = list_goals_with_progress
    module.set_task_goal = set_task_goal
    monkeypatch.setitem(sys.modules, "symnote.core.goals", module)
    task_id = sec.save_dump_task(sec.DumpTask("目次", goal_id=7, estimated_minutes=10))
    sec.save_dump_task(sec.DumpTask("無効", goal_id=999))
    assert assigned == [(task_id, 7)]
    assert any(task["effort"] == "short" for task in db.fetch_tasks())


def test_breakdown_offline_has_small_steps() -> None:
    """AI が使えなくても元タスクに着手できる小さな候補を返す。"""
    tasks = sec.breakdown_focus("レポート", generate_json=_fail)
    assert len(tasks) == 3
    assert all("レポート" in task.title and task.estimated_minutes <= 15 for task in tasks)


def test_goal_failure_exposes_saved_task(database: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """紐付けの部分失敗でも、保存済みIDを失わず再登録を防止できる。"""
    module = types.ModuleType("symnote.core.goals")
    module.list_goals_with_progress = _returns([{"id": 7, "title": "出版"}])
    module.set_task_goal = _fail
    monkeypatch.setitem(sys.modules, "symnote.core.goals", module)
    with pytest.raises(sec.GoalAssignmentError) as error:
        sec.save_dump_task(sec.DumpTask("目次", goal_id=7))
    assert [task["id"] for task in db.fetch_tasks()] == [error.value.task_id]


def _panel(monkeypatch: pytest.MonkeyPatch, source: str = "render_secretary_panel") -> Any:
    """外部通信をフェイクに置き換え、Streamlitの実部品をテストする。"""
    from streamlit.testing.v1 import AppTest
    from symnote.views import today_ai_view

    monkeypatch.setattr(sec, "_text_generate", _fail)
    monkeypatch.setattr(sec, "_json_generate", _fail)
    monkeypatch.setattr(today_ai_view, "today_events_for_plan", _returns([]))
    return AppTest.from_string(
        f"from symnote.views.secretary_view import {source}\n{source}()"
    ).run(timeout=15)


def test_panel_completion_requires_visible_confirmation(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """カードからも確認前に完了せず、確認後はDBを更新できる。"""
    task_id = db.insert_task("レポート")
    app = _panel(monkeypatch)
    assert not app.exception and not app.warning
    app.button(key=f"secretary_complete_{task_id}").click().run()
    assert db.fetch_tasks()[0]["status"] == "inbox"
    assert any("レポート" in info.value for info in app.info)
    app.button(key="secretary_confirm").click().run()
    assert db.fetch_tasks()[0]["status"] == "done"
    assert not app.exception and app.success


def test_brain_dump_ui_checks_before_saving(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """候補作成では保存せず、チェックしたタスクだけを追加する。"""
    app = _panel(monkeypatch, "render_brain_dump")
    app.text_area[0].input("明日 掃除。返信")
    app.button[0].click().run()
    assert not db.fetch_tasks() and len(app.checkbox) == 2
    app.checkbox[1].uncheck().run()
    app.button(key="secretary_dump_save").click().run()
    assert len(db.fetch_tasks()) == 1
    assert db.fetch_tasks()[0]["tags"] == "掃除"
    assert not app.exception


def test_settings_ui_saves_persona(database: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """設定フォームから名前・口調・呼び方・音を保存できる。"""
    app = _panel(monkeypatch, "render_secretary_settings")
    app.text_input[0].input("ソラ")
    app.text_input[1].input("先生")
    app.selectbox[0].select("執事")
    app.checkbox[0].check()
    app.button[0].click().run()
    assert sec.load_settings() == sec.SecretarySettings("ソラ", "執事", "先生", True)
    assert not app.exception


def test_available_minutes_uses_nearest_future_event() -> None:
    """過去・終日・不正な予定を除き、最も近い次の予定までを空き時間にする。"""
    events = [
        {"start": "08:00"},
        {"start": "09:15"},
        {"start": "10:00"},
        {"start": "終日"},
        {"start": "bad"},
    ]
    assert sec.available_minutes(events, NOW) == 15
    assert sec.available_minutes([{"start": "2026-10-16T09:10:00"}], NOW) == 10
    assert sec.available_minutes([], NOW) is None


def test_rescheduling_preserves_unspecified_time(database: None) -> None:
    """日付だけの延期では、既存の時刻を勝手に削除しない。"""
    task_id = db.insert_item(
        kind="task", raw_text="歯医者", due_date="2026-10-16", due_time="10:00"
    )
    sec.execute_command(
        sec.Command("reschedule", {"task_id": task_id, "due_date": "2026-10-17"}), confirmed=True
    )
    assert db.fetch_tasks()[0]["due_time"] == "10:00"


def test_minutes_prefer_the_database_estimate_column() -> None:
    """DB の estimate_minutes を所要時間の区分より優先する。"""
    assert sec._minutes({"estimate_minutes": 120, "effort": "short"}) == 120
