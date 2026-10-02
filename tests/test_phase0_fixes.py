"""今日のトップ3・繰り返しタスク・DB パス解決の回帰テスト。"""

from __future__ import annotations

import datetime as dt
import sqlite3

from streamlit.testing.v1 import AppTest

from symnote.config import load_config
from symnote.core.db import (
    create_recurring_task,
    fetch_tasks,
    init_db,
    insert_task,
    search_items,
    update_item_fields,
)
from symnote.core.nlp import suggest_today_tasks


def test_fetch_tasks_rows_feed_today_top3_suggestions(monkeypatch, tmp_path) -> None:
    """fetch_tasks の結果をそのまま渡すと、今日のトップ3が空にならない。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "top3.db"))
    init_db()
    for index in range(4):
        insert_task(f"タスク{index}", due_date=dt.date.today().isoformat())

    tasks = fetch_tasks(statuses=["today", "week", "inbox"])

    assert all(task["kind"] == "task" for task in tasks)
    assert len(suggest_today_tasks(tasks, dt.date.today())) == 3


def test_marking_recurring_task_done_from_edit_creates_next_occurrence(
    monkeypatch, tmp_path
) -> None:
    """編集フォーム経由で status=done にしても、繰り返しの次回分が作られる。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "edit-done.db"))
    init_db()
    first_id = create_recurring_task("週次レポート", due_date="2026-10-05", frequency="weekly")

    update_item_fields(first_id, raw_text="週次レポート（更新）", status="done")

    tasks = {task["id"]: task for task in fetch_tasks(limit=10)}
    assert tasks[first_id]["status"] == "done"
    assert tasks[first_id]["raw_text"] == "週次レポート（更新）"
    next_tasks = [task for task in tasks.values() if task["id"] != first_id]
    assert [task["due_date"] for task in next_tasks] == ["2026-10-12"]
    assert next_tasks[0]["status"] == "inbox"


def test_marking_done_twice_does_not_duplicate_occurrences(monkeypatch, tmp_path) -> None:
    """完了済みタスクを再度 done で保存しても次回分は増えない。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "done-twice.db"))
    init_db()
    first_id = create_recurring_task("毎日の運動", due_date="2026-10-05", frequency="daily")

    update_item_fields(first_id, status="done")
    update_item_fields(first_id, status="done")

    assert len(fetch_tasks(limit=10)) == 2


def test_insert_task_links_recurrence_rule_in_the_same_row(monkeypatch, tmp_path) -> None:
    """insert_task に渡した繰り返しルールが登録時点で行に入っている。"""
    database = tmp_path / "atomic.db"
    monkeypatch.setenv("DB_PATH", str(database))
    init_db()
    with sqlite3.connect(database) as conn:
        rule_id = conn.execute(
            "INSERT INTO task_recurrence_rules (frequency, interval_days, created_at) "
            "VALUES ('daily', 1, '2026-10-01T00:00:00')"
        ).lastrowid

    task_id = insert_task("ルール付き", due_date="2026-10-05", recurrence_rule_id=rule_id)

    assert {task["id"]: task for task in fetch_tasks()}[task_id]["recurrence_rule_id"] == rule_id


def test_home_relative_db_path_is_expanded(monkeypatch, tmp_path) -> None:
    """DB_PATH の先頭の ~ はホームディレクトリに展開される。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DB_PATH", "~/SymNote/symnote.db")

    assert load_config().db_path == str(tmp_path / "SymNote" / "symnote.db")


def test_relative_db_path_is_kept_as_configured(monkeypatch) -> None:
    """~ 以外の相対パスは既存の挙動どおり変更しない。"""
    monkeypatch.setenv("DB_PATH", "./symnote.db")

    assert load_config().db_path == "./symnote.db"


def test_search_items_matches_all_terms_and_treats_wildcards_literally(
    monkeypatch, tmp_path
) -> None:
    """検索はすべての語を含む項目だけを返し、% や _ を文字として扱う。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "search.db"))
    init_db()
    hit = insert_task("100% 達成の見積書を送る", tags="見積")
    insert_task("見積書を送る")
    done = insert_task("100% 完了済み")
    update_item_fields(done, status="done")

    assert [row["id"] for row in search_items("100% 見積")] == [hit]
    assert search_items("_") == []
    assert {row["id"] for row in search_items("100%", include_done=True)} == {hit, done}


def test_task_form_creates_open_ended_recurrence(monkeypatch, tmp_path) -> None:
    """タスク画面で繰り返しを選び終了日を空欄にすると、無期限のルールが作られる。"""
    database = tmp_path / "form.db"
    monkeypatch.setenv("DB_PATH", str(database))
    app = AppTest.from_file("src/symnote/app.py", default_timeout=15)
    app.run()
    app.sidebar.radio[0].set_value("タスク").run()

    app.text_input[0].set_value("ゼミ準備")
    app.text_area[0].set_value("毎週のゼミ資料を確認する")
    app.selectbox[0].set_value("毎週")
    next(b for b in app.button if b.label == "タスクを保存").click().run()

    assert not app.exception
    with sqlite3.connect(database) as conn:
        rules = conn.execute("SELECT interval_days, end_date FROM task_recurrence_rules").fetchall()
    assert rules == [(7, None)]
