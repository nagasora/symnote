"""Small standard-library reporter for any local coding agent.

This writes inert JSONL data only. It never connects to a service, opens a
database, executes a test reference, or interprets a reported command.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

EVENT_SCHEMA = "symnote.progress-event.v1"
APPROVAL_STATES = ("not_required", "pending", "approved", "rejected")
PROGRESS_STATUSES = ("not_started", "in_progress", "blocked", "completed", "needs_approval")
TEST_RESULTS = ("passed", "failed", "not_run", "unknown")


def build_event(
    *,
    project_key: str,
    target_kind: str,
    target_id: int,
    expected_revision: int,
    claimed_status: str,
    source_repo: str,
    source_branch: str,
    source_commit: str,
    source_worktree: str = "",
    current_work: str = "",
    next_action: str = "",
    owner: str = "",
    blocked_reason: str = "",
    approval_required: bool = False,
    approval_state: str = "not_required",
    artifact_ref: str = "",
    test_ref: str = "",
    claimed_test_result: str = "unknown",
) -> dict[str, object]:
    """Build one v1 event with a stable ID for safe repeat import."""
    if target_kind not in {"goal", "task"} or target_id <= 0:
        raise ValueError("target must be a goal/task and a positive id")
    if expected_revision < 0:
        raise ValueError("expected_revision must be 0 or greater")
    if claimed_status not in PROGRESS_STATUSES:
        raise ValueError("unsupported claimed_status")
    if approval_state not in APPROVAL_STATES:
        raise ValueError("unsupported approval_state")
    if claimed_test_result not in TEST_RESULTS:
        raise ValueError("unsupported claimed_test_result")
    if not project_key or not source_repo or not source_branch or not source_commit:
        raise ValueError("project_key, source_repo, source_branch, and source_commit are required")
    if type(approval_required) is not bool:
        raise ValueError("approval_required must be a boolean")
    timestamp = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "schema": EVENT_SCHEMA,
        "event_id": str(uuid.uuid4()),
        "reported_at": timestamp,
        "project_key": project_key,
        "target": {"kind": target_kind, "id": target_id},
        "expected_revision": expected_revision,
        "claimed_status": claimed_status,
        "current_work": current_work,
        "next_action": next_action,
        "owner": owner,
        "blocked_reason": blocked_reason,
        "approval_required": approval_required,
        "approval_state": approval_state,
        "source": {
            "repo": source_repo,
            "branch": source_branch,
            "commit": source_commit,
            "worktree": source_worktree,
        },
        "artifact_ref": artifact_ref,
        "test_ref": test_ref,
        "claimed_test_result": claimed_test_result,
    }


def append_event(output_path: str | Path, event: dict[str, object]) -> Path:
    """Append one compact JSON record; the caller can reimport the file idempotently."""
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        data = record.encode("utf-8")
        written = os.write(descriptor, data)
        if written != len(data):
            raise OSError("incomplete event write")
    finally:
        os.close(descriptor)
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Append one SymNote progress report as local JSONL data."
    )
    parser.add_argument("--output", required=True, help="JSONL report file; choose a local path")
    parser.add_argument("--project-key", required=True)
    parser.add_argument("--target-kind", required=True, choices=("goal", "task"))
    parser.add_argument("--target-id", required=True, type=int)
    parser.add_argument("--expected-revision", required=True, type=int)
    parser.add_argument("--status", required=True, choices=PROGRESS_STATUSES)
    parser.add_argument("--current", default="")
    parser.add_argument("--next-action", default="")
    parser.add_argument("--owner", default="")
    parser.add_argument("--blocked-reason", default="")
    parser.add_argument("--approval-required", action="store_true")
    parser.add_argument("--approval-state", choices=APPROVAL_STATES, default="not_required")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--worktree", default="")
    parser.add_argument("--artifact-ref", default="")
    parser.add_argument("--test-ref", default="")
    parser.add_argument("--test-result", choices=TEST_RESULTS, default="unknown")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Write a claim file for later review/import; no references are executed."""
    args = _parser().parse_args(argv)
    try:
        event = build_event(
            project_key=args.project_key,
            target_kind=args.target_kind,
            target_id=args.target_id,
            expected_revision=args.expected_revision,
            claimed_status=args.status,
            current_work=args.current,
            next_action=args.next_action,
            owner=args.owner,
            blocked_reason=args.blocked_reason,
            approval_required=args.approval_required,
            approval_state=args.approval_state,
            source_repo=args.repo,
            source_branch=args.branch,
            source_commit=args.commit,
            source_worktree=args.worktree,
            artifact_ref=args.artifact_ref,
            test_ref=args.test_ref,
            claimed_test_result=args.test_result,
        )
        destination = append_event(args.output, event)
    except (OSError, ValueError) as exc:
        print(f"SymNote reporter: {exc}", file=sys.stderr)
        return 2
    print(f"Appended event {event['event_id']} to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
