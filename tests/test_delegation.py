"""一時 DB で AI 委任と承認制の進捗反映を保証する。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any, Dict

import pytest

from symnote.core import db, delegation, goals


@pytest.fixture()
def database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """ユーザーの状態と分離した DB を初期化する。"""
    path = tmp_path / "delegation.db"
    monkeypatch.setenv("DB_PATH", str(path))
    db.init_db()
    return path


def _row(table: str, row_id: int) -> Dict[str, Any]:
    """テストで作成した行を取得する。"""
    assert table in ("items", "agent_reports")
    with db.connection() as conn:
        return dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())


def test_handoff_includes_task_ancestors_notes_and_reporting(database: Path) -> None:
    """貼付用指示書にタスク、祖先の理由・成功条件、関連メモ、報告方法が入る。"""
    root = goals.create_goal("公開", why="知見を共有", success_criteria="読者が再現できる")
    goal = goals.create_goal("下書き", why="整理する", success_criteria="構成完成", parent_id=root)
    task = db.insert_task("要点を3つ書く", tags="構成", due_date="2026-12-01")
    goals.set_task_goal(task, goal)
    memo = db.insert_memo("参考となる知見")
    db.insert_memo("無関係な秘密メモ")
    with db.connection() as conn, conn:
        conn.execute("UPDATE items SET goal_id = ? WHERE id = ?", (goal, memo))
    handoff = delegation.generate_handoff(task, "claude_code")
    for text in (
        "claude_code",
        "構成",
        "要点を3つ書く",
        "2026-12-01",
        "公開 → 下書き",
        "知見を共有",
        "読者が再現できる",
        "整理する",
        "構成完成",
        "参考となる知見",
        "承認待ち",
    ):
        assert text in handoff
    assert "無関係な秘密メモ" not in handoff
    assert handoff.endswith(
        f"終わったら SymNote MCP の report_progress で task_id={task} を報告して"
    )
    assert _row("items", task)["delegated_to"] is None


def test_delegation_save_clear_and_validation(database: Path) -> None:
    """担当の保存・解除は未完了タスクに限定され、指示書生成だけでは保存しない。"""
    task = db.insert_task("仕事")
    delegation.set_delegation(task, "codex")
    assert _row("items", task)["delegated_to"] == "codex"
    assert delegation.list_delegated_tasks()[0]["id"] == task
    delegation.set_delegation(task, None)
    assert delegation.list_delegated_tasks() == []
    for invalid in ("unknown", ""):
        with pytest.raises(ValueError):
            delegation.set_delegation(task, invalid)  # type: ignore[arg-type]
    db.complete_task(task)
    with pytest.raises(ValueError):
        delegation.set_delegation(task, "claude")
    delegation.set_delegation(task, None)
    with pytest.raises(ValueError):
        delegation.generate_handoff(db.insert_memo("メモ"))
    assert "所属ゴールは未設定" in delegation.generate_handoff(task)


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"task_id": 99999},
        {"goal_id": 99999},
        {"task_id": True},
        {"goal_id": -1},
        {"summary": ""},
        {"summary": "  "},
        {"summary": "a" * 501},
        {"details": "a" * 4001},
        {"agent": "a" * 41},
        {"suggested_status": "unknown"},
        {"details": None},
    ],
)
def test_invalid_reports_leave_no_rows(database: Path, fields: Dict[str, Any]) -> None:
    """対象や入力が不正なら報告を一切保存しない。"""
    task = db.insert_task("仕事")
    values: Dict[str, Any] = {"task_id": task, "goal_id": None, "summary": "途中まで完了"}
    values.update(fields)
    if fields == {}:
        values["task_id"] = None
    with pytest.raises(ValueError):
        delegation.record_progress(**values)
    assert delegation.pending_report_count() == 0


def test_record_progress_only_creates_pending_report(database: Path) -> None:
    """最大長を受け入れるが承認前にはタスクを変更しない。"""
    task = db.insert_task("仕事")
    before = _row("items", task)
    report = delegation.record_progress(task, None, "s" * 500, "d" * 4000, "done", "a" * 40)
    assert _row("items", task) == before
    row = _row("agent_reports", report)
    assert row["status"] == "pending" and row["reviewed_at"] is None
    assert delegation.pending_report_count() == 1
    assert delegation.list_pending_reports()[0]["id"] == report
    with pytest.raises(ValueError):
        delegation.record_progress(db.insert_memo("メモ"), None, "無効")


@pytest.mark.parametrize("suggested", ["progress", "done", "blocked"])
def test_approval_updates_once_and_preserves_rewards(database: Path, suggested: str) -> None:
    """進捗・完了・ブロックを一度だけ反映し、完了時は既存の報酬経路を通す。"""
    task = db.insert_item(kind="task", raw_text="元の本文", importance=2)
    report = delegation.record_progress(task, None, "成果", "詳細", suggested, "codex")  # type: ignore[arg-type]
    assert delegation.approve_report(report)
    assert not delegation.approve_report(report)
    assert not delegation.reject_report(report)
    row = _row("items", task)
    assert f"元の本文\n\n[進捗 {date.today().isoformat()} codex] 成果" in row["raw_text"]
    assert row["raw_text"].count("[進捗") == 1
    assert "詳細" in row["raw_text"]
    assert row["status"] == ("done" if suggested == "done" else "inbox")
    assert row["importance"] == (5 if suggested == "blocked" else 2)
    assert _row("agent_reports", report)["reviewed_at"]
    assert delegation.pending_report_count() == 0
    with db.connection() as conn:
        rewards = conn.execute("SELECT * FROM quest_log WHERE task_id = ?", (task,)).fetchall()
    assert len(rewards) == (1 if suggested == "done" else 0)


def test_concurrent_reviews_only_apply_once(database: Path) -> None:
    """同時承認されても本文追記と報酬は一度に限定される。"""
    task = db.insert_task("仕事")
    report = delegation.record_progress(task, None, "完了", suggested_status="done")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(delegation.approve_report, [report, report]))
    assert sorted(results) == [False, True]
    assert _row("items", task)["raw_text"].count("[進捗") == 1
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM quest_log").fetchone()[0] == 1


def test_completion_keeps_recurrence(database: Path) -> None:
    """報告承認で繰り返しタスクを完了しても次回分が作成される。"""
    task = db.create_recurring_task("週報", due_date="2026-10-16", frequency="weekly")
    report = delegation.record_progress(task, None, "完成", suggested_status="done")
    delegation.approve_report(report)
    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM items WHERE kind = 'task' ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0]["status"] == "done"
    assert rows[1]["due_date"] == "2026-10-23" and rows[1]["status"] == "inbox"


@pytest.mark.parametrize("suggested", ["progress", "done", "blocked"])
def test_goal_only_reports_become_notes(database: Path, suggested: str) -> None:
    """ゴールのみへの報告は理由・成功条件を変えず関連メモとして保存する。"""
    goal = goals.create_goal("公開", why="理由", success_criteria="成功")
    before = goals.get_goal_context(goal)
    report = delegation.record_progress(None, goal, "成果", "検証結果", suggested, "claude")  # type: ignore[arg-type]
    assert delegation.approve_report(report)
    assert not delegation.approve_report(report)
    assert goals.get_goal_context(goal) == before
    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM items WHERE goal_id = ?", (goal,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "memo" and rows[0]["tags"] == "進捗メモ"
    assert "成果" in rows[0]["raw_text"] and "検証結果" in rows[0]["raw_text"]


def test_reject_does_not_change_target(database: Path) -> None:
    """却下は報告状態と審査日時だけを変更する。"""
    task = db.insert_task("仕事")
    before = _row("items", task)
    report = delegation.record_progress(task, None, "完了", suggested_status="done")
    assert delegation.reject_report(report)
    assert not delegation.reject_report(report)
    assert not delegation.approve_report(report)
    assert _row("items", task) == before
    assert _row("agent_reports", report)["status"] == "rejected"
    assert _row("agent_reports", report)["reviewed_at"]


def test_deleted_task_report_is_rejected_automatically(database: Path) -> None:
    """報告後に対象タスクが消えたら、報告は自動で却下され承認できない。"""
    task = db.insert_task("仕事")
    report = delegation.record_progress(task, None, "完了", suggested_status="done")
    db.delete_item(task)
    assert delegation.pending_report_count() == 0
    assert _row("agent_reports", report)["status"] == "rejected"
    assert delegation.approve_report(report) is False


def test_deleted_goal_report_is_rejected_automatically(database: Path) -> None:
    """ゴール削除後の報告は自動で却下され、承認できない。"""
    goal = goals.create_goal("公開")
    report = delegation.record_progress(None, goal, "報告")
    goals.delete_goal(goal)
    assert _row("agent_reports", report)["status"] == "rejected"
    assert delegation.approve_report(report) is False


def test_failure_rolls_back_review_and_task(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """完了報酬処理が失敗すれば承認も本文もロールバックする。"""
    from symnote.core import quest

    def fail(*args: Any, **kwargs: Any) -> None:
        """報酬保存失敗を再現する。"""
        raise RuntimeError("保存失敗")

    task = db.insert_task("仕事")
    before = _row("items", task)
    report = delegation.record_progress(task, None, "完成", suggested_status="done")
    monkeypatch.setattr(quest, "award_for_completion_in_transaction", fail)
    with pytest.raises(RuntimeError):
        delegation.approve_report(report)
    assert _row("items", task) == before
    assert _row("agent_reports", report)["status"] == "pending"


def test_next_actions_sort_and_limit(database: Path) -> None:
    """期限が最優先で、同期限は重要度・緊急度順となり完了は除外される。"""
    no_due = db.insert_item(kind="task", raw_text="期限なし", importance=5, urgency=5)
    urgent = db.insert_item(
        kind="task", raw_text="緊急", due_date="2026-11-01", importance=3, urgency=5
    )
    important = db.insert_item(
        kind="task", raw_text="重要", due_date="2026-11-01", importance=4, urgency=1
    )
    earliest = db.insert_item(kind="task", raw_text="最短", due_date="2026-10-01", importance=1)
    db.insert_item(kind="task", raw_text="完了", due_date="2026-01-01", status="done")
    assert [row["id"] for row in delegation.list_next_actions()] == [
        earliest,
        important,
        urgent,
        no_due,
    ]
    assert len(delegation.list_next_actions(1)) == 1


def test_delegate_view_saves_and_clears(database: Path) -> None:
    """委任 UI からコピー用指示書を表示し、担当を保存・解除できる。"""
    from streamlit.testing.v1 import AppTest

    task = db.insert_task("仕事")
    script = f"""
from symnote.core import db
from symnote.views.delegation_view import render_delegate_button
with db.connection() as conn:
    task = dict(conn.execute("SELECT * FROM items WHERE id = ?", ({task},)).fetchone())
render_delegate_button(task)
"""
    app = AppTest.from_string(script).run()
    assert not app.exception
    assert not app.code
    app.toggle(key=f"delegate_open_{task}").set_value(True).run()
    assert f"task_id={task}" in app.code[0].value
    app.selectbox[0].select("codex").run()
    app.button(key=f"delegate_save_{task}").click().run()
    assert not app.exception
    assert _row("items", task)["delegated_to"] == "codex"
    app.button(key=f"delegate_clear_{task}").click().run()
    assert not app.exception
    assert _row("items", task)["delegated_to"] is None


def test_report_view_escapes_external_text_and_approves(database: Path) -> None:
    """報告の HTML をエスケープし、UI 承認で初めて完了状態にする。"""
    from streamlit.testing.v1 import AppTest

    task = db.insert_task("<script>task</script>", tags="<b>task</b>")
    report = delegation.record_progress(
        task,
        None,
        "<script>summary</script>",
        "<img src=x onerror=alert(1)>",
        "done",
        "<b>agent</b>",
    )
    app = AppTest.from_string(
        "from symnote.views.delegation_view import render_agent_reports\nrender_agent_reports()"
    ).run()
    assert not app.exception
    markup = "\n".join(element.value for element in app.markdown)
    assert "&lt;script&gt;summary&lt;/script&gt;" in markup
    assert "&lt;b&gt;agent&lt;/b&gt;" in markup
    assert "&lt;b&gt;task&lt;/b&gt;" in markup
    assert "&lt;img" in markup
    assert "<script>" not in markup and "<img" not in markup
    app.button(key=f"report_approve_{report}").click().run()
    assert not app.exception
    assert _row("items", task)["status"] == "done"
    assert delegation.pending_report_count() == 0


def test_report_view_rejects(database: Path) -> None:
    """UI 却下ではタスクに報告内容を反映しない。"""
    from streamlit.testing.v1 import AppTest

    task = db.insert_task("仕事")
    report = delegation.record_progress(task, None, "変更しない", suggested_status="done")
    app = AppTest.from_string(
        "from symnote.views.delegation_view import render_agent_reports\nrender_agent_reports()"
    ).run()
    app.button(key=f"report_reject_{report}").click().run()
    assert not app.exception
    assert _row("items", task)["status"] == "inbox"
    assert _row("items", task)["raw_text"] == "仕事"
    assert _row("agent_reports", report)["status"] == "rejected"
