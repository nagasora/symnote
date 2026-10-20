"""拡張した繰り返しの純粋計算、保存、自然文入力と UI を検証する。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Dict

import pytest
from streamlit.testing.v1 import AppTest

from symnote.core import db, delegation, focus
from symnote.core.nlp import parse_quick_recurrence
from symnote.core.recurrence import RecurrenceRule, first_occurrence, next_occurrence, rule_label


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """ユーザーの DB に接続せず、一時 DB を初期化する。"""
    path = tmp_path / "recurrence.db"
    monkeypatch.setenv("DB_PATH", str(path))
    db.init_db()
    return path


@pytest.mark.parametrize(
    "base,rule,expected",
    [
        ("2026-01-31", RecurrenceRule("monthly", month_day=31), "2026-02-28"),
        ("2026-02-28", RecurrenceRule("monthly", month_day=31), "2026-03-31"),
        ("2028-01-31", RecurrenceRule("monthly", month_day=31), "2028-02-29"),
        ("2026-12-31", RecurrenceRule("monthly", month_day=31), "2027-01-31"),
        ("2026-07-24", RecurrenceRule("weekdays"), "2026-07-27"),
        ("2026-07-25", RecurrenceRule("weekdays"), "2026-07-27"),
        ("2026-07-20", RecurrenceRule("weekly", 7, (0, 3)), "2026-07-23"),
        ("2026-07-23", RecurrenceRule("weekly", 7, (0, 3)), "2026-07-27"),
        ("2026-07-23", RecurrenceRule("weekly", 14, (0, 3)), "2026-08-03"),
        ("2026-07-20", RecurrenceRule("weekly", 14), "2026-08-03"),
        ("2026-07-20", RecurrenceRule("custom", 3), "2026-07-23"),
    ],
)
def test_next_occurrence(base: str, rule: RecurrenceRule, expected: str) -> None:
    """月末・閏年・年越し・週末・複数曜日・隔週の次回日を保証する。"""
    assert next_occurrence(dt.date.fromisoformat(base), rule).isoformat() == expected


@pytest.mark.parametrize(
    "rule,expected",
    [
        (RecurrenceRule("weekdays"), "2026-07-27"),
        (RecurrenceRule("weekly", 7, (0, 3)), "2026-07-27"),
        (RecurrenceRule("monthly", month_day=15), "2026-08-15"),
        (RecurrenceRule("daily"), "2026-07-25"),
    ],
)
def test_first_occurrence(rule: RecurrenceRule, expected: str) -> None:
    """最初の期限も指定曜日または月内日付に合わせる。"""
    assert first_occurrence(dt.date(2026, 7, 25), rule).isoformat() == expected


@pytest.mark.parametrize(
    "text,rule,remaining,label",
    [
        ("毎日 日報", RecurrenceRule("daily"), "日報", "毎日"),
        ("平日 運動", RecurrenceRule("weekdays"), "運動", "平日"),
        ("毎週月曜 会議", RecurrenceRule("weekly", 7, (0,)), "会議", "毎週 月"),
        ("毎週月曜日・木曜日 会議", RecurrenceRule("weekly", 7, (0, 3)), "会議", "毎週 月・木"),
        ("隔週 読書", RecurrenceRule("weekly", 14), "読書", "隔週"),
        ("毎月15日 請求書", RecurrenceRule("monthly", month_day=15), "請求書", "毎月15日"),
        ("３日ごと 掃除", RecurrenceRule("custom", 3), "掃除", "3日ごと"),
        ("毎週日曜日 買い物", RecurrenceRule("weekly", 7, (6,)), "買い物", "毎週 日"),
    ],
)
def test_local_parser(text: str, rule: RecurrenceRule, remaining: str, label: str) -> None:
    """日本語・全角数字を解析し、曜日という接尾辞を日曜と誤認しない。"""
    actual, rest = parse_quick_recurrence(text)
    assert actual == rule
    assert rest == remaining
    assert rule_label(actual) == label


@pytest.mark.parametrize("text", ["明日 会議", "毎月32日 請求書", "0日ごと 掃除"])
def test_non_recurrence_text_is_preserved(text: str) -> None:
    """繰り返しがない文や不正な表現を勝手に書き換えない。"""
    assert parse_quick_recurrence(text) == (None, text)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"frequency": "unknown"},
        {"frequency": "custom", "interval_days": 0},
        {"frequency": "weekly", "interval_days": 8},
        {"frequency": "weekly", "interval_days": 7, "weekdays": (7,)},
        {"frequency": "monthly", "month_day": 32},
    ],
)
def test_invalid_rules(kwargs: Dict[str, Any]) -> None:
    """不正な規則を保存前に拒否する。"""
    with pytest.raises(ValueError):
        RecurrenceRule(**kwargs)


def test_monthly_series_preserves_anchor_and_metadata(database: Path) -> None:
    """月末を丸めても指定31日と時刻・タイトルを維持し、終了日で止まる。"""
    first = db.create_recurring_task(
        "月末処理", "2026-01-31", "monthly", tags="経理", due_time="15:30", end_date="2026-03-31"
    )
    second = db.complete_task(first)
    third = db.complete_task(second)
    assert db.complete_task(third) is None
    tasks = db.fetch_tasks()
    assert {task["due_date"] for task in tasks} == {"2026-01-31", "2026-02-28", "2026-03-31"}
    assert all(task["due_time"] == "15:30" and task["tags"] == "経理" for task in tasks)
    assert all(task["recurrence_month_day"] == 31 for task in tasks)
    assert "停止中" in db.recurrence_label(tasks[0])
    assert db.complete_task(first) is None


def test_existing_task_can_configure_change_and_stop(database: Path) -> None:
    """期限なしの単発タスクを今日起点の繰り返しにし、変更・停止できる。"""
    task_id = db.insert_task("復習")
    db.set_task_recurrence(task_id, RecurrenceRule("daily"))
    task = db.fetch_tasks()[0]
    assert task["due_date"] == dt.date.today().isoformat()
    assert db.recurrence_label(task) == "毎日"
    rule_id = task["recurrence_rule_id"]
    db.set_task_recurrence(task_id, RecurrenceRule("custom", 3))
    assert db.recurrence_label(db.fetch_tasks()[0]) == "3日ごと"
    assert db.fetch_tasks()[0]["recurrence_rule_id"] == rule_id
    db.set_task_recurrence(task_id, None)
    assert db.complete_task(task_id) is None
    assert len(db.fetch_tasks()) == 1


def test_reopen_discards_next_occurrence_and_detaches_its_references(database: Path) -> None:
    """完了取消で次回分を削除した後、再利用 ID に古い集中や関連状態を効かせない。"""
    original = db.create_recurring_task("日次確認", "2026-10-20", "daily")
    next_task = db.complete_task(original)
    assert next_task is not None

    report = delegation.record_progress(next_task, None, "作業中", suggested_status="done")
    session = focus.start_session(next_task, 25)
    with db.connection() as conn, conn:
        quest_log = conn.execute(
            "INSERT INTO quest_log(task_id, title, xp, gold, completed_at) "
            "VALUES (?, '次回分', 10, 5, '2026-10-20T12:00:00')",
            (next_task,),
        ).lastrowid
        time_block = conn.execute(
            "INSERT INTO time_blocks "
            "(day, task_id, title, start_time, end_time, status, created_at) "
            "VALUES ('2999-01-01', ?, '次回分', '09:00', '09:25', 'planned', '2026-10-20T00:00:00')",
            (next_task,),
        ).lastrowid

    reopened = db.reopen_task(original, discard_next_task_id=next_task)
    assert reopened["status"] == "today"
    assert db.fetch_item(next_task) is None
    assert focus.active_session() is None

    with db.connection() as conn:
        assert tuple(
            conn.execute(
                "SELECT status, task_id FROM agent_reports WHERE id = ?", (report,)
            ).fetchone()
        ) == ("rejected", -next_task)
        assert tuple(
            conn.execute(
                "SELECT outcome, task_id FROM focus_sessions WHERE id = ?", (session,)
            ).fetchone()
        ) == ("stopped", -next_task)
        assert conn.execute(
            "SELECT task_id FROM quest_log WHERE id = ?", (quest_log,)
        ).fetchone()[0] == -quest_log
        assert conn.execute(
            "SELECT status FROM time_blocks WHERE id = ?", (time_block,)
        ).fetchone()[0] == "cancelled"

    replacement = db.insert_task("無関係な新規タスク")
    assert replacement == next_task
    with pytest.raises(focus.FocusSessionEndedError):
        focus.finish_session(session, complete_task=True)
    assert db.fetch_item(replacement)["status"] == "inbox"


def test_editor_completion_uses_new_rule_atomically(database: Path) -> None:
    """エディタで規則変更と完了を同時保存すると新規則から次回分を作る。"""
    task_id = db.insert_task("復習", due_date="2026-07-20")
    db.update_item_fields(
        task_id, status="done", recurrence_settings=(RecurrenceRule("custom", 3), None)
    )
    assert {task["due_date"] for task in db.fetch_tasks()} == {"2026-07-20", "2026-07-23"}


def test_invalid_end_date_rolls_back_editor_updates(database: Path) -> None:
    """終了日が不正な場合は内容も規則も部分保存しない。"""
    task_id = db.insert_task("元の内容", due_date="2026-07-20")
    with pytest.raises(ValueError):
        db.update_item_fields(
            task_id,
            raw_text="変更",
            status="done",
            recurrence_settings=(RecurrenceRule("daily"), "2026-07-19"),
        )
    task = db.fetch_tasks()[0]
    assert task["raw_text"] == "元の内容"
    assert task["status"] == "inbox"
    assert task["recurrence_rule_id"] is None


@pytest.mark.parametrize(
    "text,frequency",
    [
        ("毎日15時 日報", "daily"),
        ("平日 運動", "weekdays"),
        ("毎週月曜 会議", "weekly"),
        ("毎月15日 請求書", "monthly"),
        ("3日ごと 掃除", "custom"),
    ],
)
def test_quick_save_offline(
    database: Path, monkeypatch: pytest.MonkeyPatch, text: str, frequency: str
) -> None:
    """API が呼べない状態でもホームと同じ保存処理が繰り返しを登録する。"""
    from symnote.app import save_quick_recurring_task
    from symnote.core import llm

    def unavailable(*args: Any, **kwargs: Any) -> None:
        """オフライン時に AI を呼び出したテストを失敗させる。"""
        raise AssertionError("AI 呼び出しは禁止")

    monkeypatch.setattr(llm, "generate_json", unavailable)
    task_id, parsed, label = save_quick_recurring_task(text, use_ai=False)
    task = db.fetch_tasks()[0]
    assert task["id"] == task_id
    assert task["recurrence_frequency"] == frequency
    assert label
    assert task["due_date"] == parsed.due_date
    if frequency == "weekly":
        assert dt.date.fromisoformat(task["due_date"]).weekday() == 0
    if frequency == "monthly":
        assert dt.date.fromisoformat(task["due_date"]).day == 15
    if frequency == "daily":
        assert task["due_time"] == "15:00"


def test_task_form_and_editor_recurrence(database: Path) -> None:
    """追加フォームと既存エディタで曜日指定・変更・停止が動作する。"""
    app = AppTest.from_string(
        "from symnote.app import render_task_tab\nrender_task_tab()", default_timeout=15
    ).run()
    assert not app.exception
    app.selectbox(key="task_add_frequency").set_value("毎週")
    app.multiselect(key="task_add_weekdays").set_value(["月", "木"])
    next(area for area in app.text_area if area.label == "詳細").set_value("会議")
    next(button for button in app.button if button.label == "タスクを保存").click().run()
    assert not app.exception
    task = db.fetch_tasks()[0]
    assert task["recurrence_weekdays"] == "0,3"
    app.run()
    task_id = task["id"]
    app.selectbox(key=f"task_edit_{task_id}_frequency").set_value("毎月")
    app.number_input(key=f"task_edit_{task_id}_month_day").set_value(31)
    next(button for button in app.button if button.label == "更新").click().run()
    assert not app.exception
    assert db.fetch_tasks()[0]["recurrence_month_day"] == 31
    app.run()
    app.selectbox(key=f"task_edit_{task_id}_frequency").set_value("なし")
    next(button for button in app.button if button.label == "更新").click().run()
    assert not app.exception
    assert db.fetch_tasks()[0]["recurrence_active"] == 0


def test_home_quick_add_recurrence(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ホーム入力が繰り返しを保存し、取り消しで作成タスクを削除する。"""
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    app = AppTest.from_string(
        "from symnote.app import render_quick_add\nrender_quick_add()", default_timeout=15
    ).run()
    next(field for field in app.text_input if field.label == "クイック追加").set_value(
        "3日ごと 掃除"
    )
    next(button for button in app.button if button.label == "＋ 追加").click().run()
    assert not app.exception
    task = db.fetch_tasks()[0]
    assert task["recurrence_interval_days"] == 3
    app.button(key=f"undo_quick_{task['id']}").click().run()
    assert not db.fetch_tasks()
