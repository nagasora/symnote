"""Progress hierarchy, event import, evidence review, and migration durability tests."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from symnote.core import progress
from symnote.core.db import SCHEMA_VERSION, fetch_tasks, init_db, insert_task, update_item_fields


@pytest.fixture()
def database(monkeypatch, tmp_path):
    path = tmp_path / "progress.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    init_db()
    return path


def _workspace(database):
    project = progress.create_project(
        "Demo Planning", scope="workspace", workspace_name="Sample Team", owner="Aki"
    )
    goal_id = progress.create_goal(
        project["id"],
        "Ship the sample release",
        status="active",
        current_work="Reviewing the import flow",
        next_action="Check the conflict screen",
        owner="Aki",
    )
    task_id = progress.create_project_task(
        project["id"], goal_id, "Test event import", owner="Aki", due_date="2026-10-05"
    )
    return project, goal_id, task_id


def _report(project_key: str, target_kind: str, target_id: int, **overrides):
    payload = {
        "schema": progress.EVENT_SCHEMA,
        "event_id": str(uuid.uuid4()),
        "reported_at": "2026-10-02T12:00:00Z",
        "project_key": project_key,
        "target": {"kind": target_kind, "id": target_id},
        "expected_revision": 0,
        "claimed_status": "in_progress",
        "current_work": "Reviewing local progress reports",
        "next_action": "Import the event file",
        "owner": "Aki",
        "blocked_reason": "",
        "approval_required": False,
        "approval_state": "not_required",
        "source": {
            "repo": "https://example.test/team/sample",
            "branch": "feature/progress",
            "commit": "a" * 40,
            "worktree": "/tmp/sample-worktree",
        },
        "artifact_ref": "",
        "test_ref": "",
        "claimed_test_result": "not_run",
    }
    payload.update(overrides)
    return payload


def _convert_to_v10(path: Path) -> None:
    """Turn an empty synthetic v11 DB into a minimal v10 fixture for migration tests."""
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE progress_events")
        conn.execute("DROP INDEX IF EXISTS idx_items_progress_project")
        conn.execute("ALTER TABLE items DROP COLUMN progress_revision")
        conn.execute("ALTER TABLE items DROP COLUMN owner")
        conn.execute("ALTER TABLE items DROP COLUMN progress_goal_id")
        conn.execute("ALTER TABLE items DROP COLUMN progress_project_id")
        conn.execute("DROP TABLE progress_goals")
        conn.execute("DROP TABLE progress_projects")
        conn.execute("PRAGMA user_version = 10")


def test_v10_migration_is_backed_up_before_schema_changes(monkeypatch, tmp_path) -> None:
    path = tmp_path / "upgrade.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    init_db()
    insert_task("Keep this synthetic task")
    _convert_to_v10(path)

    init_db()

    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            conn.execute("SELECT raw_text FROM items").fetchone()[0] == "Keep this synthetic task"
        )
    backups = sorted((tmp_path / "backups").glob("symnote-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 10
        assert (
            backup.execute("SELECT raw_text FROM items").fetchone()[0] == "Keep this synthetic task"
        )
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_failed_v11_migration_rolls_back_schema_and_keeps_backup(monkeypatch, tmp_path) -> None:
    path = tmp_path / "rollback.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    init_db()
    _convert_to_v10(path)
    with sqlite3.connect(path) as conn:
        # A broken pre-existing table forces the indexed v11 migration to fail after
        # it has started making schema changes, exercising SQLite transaction rollback.
        conn.execute("CREATE TABLE progress_goals (id INTEGER PRIMARY KEY, title TEXT)")

    with pytest.raises(sqlite3.OperationalError):
        init_db()

    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
        assert "progress_projects" not in {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        item_columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
        assert "progress_project_id" not in item_columns
        assert "progress_revision" not in item_columns
    backups = list((tmp_path / "backups").glob("symnote-*.db"))
    assert len(backups) == 1


def test_project_goal_task_hierarchy_and_workspace_label(database) -> None:
    project, goal_id, task_id = _workspace(database)

    listed_project = progress.list_projects()[0]
    goal = progress.list_goals(project["id"])[0]
    task = progress.list_project_tasks(project["id"])[0]

    assert listed_project["scope"] == "workspace"
    assert listed_project["workspace_name"] == "Sample Team"
    assert (listed_project["goal_count"], listed_project["task_count"]) == (1, 1)
    assert goal["id"] == goal_id
    assert goal["next_action"] == "Check the conflict screen"
    assert task["id"] == task_id
    assert task["goal_title"] == goal["title"]
    assert task["status"] == "inbox"


def test_report_is_idempotent_and_does_not_complete_task(database) -> None:
    project, _goal_id, task_id = _workspace(database)
    event = _report(project["project_key"], "task", task_id, claimed_status="completed")

    first = progress.import_progress_events([event])
    second = progress.import_progress_events([event])

    task = progress.list_project_tasks(project["id"])[0]
    assert first["imported_event_ids"] == [event["event_id"]]
    assert second["duplicates_skipped"] == 1
    assert second["imported_event_ids"] == []
    assert task["status"] == "inbox"
    assert task["progress_revision"] == 1
    assert fetch_tasks()[0]["status"] == "inbox"
    activity = progress.list_project_activity(project["id"])
    assert activity[0]["outcome"] == "pending_review"
    assert activity[0]["claimed_status"] == "completed"


def test_same_task_stale_revision_is_visible_conflict_without_overwrite(database) -> None:
    project, _goal_id, task_id = _workspace(database)
    first = _report(project["project_key"], "task", task_id, claimed_status="in_progress")
    second = _report(
        project["project_key"],
        "task",
        task_id,
        event_id=str(uuid.uuid4()),
        claimed_status="blocked",
        blocked_reason="Waiting on a review",
    )

    progress.import_progress_events([first])
    result = progress.import_progress_events([second])

    assert result["conflicts"] == [second["event_id"]]
    task = progress.list_project_tasks(project["id"])[0]
    assert task["progress_revision"] == 1
    activity = progress.list_project_activity(project["id"])
    conflict = next(row for row in activity if row["event_id"] == second["event_id"])
    assert conflict["outcome"] == "conflict"
    assert "expected_revision=0" in conflict["conflict_reason"]
    assert conflict["blocked_reason"] == "Waiting on a review"


def test_event_id_cannot_be_reused_for_different_payload(database) -> None:
    project, _goal_id, task_id = _workspace(database)
    event = _report(project["project_key"], "task", task_id)
    progress.import_progress_events([event])
    altered = {**event, "current_work": "different report content"}

    with pytest.raises(ValueError, match="内容が以前"):
        progress.import_progress_events([altered])

    reports = [
        row
        for row in progress.list_project_activity(project["id"])
        if row["event_type"] == "report"
    ]
    assert len(reports) == 1
    assert progress.list_project_tasks(project["id"])[0]["progress_revision"] == 1


def test_invalid_batch_rolls_back_prior_valid_events(database) -> None:
    project, _goal_id, task_id = _workspace(database)
    valid = _report(project["project_key"], "task", task_id)
    invalid = _report("missing-project", "task", task_id, event_id=str(uuid.uuid4()))

    with pytest.raises(LookupError):
        progress.import_progress_events([valid, invalid])

    assert not any(
        row["event_type"] == "report" for row in progress.list_project_activity(project["id"])
    )
    assert progress.list_project_tasks(project["id"])[0]["progress_revision"] == 0


def test_unknown_fields_are_rejected_without_interpreting_commands(database) -> None:
    project, _goal_id, task_id = _workspace(database)
    event = _report(project["project_key"], "task", task_id)
    event["command"] = "echo should never run"

    with pytest.raises(ValueError, match="未対応"):
        progress.import_progress_events([event])

    assert progress.list_project_tasks(project["id"])[0]["progress_revision"] == 0


def test_completion_review_requires_artifact_and_test_and_binds_snapshot(database) -> None:
    project, _goal_id, task_id = _workspace(database)
    report = _report(
        project["project_key"],
        "task",
        task_id,
        claimed_status="completed",
        artifact_ref="artifacts/build-summary.txt",
        test_ref="artifacts/tests-main-commit.log",
        claimed_test_result="passed",
    )
    progress.import_progress_events([report])

    with pytest.raises(ValueError, match="根拠を確認"):
        progress.review_completion_claim(
            report["event_id"], "Reviewer", "Checked", artifact_checked=True, test_checked=False
        )
    review_id = progress.review_completion_claim(
        report["event_id"],
        "Reviewer",
        "Inspected the linked evidence",
        artifact_checked=True,
        test_checked=True,
    )
    claim = next(
        row
        for row in progress.list_project_activity(project["id"])
        if row["event_id"] == report["event_id"]
    )
    review = next(
        row for row in progress.list_project_activity(project["id"]) if row["event_id"] == review_id
    )

    assert claim["review_outcome"] == "verified"
    assert review["target_event_id"] == report["event_id"]
    assert review["source_commit"] == report["source"]["commit"]
    assert review["artifact_ref"] == report["artifact_ref"]
    with pytest.raises(ValueError, match="既にレビュー"):
        progress.review_completion_claim(
            report["event_id"],
            "Reviewer",
            "Second review",
            artifact_checked=True,
            test_checked=True,
        )


def test_review_of_one_commit_does_not_certify_a_later_claim(database) -> None:
    project, _goal_id, task_id = _workspace(database)
    first = _report(
        project["project_key"],
        "task",
        task_id,
        claimed_status="completed",
        artifact_ref="build/a",
        test_ref="tests/a.log",
        claimed_test_result="passed",
    )
    progress.import_progress_events([first])
    progress.review_completion_claim(
        first["event_id"],
        "Reviewer",
        "Reviewed commit a",
        artifact_checked=True,
        test_checked=True,
    )
    second = _report(
        project["project_key"],
        "task",
        task_id,
        expected_revision=1,
        event_id=str(uuid.uuid4()),
        source={
            "repo": "https://example.test/team/sample",
            "branch": "feature/progress",
            "commit": "b" * 40,
            "worktree": "/tmp/sample-worktree",
        },
        claimed_status="completed",
        artifact_ref="build/b",
        test_ref="tests/b.log",
        claimed_test_result="passed",
    )
    progress.import_progress_events([second])

    claim_rows = {
        row["event_id"]: row
        for row in progress.list_project_activity(project["id"])
        if row["event_type"] == "report"
    }
    assert claim_rows[first["event_id"]]["review_outcome"] == "verified"
    assert claim_rows[second["event_id"]]["review_outcome"] is None
    assert claim_rows[second["event_id"]]["source_commit"] == "b" * 40


def test_activity_log_is_append_only_and_exportable(database) -> None:
    project, _goal_id, task_id = _workspace(database)
    report = _report(project["project_key"], "task", task_id)
    progress.import_progress_events([report])
    with sqlite3.connect(database) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE progress_events SET owner='overwritten' WHERE event_id=?",
                (report["event_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM progress_events WHERE event_id=?", (report["event_id"],))

    export = progress.export_progress_jsonl(project["id"])
    records = [json.loads(line) for line in export.splitlines()]
    assert any(row["event_id"] == report["event_id"] for row in records)
    assert progress.is_stale(None)
    assert not progress.is_stale(dt.datetime.now(dt.UTC).isoformat())


def test_manual_task_update_advances_revision(database) -> None:
    project, _goal_id, task_id = _workspace(database)
    update_item_fields(task_id, status="today")
    task = progress.list_project_tasks(project["id"])[0]
    assert task["status"] == "today"
    assert task["progress_revision"] == 1


def test_local_reporter_appends_agent_neutral_jsonl(tmp_path, database) -> None:
    from symnote.progress_reporter import append_event, build_event

    project, _goal_id, task_id = _workspace(database)
    output = tmp_path / "reports" / "agent-progress.jsonl"
    event = build_event(
        project_key=project["project_key"],
        target_kind="task",
        target_id=task_id,
        expected_revision=0,
        claimed_status="in_progress",
        source_repo="example/repo",
        source_branch="feature/test",
        source_commit="f" * 40,
        next_action="Review the report",
    )

    append_event(output, event)
    append_event(output, event)
    parsed = progress.parse_progress_file(output.read_text(encoding="utf-8"))
    result = progress.import_progress_events(parsed)

    assert result["imported_event_ids"] == [event["event_id"]]
    assert result["duplicates_skipped"] == 1
    assert output.read_text(encoding="utf-8").count("\n") == 2
