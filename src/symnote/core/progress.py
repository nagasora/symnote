"""Cross-project progress metadata and append-only agent reports.

Agent reports are claims. They never change a task's status or certify evidence;
verification is a separate, explicit action in the local review screen.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Sequence
from typing import Any

from symnote.core.db import connection, insert_item_in_transaction

EVENT_SCHEMA = "symnote.progress-event.v1"
MAX_EVENTS_PER_IMPORT = 50
MAX_EVENT_TEXT = 2_000
MAX_SOURCE_TEXT = 500
PROGRESS_STATUSES = ("not_started", "in_progress", "blocked", "completed", "needs_approval")
APPROVAL_STATES = ("not_required", "pending", "approved", "rejected")
TEST_RESULTS = ("passed", "failed", "not_run", "unknown")
PROJECT_SCOPES = ("personal", "workspace")

EVENT_COLUMNS = (
    "event_id",
    "payload_hash",
    "event_type",
    "project_id",
    "target_kind",
    "goal_id",
    "task_id",
    "expected_revision",
    "observed_revision",
    "revision_after",
    "outcome",
    "claimed_status",
    "current_work",
    "next_action",
    "owner",
    "blocked_reason",
    "approval_required",
    "approval_state",
    "source_repo",
    "source_branch",
    "source_commit",
    "source_worktree",
    "artifact_ref",
    "test_ref",
    "claimed_test_result",
    "target_event_id",
    "reviewer",
    "review_note",
    "conflict_reason",
    "reported_at",
    "received_at",
)


class ProgressConflict(ValueError):
    """A local edit was based on an older project/goal/task revision."""


def _now() -> str:
    """Return a sortable UTC timestamp for local receipt and activity records."""
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_text(value: Any, field: str, limit: int = MAX_EVENT_TEXT, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} は文字列で指定してください。")
    cleaned = value.strip()
    if required and not cleaned:
        raise ValueError(f"{field} は必須です。")
    if len(cleaned) > limit:
        raise ValueError(f"{field} は {limit} 文字以内にしてください。")
    return cleaned


def _slug(value: str) -> str:
    """Generate a readable stable key; Japanese-only names receive a short opaque suffix."""
    base = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48]
    return base or f"project-{uuid.uuid4().hex[:8]}"


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _append_event(conn: sqlite3.Connection, **values: Any) -> None:
    """Insert one immutable log row; callers own the surrounding transaction."""
    now = _now()
    defaults: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "payload_hash": "",
        "event_type": "manual",
        "project_id": None,
        "target_kind": "project",
        "goal_id": None,
        "task_id": None,
        "expected_revision": None,
        "observed_revision": None,
        "revision_after": None,
        "outcome": "applied",
        "claimed_status": "",
        "current_work": "",
        "next_action": "",
        "owner": "",
        "blocked_reason": "",
        "approval_required": 0,
        "approval_state": "not_required",
        "source_repo": "",
        "source_branch": "",
        "source_commit": "",
        "source_worktree": "",
        "artifact_ref": "",
        "test_ref": "",
        "claimed_test_result": "unknown",
        "target_event_id": "",
        "reviewer": "",
        "review_note": "",
        "conflict_reason": "",
        "reported_at": now,
        "received_at": now,
    }
    record = {**defaults, **values}
    if not record["payload_hash"]:
        record["payload_hash"] = _payload_hash(
            {
                key: record[key]
                for key in EVENT_COLUMNS
                if key not in {"payload_hash", "received_at"}
            }
        )
    columns = ", ".join(EVENT_COLUMNS)
    placeholders = ", ".join("?" for _ in EVENT_COLUMNS)
    conn.execute(
        f"INSERT INTO progress_events ({columns}) VALUES ({placeholders})",
        tuple(record[column] for column in EVENT_COLUMNS),
    )


def create_project(
    name: str,
    scope: str = "personal",
    workspace_name: str = "",
    description: str = "",
    owner: str = "",
    project_key: str | None = None,
) -> dict[str, Any]:
    """Create a local project; scope labels organize data but are not access controls."""
    clean_name = _clean_text(name, "name", 120, required=True)
    if scope not in PROJECT_SCOPES:
        raise ValueError("scope は personal または workspace で指定してください。")
    workspace = _clean_text(workspace_name, "workspace_name", 120)
    clean_description = _clean_text(description, "description", 2_000)
    clean_owner = _clean_text(owner, "owner", 120)
    key = _clean_text(project_key, "project_key", 64) if project_key else _slug(clean_name)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", key):
        raise ValueError("project_key は英小文字・数字・.-_ を使ってください。")
    now = _now()
    with connection() as conn, conn:
        existing = conn.execute(
            "SELECT 1 FROM progress_projects WHERE project_key = ?", (key,)
        ).fetchone()
        if existing and project_key:
            raise ValueError(f"project_key '{key}' は既に使われています。")
        if existing:
            key = f"{key[:54]}-{uuid.uuid4().hex[:8]}"
        cur = conn.execute(
            """
            INSERT INTO progress_projects (
              project_key, name, scope, workspace_name, description, owner, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (key, clean_name, scope, workspace, clean_description, clean_owner, now, now),
        )
        project_id = int(cur.lastrowid)
        _append_event(
            conn,
            event_type="manual",
            project_id=project_id,
            claimed_status="created",
            current_work="プロジェクトを作成しました。",
        )
    return {"id": project_id, "project_key": key, "name": clean_name, "scope": scope}


def list_projects() -> list[dict[str, Any]]:
    """Return project rows with linked task/goal counts and report freshness."""
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
              (SELECT COUNT(*) FROM progress_goals g WHERE g.project_id = p.id) AS goal_count,
              (SELECT COUNT(*) FROM items i WHERE i.progress_project_id = p.id AND i.kind = 'task')
                AS task_count,
              (SELECT MAX(e.received_at) FROM progress_events e
                WHERE e.project_id = p.id AND e.event_type = 'report') AS last_report_at
              ,(SELECT e.outcome FROM progress_events e
                WHERE e.project_id = p.id AND e.event_type = 'report'
                ORDER BY e.received_at DESC, e.id DESC LIMIT 1) AS last_report_outcome
            FROM progress_projects p
            ORDER BY p.updated_at DESC, p.id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def create_goal(
    project_id: int,
    title: str,
    description: str = "",
    status: str = "planned",
    current_work: str = "",
    next_action: str = "",
    owner: str = "",
    blocked_reason: str = "",
    approval_required: bool = False,
    approval_state: str = "not_required",
) -> int:
    """Create a goal under one project and record the creation in activity."""
    values = _goal_values(
        title,
        description,
        status,
        current_work,
        next_action,
        owner,
        blocked_reason,
        approval_required,
        approval_state,
    )
    now = _now()
    with connection() as conn, conn:
        if not conn.execute(
            "SELECT 1 FROM progress_projects WHERE id = ?", (project_id,)
        ).fetchone():
            raise LookupError(f"プロジェクト {project_id} はありません。")
        cur = conn.execute(
            """
            INSERT INTO progress_goals (
              project_id, title, description, status, current_work, next_action, owner,
              blocked_reason, approval_required, approval_state, revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (project_id, *values, now, now),
        )
        goal_id = int(cur.lastrowid)
        conn.execute("UPDATE progress_projects SET updated_at = ? WHERE id = ?", (now, project_id))
        _append_event(
            conn,
            project_id=project_id,
            target_kind="goal",
            goal_id=goal_id,
            claimed_status="created",
            current_work=values[3],
            next_action=values[4],
            owner=values[5],
            blocked_reason=values[6],
            approval_required=int(values[7]),
            approval_state=values[8],
            observed_revision=0,
            revision_after=0,
        )
        return goal_id


def _goal_values(
    title: str,
    description: str,
    status: str,
    current_work: str,
    next_action: str,
    owner: str,
    blocked_reason: str,
    approval_required: bool,
    approval_state: str,
) -> tuple[str, str, str, str, str, str, str, int, str]:
    clean_title = _clean_text(title, "title", 200, required=True)
    if status not in {"planned", "active", "blocked", "done"}:
        raise ValueError("goal status は planned / active / blocked / done です。")
    if type(approval_required) is not bool:
        raise ValueError("approval_required は true または false です。")
    if approval_state not in APPROVAL_STATES:
        raise ValueError("approval_state が不正です。")
    return (
        clean_title,
        _clean_text(description, "description"),
        status,
        _clean_text(current_work, "current_work"),
        _clean_text(next_action, "next_action"),
        _clean_text(owner, "owner", 120),
        _clean_text(blocked_reason, "blocked_reason"),
        int(approval_required),
        approval_state,
    )


def list_goals(project_id: int) -> list[dict[str, Any]]:
    """List goals in a project, including task counts."""
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT g.*,
              (SELECT COUNT(*) FROM items i
                WHERE i.progress_goal_id = g.id AND i.kind = 'task') AS task_count
            FROM progress_goals g WHERE g.project_id = ?
            ORDER BY CASE g.status WHEN 'active' THEN 0 WHEN 'blocked' THEN 1
              WHEN 'planned' THEN 2 ELSE 3 END, g.id
            """,
            (project_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def update_goal(
    goal_id: int,
    expected_revision: int,
    *,
    title: str,
    description: str = "",
    status: str = "planned",
    current_work: str = "",
    next_action: str = "",
    owner: str = "",
    blocked_reason: str = "",
    approval_required: bool = False,
    approval_state: str = "not_required",
) -> int:
    """Update human-owned goal context with optimistic concurrency and an activity row."""
    values = _goal_values(
        title,
        description,
        status,
        current_work,
        next_action,
        owner,
        blocked_reason,
        approval_required,
        approval_state,
    )
    if expected_revision < 0:
        raise ValueError("expected_revision は 0 以上です。")
    now = _now()
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT project_id, revision FROM progress_goals WHERE id = ?", (goal_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"目標 {goal_id} はありません。")
        current_revision = int(row["revision"])
        if current_revision != expected_revision:
            raise ProgressConflict(
                f"目標の版が更新されています（画面 {expected_revision} / "
                f"現在 {current_revision}）。"
            )
        revision_after = current_revision + 1
        conn.execute(
            """
            UPDATE progress_goals
            SET title = ?, description = ?, status = ?, current_work = ?, next_action = ?,
                owner = ?, blocked_reason = ?, approval_required = ?, approval_state = ?,
                revision = ?, updated_at = ?
            WHERE id = ? AND revision = ?
            """,
            (*values, revision_after, now, goal_id, current_revision),
        )
        _append_event(
            conn,
            event_type="manual",
            project_id=int(row["project_id"]),
            target_kind="goal",
            goal_id=goal_id,
            expected_revision=current_revision,
            observed_revision=current_revision,
            revision_after=revision_after,
            claimed_status=status,
            current_work=values[3],
            next_action=values[4],
            owner=values[5],
            blocked_reason=values[6],
            approval_required=values[7],
            approval_state=values[8],
        )
        conn.execute(
            "UPDATE progress_projects SET updated_at = ? WHERE id = ?", (now, row["project_id"])
        )
    return revision_after


def create_project_task(
    project_id: int,
    goal_id: int,
    title: str,
    details: str = "",
    owner: str = "",
    due_date: str | None = None,
) -> int:
    """Create and link a task to its project and goal in one local transaction."""
    clean_title = _clean_text(title, "title", 200, required=True)
    clean_details = _clean_text(details, "details")
    clean_owner = _clean_text(owner, "owner", 120)
    if due_date:
        try:
            due_date = dt.date.fromisoformat(due_date).isoformat()
        except ValueError as exc:
            raise ValueError("due_date は YYYY-MM-DD 形式で指定してください。") from exc
    now = _now()
    with connection() as conn, conn:
        goal = conn.execute(
            "SELECT project_id FROM progress_goals WHERE id = ?", (goal_id,)
        ).fetchone()
        if goal is None or int(goal["project_id"]) != project_id:
            raise ValueError("選んだ目標はこのプロジェクトに属していません。")
        task_id = insert_item_in_transaction(
            conn,
            kind="task",
            raw_text=clean_details or clean_title,
            tags=clean_title,
            due_date=due_date,
            status="inbox",
            ai_category="task",
            importance=3,
            urgency=3,
        )
        conn.execute(
            """
            UPDATE items SET progress_project_id = ?, progress_goal_id = ?, owner = ?
            WHERE id = ?
            """,
            (project_id, goal_id, clean_owner, task_id),
        )
        _append_event(
            conn,
            project_id=project_id,
            target_kind="task",
            task_id=task_id,
            claimed_status="created",
            owner=clean_owner,
            observed_revision=0,
            revision_after=0,
        )
        conn.execute("UPDATE progress_projects SET updated_at = ? WHERE id = ?", (now, project_id))
        return task_id


def list_project_tasks(project_id: int, goal_id: int | None = None) -> list[dict[str, Any]]:
    """Return tasks linked to a project; existing unlinked tasks remain untouched."""
    query = """
        SELECT i.id, i.tags AS title, i.raw_text AS details, i.status, i.due_date, i.due_time,
               i.owner, i.progress_revision, i.progress_goal_id, g.title AS goal_title
        FROM items i LEFT JOIN progress_goals g ON g.id = i.progress_goal_id
        WHERE i.kind = 'task' AND i.progress_project_id = ?
    """
    params: list[Any] = [project_id]
    if goal_id is not None:
        query += " AND i.progress_goal_id = ?"
        params.append(goal_id)
    query += " ORDER BY CASE i.status WHEN 'today' THEN 0 WHEN 'inbox' THEN 1 "
    query += "WHEN 'week' THEN 2 ELSE 3 END, i.due_date, i.id"
    with connection() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def link_task_to_goal(
    task_id: int,
    project_id: int,
    goal_id: int,
    expected_revision: int,
    owner: str = "",
) -> int:
    """Link an existing task with an optimistic revision check."""
    clean_owner = _clean_text(owner, "owner", 120)
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute(
            "SELECT kind, progress_revision FROM items WHERE id = ?", (task_id,)
        ).fetchone()
        goal = conn.execute(
            "SELECT project_id FROM progress_goals WHERE id = ?", (goal_id,)
        ).fetchone()
        if task is None or task["kind"] != "task":
            raise LookupError(f"タスク {task_id} はありません。")
        if goal is None or int(goal["project_id"]) != project_id:
            raise ValueError("選んだ目標はこのプロジェクトに属していません。")
        current_revision = int(task["progress_revision"])
        if current_revision != expected_revision:
            raise ProgressConflict(
                f"タスクの版が更新されています（画面 {expected_revision} / "
                f"現在 {current_revision}）。"
            )
        revision_after = current_revision + 1
        conn.execute(
            """
            UPDATE items SET progress_project_id = ?, progress_goal_id = ?, owner = ?,
              progress_revision = ? WHERE id = ? AND progress_revision = ?
            """,
            (project_id, goal_id, clean_owner, revision_after, task_id, current_revision),
        )
        _append_event(
            conn,
            project_id=project_id,
            target_kind="task",
            task_id=task_id,
            expected_revision=current_revision,
            observed_revision=current_revision,
            revision_after=revision_after,
            claimed_status="linked",
            owner=clean_owner,
        )
        return revision_after


def _validate_report(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the agent-neutral v1 payload; extra fields are rejected as inert data."""
    allowed = {
        "schema",
        "event_id",
        "reported_at",
        "project_key",
        "target",
        "expected_revision",
        "claimed_status",
        "current_work",
        "next_action",
        "owner",
        "blocked_reason",
        "approval_required",
        "approval_state",
        "source",
        "artifact_ref",
        "test_ref",
        "claimed_test_result",
    }
    if not isinstance(payload, dict):
        raise ValueError("進捗イベントは JSON オブジェクトで指定してください。")
    extra = set(payload) - allowed
    if extra:
        raise ValueError("未対応のフィールドがあります: " + ", ".join(sorted(extra)))
    required = {
        "schema",
        "event_id",
        "reported_at",
        "project_key",
        "target",
        "expected_revision",
        "claimed_status",
        "source",
    }
    missing = required - set(payload)
    if missing:
        raise ValueError("必須フィールドがありません: " + ", ".join(sorted(missing)))
    if payload["schema"] != EVENT_SCHEMA:
        raise ValueError(f"schema は {EVENT_SCHEMA} を指定してください。")
    event_id = _clean_text(payload["event_id"], "event_id", 128, required=True)
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", event_id):
        raise ValueError("event_id に使えない文字が含まれています。")
    project_key = _clean_text(payload["project_key"], "project_key", 64, required=True)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", project_key):
        raise ValueError("project_key の形式が不正です。")
    target = payload["target"]
    if not isinstance(target, dict) or set(target) != {"kind", "id"}:
        raise ValueError("target は kind と id を持つオブジェクトです。")
    target_kind = target["kind"]
    target_id = target["id"]
    if target_kind not in {"goal", "task"} or type(target_id) is not int or target_id <= 0:
        raise ValueError("target.kind は goal/task、target.id は正の整数です。")
    expected_revision = payload["expected_revision"]
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("expected_revision は 0 以上の整数です。")
    status = payload["claimed_status"]
    if status not in PROGRESS_STATUSES:
        raise ValueError("claimed_status が不正です。")
    approval_required = payload.get("approval_required", False)
    if type(approval_required) is not bool:
        raise ValueError("approval_required は true または false です。")
    approval_state = payload.get("approval_state", "not_required")
    if approval_state not in APPROVAL_STATES:
        raise ValueError("approval_state が不正です。")
    test_result = payload.get("claimed_test_result", "unknown")
    if test_result not in TEST_RESULTS:
        raise ValueError("claimed_test_result が不正です。")
    reported_at = _clean_text(payload["reported_at"], "reported_at", 64, required=True)
    try:
        dt.datetime.fromisoformat(reported_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("reported_at は ISO 8601 形式で指定してください。") from exc
    source = payload["source"]
    if not isinstance(source, dict) or set(source) - {"repo", "branch", "commit", "worktree"}:
        raise ValueError("source は repo/branch/commit/worktree のみ指定できます。")
    for field in ("repo", "branch", "commit"):
        if not _clean_text(source.get(field), f"source.{field}", MAX_SOURCE_TEXT, required=True):
            raise ValueError(f"source.{field} は必須です。")
    return {
        "schema": EVENT_SCHEMA,
        "event_id": event_id,
        "reported_at": reported_at,
        "project_key": project_key,
        "target_kind": target_kind,
        "target_id": target_id,
        "expected_revision": expected_revision,
        "claimed_status": status,
        "current_work": _clean_text(payload.get("current_work", ""), "current_work"),
        "next_action": _clean_text(payload.get("next_action", ""), "next_action"),
        "owner": _clean_text(payload.get("owner", ""), "owner", 120),
        "blocked_reason": _clean_text(payload.get("blocked_reason", ""), "blocked_reason"),
        "approval_required": approval_required,
        "approval_state": approval_state,
        "source_repo": _clean_text(source["repo"], "source.repo", MAX_SOURCE_TEXT, required=True),
        "source_branch": _clean_text(source["branch"], "source.branch", 255, required=True),
        "source_commit": _clean_text(source["commit"], "source.commit", 128, required=True),
        "source_worktree": _clean_text(
            source.get("worktree", ""), "source.worktree", MAX_SOURCE_TEXT
        ),
        "artifact_ref": _clean_text(
            payload.get("artifact_ref", ""), "artifact_ref", MAX_SOURCE_TEXT
        ),
        "test_ref": _clean_text(payload.get("test_ref", ""), "test_ref", MAX_SOURCE_TEXT),
        "claimed_test_result": test_result,
    }


def parse_progress_file(raw_text: str) -> list[dict[str, Any]]:
    """Parse a JSON object, JSON array, or JSONL file without executing its contents."""
    if len(raw_text.encode("utf-8")) > 1_000_000:
        raise ValueError("進捗ファイルは 1 MB 以下にしてください。")
    text = raw_text.strip()
    if not text:
        raise ValueError("進捗ファイルが空です。")
    try:
        parsed = json.loads(text)
        events = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        events = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL の {line_number} 行目を読めません。") from exc
    if not isinstance(events, list) or not events or len(events) > MAX_EVENTS_PER_IMPORT:
        raise ValueError(f"一度に読み込めるイベントは 1〜{MAX_EVENTS_PER_IMPORT} 件です。")
    if any(not isinstance(event, dict) for event in events):
        raise ValueError("進捗イベントは JSON オブジェクトで指定してください。")
    return events


def _entity_revision(
    conn: sqlite3.Connection, project_id: int, target_kind: str, target_id: int
) -> int:
    if target_kind == "goal":
        row = conn.execute(
            "SELECT revision FROM progress_goals WHERE id = ? AND project_id = ?",
            (target_id, project_id),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT progress_revision AS revision FROM items
            WHERE id = ? AND progress_project_id = ? AND kind = 'task'
            """,
            (target_id, project_id),
        ).fetchone()
    if row is None:
        raise LookupError(f"{target_kind} {target_id} は指定したプロジェクトにありません。")
    return int(row["revision"])


def import_progress_events(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Append validated reports idempotently; stale revisions become visible conflicts."""
    if not 1 <= len(events) <= MAX_EVENTS_PER_IMPORT:
        raise ValueError(f"一度に読み込めるイベントは 1〜{MAX_EVENTS_PER_IMPORT} 件です。")
    validated = [_validate_report(event) for event in events]
    imported: list[str] = []
    conflicts: list[str] = []
    duplicate_count = 0
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for event in validated:
            digest = _payload_hash(event)
            previous = conn.execute(
                "SELECT payload_hash FROM progress_events WHERE event_id = ?",
                (event["event_id"],),
            ).fetchone()
            if previous:
                if previous["payload_hash"] != digest:
                    raise ValueError(
                        f"event_id '{event['event_id']}' の内容が以前の報告と異なります。"
                    )
                duplicate_count += 1
                continue
            project = conn.execute(
                "SELECT id FROM progress_projects WHERE project_key = ?", (event["project_key"],)
            ).fetchone()
            if project is None:
                raise LookupError(f"プロジェクト '{event['project_key']}' はありません。")
            project_id = int(project["id"])
            target_id = event["target_id"]
            current_revision = _entity_revision(conn, project_id, event["target_kind"], target_id)
            accepted = current_revision == event["expected_revision"]
            revision_after = current_revision + 1 if accepted else None
            conflict_reason = ""
            if accepted:
                table, revision_column = (
                    ("progress_goals", "revision")
                    if event["target_kind"] == "goal"
                    else ("items", "progress_revision")
                )
                id_column = "id"
                conn.execute(
                    f"UPDATE {table} SET {revision_column} = ? WHERE {id_column} = ? "
                    f"AND {revision_column} = ?",
                    (revision_after, target_id, current_revision),
                )
                outcome = "pending_review"
            else:
                outcome = "conflict"
                conflict_reason = (
                    f"expected_revision={event['expected_revision']}, "
                    f"current_revision={current_revision}"
                )
                conflicts.append(event["event_id"])
            conn.execute(
                "UPDATE progress_projects SET updated_at = ? WHERE id = ?",
                (_now(), project_id),
            )
            _append_event(
                conn,
                event_id=event["event_id"],
                payload_hash=digest,
                event_type="report",
                project_id=project_id,
                target_kind=event["target_kind"],
                goal_id=target_id if event["target_kind"] == "goal" else None,
                task_id=target_id if event["target_kind"] == "task" else None,
                expected_revision=event["expected_revision"],
                observed_revision=current_revision,
                revision_after=revision_after,
                outcome=outcome,
                claimed_status=event["claimed_status"],
                current_work=event["current_work"],
                next_action=event["next_action"],
                owner=event["owner"],
                blocked_reason=event["blocked_reason"],
                approval_required=int(event["approval_required"]),
                approval_state=event["approval_state"],
                source_repo=event["source_repo"],
                source_branch=event["source_branch"],
                source_commit=event["source_commit"],
                source_worktree=event["source_worktree"],
                artifact_ref=event["artifact_ref"],
                test_ref=event["test_ref"],
                claimed_test_result=event["claimed_test_result"],
                conflict_reason=conflict_reason,
                reported_at=event["reported_at"],
                received_at=_now(),
            )
            imported.append(event["event_id"])
    return {
        "imported_event_ids": imported,
        "duplicates_skipped": duplicate_count,
        "conflicts": conflicts,
    }


def review_completion_claim(
    event_id: str,
    reviewer: str,
    note: str,
    *,
    artifact_checked: bool,
    test_checked: bool,
) -> str:
    """Append an explicit local review tied to the exact report snapshot."""
    clean_reviewer = _clean_text(reviewer, "reviewer", 120, required=True)
    clean_note = _clean_text(note, "review_note", 1_000, required=True)
    if artifact_checked is not True or test_checked is not True:
        raise ValueError("成果物とテストの根拠を確認してからレビューしてください。")
    now = _now()
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        claim = conn.execute(
            "SELECT * FROM progress_events WHERE event_id = ? AND event_type = 'report'",
            (event_id,),
        ).fetchone()
        if claim is None:
            raise LookupError(f"報告 {event_id} はありません。")
        if claim["outcome"] != "pending_review":
            raise ValueError("競合した報告は、そのまま verified にできません。")
        if claim["claimed_status"] != "completed":
            raise ValueError("completed の報告だけを完了レビューできます。")
        if (
            claim["claimed_test_result"] != "passed"
            or not claim["artifact_ref"]
            or not claim["test_ref"]
        ):
            raise ValueError("成果物参照、テスト参照、passed の報告が必要です。")
        prior_review = conn.execute(
            "SELECT 1 FROM progress_events WHERE event_type = 'review' AND target_event_id = ?",
            (event_id,),
        ).fetchone()
        if prior_review:
            raise ValueError("この報告には既にレビュー記録があります。")
        review_id = str(uuid.uuid4())
        _append_event(
            conn,
            event_id=review_id,
            event_type="review",
            project_id=int(claim["project_id"]),
            target_kind=claim["target_kind"],
            goal_id=claim["goal_id"],
            task_id=claim["task_id"],
            observed_revision=claim["observed_revision"],
            revision_after=claim["revision_after"],
            outcome="verified",
            claimed_status=claim["claimed_status"],
            current_work=claim["current_work"],
            next_action=claim["next_action"],
            owner=claim["owner"],
            blocked_reason=claim["blocked_reason"],
            approval_required=claim["approval_required"],
            approval_state=claim["approval_state"],
            source_repo=claim["source_repo"],
            source_branch=claim["source_branch"],
            source_commit=claim["source_commit"],
            source_worktree=claim["source_worktree"],
            artifact_ref=claim["artifact_ref"],
            test_ref=claim["test_ref"],
            claimed_test_result=claim["claimed_test_result"],
            target_event_id=event_id,
            reviewer=clean_reviewer,
            review_note=clean_note,
            reported_at=now,
            received_at=now,
        )
    return review_id


def list_project_activity(project_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """List immutable activity rows, attaching a review result to each claim."""
    query = """
        SELECT e.*, p.project_key, p.name AS project_name, g.title AS goal_title,
               COALESCE(NULLIF(i.tags, ''), i.raw_text) AS task_title,
               (SELECT r.outcome FROM progress_events r
                WHERE r.event_type = 'review' AND r.target_event_id = e.event_id
                ORDER BY r.id DESC LIMIT 1) AS review_outcome,
               (SELECT r.reviewer FROM progress_events r
                WHERE r.event_type = 'review' AND r.target_event_id = e.event_id
                ORDER BY r.id DESC LIMIT 1) AS reviewer
        FROM progress_events e
        JOIN progress_projects p ON p.id = e.project_id
        LEFT JOIN progress_goals g ON g.id = e.goal_id
        LEFT JOIN items i ON i.id = e.task_id
    """
    params: list[Any] = []
    if project_id is not None:
        query += " WHERE e.project_id = ?"
        params.append(project_id)
    query += " ORDER BY e.id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with connection() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def list_progress_focus() -> list[dict[str, Any]]:
    """Return the next action for active goals, with their latest agent report if present."""
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT g.*, p.project_key, p.name AS project_name, p.scope, p.workspace_name,
              (SELECT e.event_id FROM progress_events e
                WHERE e.project_id = p.id AND e.target_kind = 'goal' AND e.goal_id = g.id
                  AND e.event_type = 'report'
                ORDER BY e.received_at DESC, e.id DESC LIMIT 1) AS last_event_id,
              (SELECT e.received_at FROM progress_events e
                WHERE e.project_id = p.id AND e.target_kind = 'goal' AND e.goal_id = g.id
                  AND e.event_type = 'report'
                ORDER BY e.received_at DESC, e.id DESC LIMIT 1) AS last_report_at,
              (SELECT e.current_work FROM progress_events e
                WHERE e.project_id = p.id AND e.target_kind = 'goal' AND e.goal_id = g.id
                  AND e.event_type = 'report'
                ORDER BY e.received_at DESC, e.id DESC LIMIT 1) AS reported_current,
              (SELECT e.claimed_status FROM progress_events e
                WHERE e.project_id = p.id AND e.target_kind = 'goal' AND e.goal_id = g.id
                  AND e.event_type = 'report'
                ORDER BY e.received_at DESC, e.id DESC LIMIT 1) AS reported_status,
              (SELECT e.owner FROM progress_events e
                WHERE e.project_id = p.id AND e.target_kind = 'goal' AND e.goal_id = g.id
                  AND e.event_type = 'report'
                ORDER BY e.received_at DESC, e.id DESC LIMIT 1) AS reported_owner,
              (SELECT e.next_action FROM progress_events e
                WHERE e.project_id = p.id AND e.target_kind = 'goal' AND e.goal_id = g.id
                  AND e.event_type = 'report'
                ORDER BY e.received_at DESC, e.id DESC LIMIT 1) AS reported_next,
              (SELECT e.blocked_reason FROM progress_events e
                WHERE e.project_id = p.id AND e.target_kind = 'goal' AND e.goal_id = g.id
                  AND e.event_type = 'report'
                ORDER BY e.received_at DESC, e.id DESC LIMIT 1) AS reported_blocked_reason,
              (SELECT e.approval_required FROM progress_events e
                WHERE e.project_id = p.id AND e.target_kind = 'goal' AND e.goal_id = g.id
                  AND e.event_type = 'report'
                ORDER BY e.received_at DESC, e.id DESC LIMIT 1) AS reported_approval_required,
              (SELECT e.approval_state FROM progress_events e
                WHERE e.project_id = p.id AND e.target_kind = 'goal' AND e.goal_id = g.id
                  AND e.event_type = 'report'
                ORDER BY e.received_at DESC, e.id DESC LIMIT 1) AS reported_approval_state,
              (SELECT e.outcome FROM progress_events e
                WHERE e.project_id = p.id AND e.target_kind = 'goal' AND e.goal_id = g.id
                  AND e.event_type = 'report'
                ORDER BY e.received_at DESC, e.id DESC LIMIT 1) AS last_report_outcome
            FROM progress_goals g JOIN progress_projects p ON p.id = g.project_id
            WHERE g.status IN ('active', 'blocked', 'planned')
            ORDER BY CASE g.status WHEN 'blocked' THEN 0 WHEN 'active' THEN 1 ELSE 2 END,
              CASE WHEN g.next_action = '' THEN 1 ELSE 0 END, g.updated_at DESC, g.id
            """
        ).fetchall()
        return [dict(row) for row in rows]


def list_pending_completion_reviews() -> list[dict[str, Any]]:
    """Return completed agent claims that still need explicit evidence review."""
    return [
        row
        for row in list_project_activity(limit=500)
        if row["event_type"] == "report"
        and row["claimed_status"] == "completed"
        and row["outcome"] == "pending_review"
        and row["review_outcome"] is None
    ]


def is_stale(received_at: str | None, days: int = 7) -> bool:
    """Treat missing progress and reports older than the local freshness window as stale."""
    if not received_at:
        return True
    try:
        parsed = dt.datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return dt.datetime.now(dt.UTC) - parsed.astimezone(dt.UTC) > dt.timedelta(days=days)
    except ValueError:
        return True


def export_progress_jsonl(project_id: int | None = None) -> str:
    """Export the append-only local event ledger as JSONL for backup/transfer."""
    query = """
        SELECT e.*, p.project_key
        FROM progress_events e JOIN progress_projects p ON p.id = e.project_id
    """
    params: list[Any] = []
    if project_id is not None:
        query += " WHERE e.project_id = ?"
        params.append(project_id)
    query += " ORDER BY e.id"
    with connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return "".join(
            json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        )
