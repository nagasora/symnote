"""削除したタスク・ゴールの ID が再利用されても、古い報告や集中が新しい項目に効かないことを保証する。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
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


def test_deleted_goal_is_detached_from_trash_before_its_id_is_reused(database: Path) -> None:
    """削除済みタスクは、後から同じ ID で作られた別ゴールへ復元時に紐付かない。"""
    old_goal = goals.create_goal("削除するゴール")
    task = db.insert_task("ゴミ箱のタスク", goal_id=old_goal)
    db.delete_item(task)
    trash_id = db.list_trashed_items()[0]["id"]

    goals.delete_goal(old_goal)
    replacement_goal = goals.create_goal("別のゴール")
    assert replacement_goal == old_goal

    restored = db.fetch_item(db.restore_trashed_item(trash_id))

    assert restored is not None
    assert restored["goal_id"] is None


def test_trash_ids_are_not_reused_after_restore(database: Path) -> None:
    """復元済みの古い trash ID が、後で削除した別アイテムを指さない。"""
    first = db.insert_memo("最初")
    db.delete_item(first)
    first_trash_id = db.list_trashed_items()[0]["id"]
    db.restore_trashed_item(first_trash_id)

    second = db.insert_memo("次")
    db.delete_item(second)
    second_trash_id = db.list_trashed_items()[0]["id"]

    assert second_trash_id > first_trash_id
    with pytest.raises(LookupError):
        db.restore_trashed_item(first_trash_id)


def test_concurrent_restore_commits_exactly_once(database: Path) -> None:
    """同じ trash ID の同時復元は 1 件だけ成功し、重複アイテムを作らない。"""
    item = db.insert_task("同時復元")
    db.delete_item(item)
    trash_id = db.list_trashed_items()[0]["id"]
    ready = threading.Barrier(2)

    def restore() -> int:
        ready.wait(timeout=5)
        return db.restore_trashed_item(trash_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(restore) for _ in range(2)]

    restored_ids: list[int] = []
    failures: list[Exception] = []
    for future in futures:
        try:
            restored_ids.append(future.result())
        except Exception as exc:
            failures.append(exc)

    assert len(restored_ids) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], LookupError)
    assert db.list_trashed_items() == []
    assert [row["id"] for row in db.search_items("同時復元")] == restored_ids


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
