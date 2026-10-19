"""削除したタスク・ゴールの ID が再利用されても、古い報告や集中が新しい項目に効かないことを保証する。"""

from __future__ import annotations

from pathlib import Path

import pytest

from symnote.core import db, delegation, focus, goals


@pytest.fixture()
def database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """ユーザーの状態と分離した DB を初期化する。"""
    path = tmp_path / "detach.db"
    monkeypatch.setenv("DB_PATH", str(path))
    db.init_db()
    return path


def _one(sql: str, *params: object) -> dict:
    with db.connection() as conn:
        return dict(conn.execute(sql, params).fetchone())


def test_deleted_task_report_is_rejected_and_not_applied_to_reused_id(database: Path) -> None:
    """削除したタスクへの承認待ち報告は却下され、同じ ID の新タスクを完了させない。"""
    old = db.insert_item(kind="task", raw_text="古いタスク")
    report = delegation.record_progress(old, None, "終わりました", suggested_status="done")
    db.delete_item(old)
    new = db.insert_item(kind="task", raw_text="新しいタスク")
    assert new == old

    row = _one("SELECT status, task_id FROM agent_reports WHERE id = ?", report)
    assert row["status"] == "rejected"
    assert row["task_id"] == -old
    assert delegation.approve_report(report) is False
    assert _one("SELECT status, raw_text FROM items WHERE id = ?", new) == {
        "status": "inbox",
        "raw_text": "新しいタスク",
    }


def test_deleted_task_focus_session_ends_and_cannot_complete_reused_id(database: Path) -> None:
    """削除したタスクの集中セッションは中断扱いで終わり、新タスクの完了に使われない。"""
    old = db.insert_item(kind="task", raw_text="集中対象")
    focus.start_session(old, 25)
    db.delete_item(old)
    new = db.insert_item(kind="task", raw_text="別のタスク")
    assert new == old

    assert focus.active_session() is None
    session = _one("SELECT task_id, outcome FROM focus_sessions")
    assert session == {"task_id": -old, "outcome": "stopped"}
    assert _one("SELECT status FROM items WHERE id = ?", new)["status"] == "inbox"


def test_deleted_goal_report_is_rejected_for_goal_and_descendants(database: Path) -> None:
    """ゴール（子孫を含む）を削除すると、そのゴールへの承認待ち報告は却下される。"""
    parent = goals.create_goal("親")
    child = goals.create_goal("子", parent_id=parent)
    report = delegation.record_progress(None, child, "進みました")
    goals.delete_goal(parent)

    row = _one("SELECT status, goal_id FROM agent_reports WHERE id = ?", report)
    assert row == {"status": "rejected", "goal_id": -child}


def test_fetch_tasks_exposes_estimate_goal_and_delegation(database: Path) -> None:
    """ホームとタスク編集が使う取得結果に、見積もり・ゴール・委任先が含まれる。"""
    task = db.insert_item(kind="task", raw_text="見積もりあり", status="today")
    db.update_item_fields(task, estimate_minutes=90)
    delegation.set_delegation(task, "codex")

    for rows in (db.fetch_tasks(statuses=["today"]), db.fetch_tasks_for_today_view("2026-10-16")):
        row = next(r for r in rows if r["id"] == task)
        assert row["estimate_minutes"] == 90
        assert row["delegated_to"] == "codex"
        assert "goal_id" in row


def _block(task_id: int, day: str, start: str, end: str) -> int:
    with db.connection() as conn, conn:
        return conn.execute(
            "INSERT INTO time_blocks (day, task_id, title, start_time, end_time, status, calendar_event_id, created_at)"
            " VALUES (?, ?, 't', ?, ?, 'exported', 'ev', '2026-10-16T00:00:00')",
            (day, task_id, start, end),
        ).lastrowid


def test_deleting_task_cancels_all_its_time_blocks(database: Path) -> None:
    """タスクを削除すると、その時間割の枠は過去分も含めて取消予約になる。"""
    task = db.insert_item(kind="task", raw_text="消すタスク")
    past = _block(task, "2000-01-01", "09:00", "09:30")
    future = _block(task, "2999-01-01", "09:00", "09:30")
    db.delete_item(task)
    for block in (past, future):
        assert _one("SELECT status FROM time_blocks WHERE id = ?", block)["status"] == "cancelled"


def test_completing_task_keeps_past_blocks_and_cancels_upcoming(database: Path) -> None:
    """完了したタスクの枠は、終わったものを記録として残し、これからのものだけ取消す。"""
    task = db.insert_item(kind="task", raw_text="終えるタスク")
    past = _block(task, "2000-01-01", "09:00", "09:30")
    future = _block(task, "2999-01-01", "09:00", "09:30")
    db.complete_task(task)
    assert _one("SELECT status FROM time_blocks WHERE id = ?", past)["status"] == "exported"
    assert _one("SELECT status FROM time_blocks WHERE id = ?", future)["status"] == "cancelled"
