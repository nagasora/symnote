"""ToDo 候補（承認待ち）の検証・重複排除・承認・却下の振る舞いを保証するテスト。"""

from __future__ import annotations

import sqlite3

import pytest

from symnote.core.candidates import (
    MAX_CANDIDATES_PER_PROPOSAL,
    approve_candidate,
    count_pending_candidates,
    list_candidates,
    propose_candidates,
    reject_candidate,
    validate_candidate,
)
from symnote.core.db import SCHEMA_VERSION, fetch_tasks, init_db


@pytest.fixture()
def database(monkeypatch, tmp_path):
    """一時 DB を初期化して返す。"""
    path = tmp_path / "candidates.db"
    monkeypatch.setenv("DB_PATH", str(path))
    init_db()
    return path


def _gmail(ref: str = "msg-1", title: str = "見積書に返信する", **extra):
    """テスト用の Gmail 由来候補を作る。"""
    return validate_candidate(source="gmail", source_ref=ref, title=title, **extra)


def test_validation_normalizes_fields() -> None:
    """出典は小文字化、タイトルの空白は 1 つにまとめ、期限は ISO 形式になる。"""
    candidate = validate_candidate(
        source=" Gmail ",
        source_ref=" msg-1 ",
        title="  見積書に\n返信する ",
        due_date="2026-10-05",
        due_time="9:00",
        excerpt="x" * 5000,
    )
    assert candidate.source == "gmail"
    assert candidate.source_ref == "msg-1"
    assert candidate.title == "見積書に 返信する"
    assert candidate.due_time == "09:00"
    assert len(candidate.excerpt) == 1000


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": "fax"},
        {"source_ref": "  "},
        {"title": ""},
        {"title": "あ" * 201},
        {"source_url": "javascript:alert(1)"},
        {"due_date": "来週"},
        {"due_time": "25:00"},
        {"confidence": 1.5},
    ],
)
def test_validation_rejects_invalid_input(overrides) -> None:
    """不正な出典・空のタイトル・危険な URL・不正な日付などは拒否される。"""
    fields = {"source": "gmail", "source_ref": "msg-1", "title": "返信する"}
    fields.update(overrides)
    with pytest.raises(ValueError):
        validate_candidate(**fields)


def test_proposals_are_pending_and_not_tasks_yet(database) -> None:
    """提案された候補は承認待ちになり、タスクとしてはまだ登録されない。"""
    result = propose_candidates([_gmail(), _gmail(ref="msg-2", title="資料を送る")])

    assert len(result.created_ids) == 2
    assert count_pending_candidates() == 2
    assert fetch_tasks() == []


def test_duplicate_proposals_are_skipped_even_after_rejection(database) -> None:
    """同じ出典・タイトルの再提案は、承認待ちでも却下後でも登録されない。"""
    first = propose_candidates([_gmail()])
    assert propose_candidates([_gmail()]).duplicate_count == 1

    reject_candidate(first.created_ids[0])
    again = propose_candidates([_gmail()])

    assert again.created_ids == []
    assert again.duplicate_count == 1
    assert count_pending_candidates() == 0


def test_proposal_batch_size_is_limited(database) -> None:
    """1 回の提案件数の上限を超えると何も登録されない。"""
    batch = [_gmail(ref=f"msg-{i}") for i in range(MAX_CANDIDATES_PER_PROPOSAL + 1)]
    with pytest.raises(ValueError):
        propose_candidates(batch)
    assert count_pending_candidates() == 0


def test_approval_creates_task_with_source_and_overrides(database) -> None:
    """承認するとタスクが作られ、手直しした値と出典が反映される。"""
    candidate_id = propose_candidates(
        [_gmail(due_date="2026-10-05", source_url="https://mail.google.com/m/1", details="本文")]
    ).created_ids[0]

    task_id = approve_candidate(candidate_id, title="見積書を送る", due_date="2026-10-06")

    task = {row["id"]: row for row in fetch_tasks()}[task_id]
    assert task["tags"] == "見積書を送る"
    assert task["due_date"] == "2026-10-06"
    assert task["status"] == "inbox"
    assert "出典: gmail https://mail.google.com/m/1" in task["raw_text"]
    approved = list_candidates("approved")
    assert [(row["id"], row["task_id"]) for row in approved] == [(candidate_id, task_id)]


def test_approval_with_empty_due_date_clears_deadline(database) -> None:
    """承認時に期限日を空にすると、期限なしのタスクになる。"""
    candidate_id = propose_candidates([_gmail(due_date="2026-10-05")]).created_ids[0]

    task_id = approve_candidate(candidate_id, due_date="")

    assert {row["id"]: row for row in fetch_tasks()}[task_id]["due_date"] is None


def test_candidate_cannot_be_decided_twice(database) -> None:
    """承認済みの候補は再承認も却下もできず、タスクも増えない。"""
    candidate_id = propose_candidates([_gmail()]).created_ids[0]
    approve_candidate(candidate_id)

    with pytest.raises(ValueError):
        approve_candidate(candidate_id)
    with pytest.raises(LookupError):
        reject_candidate(candidate_id)
    assert len(fetch_tasks()) == 1


def test_failed_approval_leaves_candidate_pending(database) -> None:
    """承認時の入力が不正なら、候補は承認待ちのまま残りタスクも作られない。"""
    candidate_id = propose_candidates([_gmail()]).created_ids[0]

    with pytest.raises(ValueError):
        approve_candidate(candidate_id, due_time="25:99")

    assert count_pending_candidates() == 1
    assert fetch_tasks() == []


def test_migration_adds_candidate_table_to_existing_database(database) -> None:
    """既存 DB を開き直しても候補テーブルとスキーマ版が保たれる。"""
    init_db()
    with sqlite3.connect(database) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert "task_candidates" in tables
    assert version == SCHEMA_VERSION


def test_concurrent_approvals_create_exactly_one_task(database) -> None:
    """同じ候補を同時に承認しても、タスクは 1 件だけ作られる。"""
    import threading

    candidate_id = propose_candidates([_gmail()]).created_ids[0]
    barrier = threading.Barrier(4)
    outcomes: list[object] = []

    def approve() -> None:
        barrier.wait()
        try:
            outcomes.append(approve_candidate(candidate_id))
        except ValueError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=approve) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(isinstance(outcome, int) for outcome in outcomes) == 1
    assert len(fetch_tasks()) == 1


def test_rejected_candidate_cannot_be_approved(database) -> None:
    """却下済みの候補は承認できず、却下状態のまま残る。"""
    candidate_id = propose_candidates([_gmail()]).created_ids[0]
    reject_candidate(candidate_id)

    with pytest.raises(ValueError):
        approve_candidate(candidate_id)
    assert [row["id"] for row in list_candidates("rejected")] == [candidate_id]
    assert fetch_tasks() == []
