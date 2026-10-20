"""クイック入力の読み取りと、AI による今日の計画の検証。"""

from __future__ import annotations

import datetime as dt
from typing import Any, List

import pytest

from symnote.core import llm, task_ai

TODAY = dt.date(2026, 10, 16)  # 金曜日


@pytest.mark.parametrize(
    ("text", "title", "due_date", "due_time"),
    [
        ("明日15時までに資料提出", "資料提出", "2026-10-17", "15:00"),
        ("あさって 午後3時半 歯医者", "歯医者", "2026-10-18", "15:30"),
        ("来週月曜に企画書レビュー", "企画書レビュー", "2026-10-19", None),
        ("水曜までに見積もり送付", "見積もり送付", "2026-10-21", None),
        ("10/20 請求書を送る", "請求書を送る", "2026-10-20", None),
        ("12月3日 忘年会の予約", "忘年会の予約", "2026-12-03", None),
        ("3日後に振り返り", "振り返り", "2026-10-19", None),
        ("18:30 ジム", "ジム", "2026-10-16", "18:30"),
        ("牛乳を買う", "牛乳を買う", None, None),
    ],
)
def test_rule_based_parser_extracts_dates_and_times(text, title, due_date, due_time) -> None:
    parsed = task_ai.parse_quick_task_rule_based(text, TODAY)
    assert (parsed.title, parsed.due_date, parsed.due_time) == (title, due_date, due_time)
    assert parsed.source == "rule"


def test_rule_based_parser_keeps_extra_lines_as_details_and_flags_urgency() -> None:
    parsed = task_ai.parse_quick_task_rule_based("至急 契約書チェック\n第3条を重点的に", TODAY)
    assert parsed.details == "第3条を重点的に"
    assert parsed.importance == 5


def test_rule_based_parser_rejects_empty_input() -> None:
    with pytest.raises(ValueError):
        task_ai.parse_quick_task_rule_based("   ", TODAY)


def test_ai_parse_is_validated_and_used() -> None:
    def fake(prompt: str, **_: Any) -> Any:
        assert "2026-10-16" in prompt
        return {"title": "資料を提出する", "details": "", "due_date": "2026-10-17", "due_time": "15:00", "importance": 4}

    parsed = task_ai.parse_quick_task("明日15時 資料", TODAY, generate_json=fake)
    assert parsed == task_ai.ParsedTask("資料を提出する", "", "2026-10-17", "15:00", 4, "ai")


def test_ai_parse_drops_invalid_fields() -> None:
    def fake(prompt: str, **_: Any) -> Any:
        return {"title": "ジム", "due_date": "来週", "due_time": "25:99", "importance": True}

    parsed = task_ai.parse_quick_task("ジム", TODAY, generate_json=fake)
    assert (parsed.due_date, parsed.due_time, parsed.importance) == (None, None, 3)


@pytest.mark.parametrize("failure", [llm.LLMUnavailableError("キーなし"), RuntimeError("network"), None])
def test_ai_parse_falls_back_to_rules(failure) -> None:
    def fake(prompt: str, **_: Any) -> Any:
        if failure is None:
            return ["not", "a", "dict"]
        raise failure

    parsed = task_ai.parse_quick_task("明日 掃除", TODAY, generate_json=fake)
    assert parsed.source == "rule"
    assert parsed.due_date == "2026-10-17"


def _tasks() -> List[dict]:
    return [
        {"id": 1, "kind": "task", "status": "inbox", "tags": "レポート", "due_date": "2026-10-15", "importance": 4},
        {"id": 2, "kind": "task", "status": "today", "tags": "メール返信", "due_date": "2026-10-16"},
        {"id": 3, "kind": "task", "status": "done", "tags": "完了済み", "due_date": "2026-10-16"},
        {"id": 4, "kind": "memo", "status": "inbox", "tags": "メモ"},
    ]


def test_plan_day_keeps_only_known_open_task_ids() -> None:
    def fake(prompt: str, **_: Any) -> Any:
        assert "完了済み" not in prompt and "メモ" not in prompt
        return {
            "summary": "期限切れから片付けましょう。",
            "items": [
                {"task_id": 1, "reason": "期限切れ", "start_time": "09:00"},
                {"task_id": 99, "reason": "存在しない"},
                {"task_id": 3, "reason": "完了済み"},
                {"task_id": 1, "reason": "重複"},
                {"task_id": 2, "reason": "今日期限", "start_time": "bad"},
            ],
        }

    plan = task_ai.plan_day(_tasks(), [{"start": "10:00", "end": "11:00", "summary": "会議"}], TODAY, generate_json=fake)
    assert plan.source == "ai"
    assert [(i.task_id, i.start_time) for i in plan.items] == [(1, "09:00"), (2, None)]
    assert plan.items[0].title == "レポート"


def test_plan_day_falls_back_when_ai_unavailable() -> None:
    def fake(prompt: str, **_: Any) -> Any:
        raise llm.LLMUnavailableError("API キーが未設定です")

    plan = task_ai.plan_day(_tasks(), today=TODAY, generate_json=fake)
    assert plan.source == "rule"
    assert plan.notice == "API キーが未設定です"
    assert {i.task_id for i in plan.items} == {1, 2}


def test_plan_day_with_no_open_tasks_does_not_call_ai() -> None:
    def fake(prompt: str, **_: Any) -> Any:
        raise AssertionError("AI を呼ばない")

    plan = task_ai.plan_day([_tasks()[2]], today=TODAY, generate_json=fake)
    assert plan.items == []


def test_breakdown_task_returns_clean_steps() -> None:
    steps = task_ai.breakdown_task(
        "発表準備", generate_json=lambda prompt, **_: {"steps": [" 構成を決める ", "", 3, "スライドを作る"]}
    )
    assert steps == ["構成を決める", "スライドを作る"]


def test_breakdown_task_raises_when_empty() -> None:
    with pytest.raises(llm.LLMResponseError):
        task_ai.breakdown_task("x", generate_json=lambda prompt, **_: {"steps": []})


@pytest.mark.parametrize(
    ("text", "title", "due_date", "due_time"),
    [
        ("2時間かけて資料を読む", "2時間かけて資料を読む", None, None),
        ("午前12時に提出", "提出", "2026-10-16", "00:00"),
        ("10/15 請求書を送る", "請求書を送る", "2027-10-15", None),
    ],
)
def test_rule_based_parser_avoids_durations_and_wraps_past_dates(text, title, due_date, due_time) -> None:
    parsed = task_ai.parse_quick_task_rule_based(text, TODAY)
    assert (parsed.title, parsed.due_date, parsed.due_time) == (title, due_date, due_time)


@pytest.mark.parametrize(
    "reply",
    [
        {"title": "資料を提出する", "due_date": "tomorrow", "due_time": None},
        {"title": "資料を提出する"},
        {"title": "資料を提出する", "due_date": None, "due_time": None},
    ],
)
def test_ai_parse_never_drops_explicit_dates(reply) -> None:
    parsed = task_ai.parse_quick_task("明日15時までに資料提出", TODAY, generate_json=lambda prompt, **_: reply)
    assert parsed.title == "資料を提出する"
    assert (parsed.due_date, parsed.due_time) == ("2026-10-17", "15:00")


@pytest.mark.parametrize("steps", ["資料作成", {"first": "資料作成"}, 3])
def test_breakdown_task_rejects_non_list_steps(steps) -> None:
    with pytest.raises(llm.LLMResponseError):
        task_ai.breakdown_task("x", generate_json=lambda prompt, **_: {"steps": steps})


def test_applying_plan_skips_tasks_completed_or_deleted_after_planning(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "plan.db"))
    from symnote.core import db
    from symnote.views import today_ai_view

    db.init_db()
    open_id = db.insert_task("開いている")
    done_id = db.insert_task("完了した")
    deleted_id = db.insert_task("消した")
    db.complete_task(done_id)
    db.delete_item(deleted_id)

    assert today_ai_view.apply_plan_to_today([done_id, open_id, deleted_id]) == 1
    statuses = {row["id"]: row["status"] for row in db.fetch_tasks(limit=50)}
    assert statuses[open_id] == "today"
    assert statuses[done_id] == "done"


def test_ai_title_that_drops_the_content_is_replaced_by_the_rule_title() -> None:
    parsed = task_ai.parse_quick_task(
        "今日18時までに 至急 見積書を送る",
        TODAY,
        generate_json=lambda prompt, **_: {"title": "至急対応する", "importance": 5},
    )
    assert parsed.title == "見積書を送る"
    assert parsed.importance == 5


def test_shared_suffix_alone_does_not_count_as_shared_content() -> None:
    parsed = task_ai.parse_quick_task(
        "レポートを提出する", TODAY, generate_json=lambda prompt, **_: {"title": "至急対応する"}
    )
    assert parsed.title == "レポートを提出する"
