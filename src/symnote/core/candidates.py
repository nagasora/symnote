"""外部 AI が提案する ToDo 候補の保存・承認・却下を扱うモジュール。

Gmail / Slack / Notion などから抽出された候補は信頼できない入力として扱い、
人が承認するまで ``items`` には登録しない。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from symnote.core.db import connection, insert_item_in_transaction

CandidateRow = Dict[str, Any]

ALLOWED_SOURCES = ("gmail", "slack", "notion", "chat", "calendar", "other")
CANDIDATE_STATUSES = ("pending", "approved", "rejected")
MAX_CANDIDATES_PER_PROPOSAL = 20
MAX_TITLE_LENGTH = 200
MAX_DETAILS_LENGTH = 2000
MAX_EXCERPT_LENGTH = 1000
MAX_SOURCE_REF_LENGTH = 500
MAX_SOURCE_URL_LENGTH = 2000


@dataclass(frozen=True)
class CandidateInput:
    """検証済みの ToDo 候補。"""

    source: str
    source_ref: str
    title: str
    details: str = ""
    excerpt: str = ""
    source_url: Optional[str] = None
    due_date: Optional[str] = None
    due_time: Optional[str] = None
    confidence: Optional[float] = None


@dataclass(frozen=True)
class ProposalResult:
    """候補登録の結果。重複は既存候補として数える。"""

    created_ids: List[int]
    duplicate_count: int


def _normalize_whitespace(value: str) -> str:
    """連続する空白を 1 つにまとめ、前後の空白を取り除く。"""
    return re.sub(r"\s+", " ", value).strip()


def normalize_due_date(value: Optional[str]) -> Optional[str]:
    """期限日を ISO 形式に正規化する。空なら None を返す。"""
    if value is None or not value.strip():
        return None
    try:
        return dt.date.fromisoformat(value.strip()).isoformat()
    except ValueError as exc:
        raise ValueError(f"due_date は YYYY-MM-DD 形式で指定してください: {value!r}") from exc


def normalize_due_time(value: Optional[str]) -> Optional[str]:
    """期限時刻を HH:MM に正規化する。空なら None を返す。"""
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        return dt.time.fromisoformat(text).strftime("%H:%M")
    except ValueError:
        pass
    try:
        return dt.datetime.strptime(text, "%H:%M").strftime("%H:%M")
    except ValueError as exc:
        raise ValueError(f"due_time は HH:MM 形式で指定してください: {value!r}") from exc


def validate_candidate(
    source: str,
    source_ref: str,
    title: str,
    details: str = "",
    excerpt: str = "",
    source_url: Optional[str] = None,
    due_date: Optional[str] = None,
    due_time: Optional[str] = None,
    confidence: Optional[float] = None,
) -> CandidateInput:
    """生の入力を検証・正規化し、不正なら ValueError を送出する。

    長文になりがちな ``details`` と ``excerpt`` は上限で切り詰め、
    識別に使う項目（出典・タイトル・URL）は上限超過を拒否する。
    """
    normalized_source = source.strip().lower()
    if normalized_source not in ALLOWED_SOURCES:
        raise ValueError(f"source は {', '.join(ALLOWED_SOURCES)} のいずれかです: {source!r}")
    normalized_ref = source_ref.strip()
    if not normalized_ref:
        raise ValueError("source_ref（メール ID やメッセージ URL など）は必須です。")
    if len(normalized_ref) > MAX_SOURCE_REF_LENGTH:
        raise ValueError(f"source_ref は {MAX_SOURCE_REF_LENGTH} 文字以内にしてください。")
    normalized_title = _normalize_whitespace(title)
    if not normalized_title:
        raise ValueError("title は必須です。")
    if len(normalized_title) > MAX_TITLE_LENGTH:
        raise ValueError(f"title は {MAX_TITLE_LENGTH} 文字以内にしてください。")
    normalized_url: Optional[str] = None
    if source_url and source_url.strip():
        normalized_url = source_url.strip()
        if not re.match(r"^https?://", normalized_url, flags=re.IGNORECASE):
            raise ValueError("source_url は http(s) の URL にしてください。")
        if len(normalized_url) > MAX_SOURCE_URL_LENGTH:
            raise ValueError(f"source_url は {MAX_SOURCE_URL_LENGTH} 文字以内にしてください。")
    if confidence is not None and not 0.0 <= float(confidence) <= 1.0:
        raise ValueError("confidence は 0.0〜1.0 で指定してください。")
    return CandidateInput(
        source=normalized_source,
        source_ref=normalized_ref,
        title=normalized_title,
        details=details.strip()[:MAX_DETAILS_LENGTH],
        excerpt=excerpt.strip()[:MAX_EXCERPT_LENGTH],
        source_url=normalized_url,
        due_date=normalize_due_date(due_date),
        due_time=normalize_due_time(due_time),
        confidence=None if confidence is None else float(confidence),
    )


def propose_candidates(candidates: Sequence[CandidateInput]) -> ProposalResult:
    """候補をまとめて承認待ちに登録する。同じ出典・タイトルの候補は重複として無視する。"""
    if len(candidates) > MAX_CANDIDATES_PER_PROPOSAL:
        raise ValueError(f"一度に提案できる候補は {MAX_CANDIDATES_PER_PROPOSAL} 件までです。")
    now = dt.datetime.now().isoformat(timespec="seconds")
    created: List[int] = []
    duplicates = 0
    with connection() as conn, conn:
        for candidate in candidates:
            cur = conn.execute(
                """
                INSERT INTO task_candidates (
                  created_at, source, source_ref, source_url, title, details,
                  excerpt, due_date, due_time, confidence, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                ON CONFLICT(source, source_ref, title) DO NOTHING
                """,
                (
                    now,
                    candidate.source,
                    candidate.source_ref,
                    candidate.source_url,
                    candidate.title,
                    candidate.details,
                    candidate.excerpt,
                    candidate.due_date,
                    candidate.due_time,
                    candidate.confidence,
                ),
            )
            if cur.rowcount:
                created.append(int(cur.lastrowid))
            else:
                duplicates += 1
    return ProposalResult(created_ids=created, duplicate_count=duplicates)


def list_candidates(status: str = "pending", limit: int = 50) -> List[CandidateRow]:
    """指定状態の候補を新しい順に返す。"""
    if status not in CANDIDATE_STATUSES:
        raise ValueError(f"status は {', '.join(CANDIDATE_STATUSES)} のいずれかです。")
    with connection() as conn:
        cur = conn.execute(
            """
            SELECT id, created_at, source, source_ref, source_url, title, details,
                   excerpt, due_date, due_time, confidence, status, decided_at, task_id
            FROM task_candidates
            WHERE status = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (status, max(1, min(int(limit), 200))),
        )
        return [dict(row) for row in cur.fetchall()]


def count_pending_candidates() -> int:
    """承認待ちの候補数を返す。"""
    with connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM task_candidates WHERE status = 'pending'"
        ).fetchone()
        return int(row[0])


def _task_text_from_candidate(candidate: CandidateRow, details: Optional[str]) -> str:
    """承認時に保存するタスク本文を、詳細と出典から組み立てる。"""
    body = (details if details is not None else candidate["details"]).strip()
    body = body or candidate["title"]
    origin = candidate["source_url"] or candidate["source_ref"]
    return f"{body}\n\n出典: {candidate['source']} {origin}"


def approve_candidate(
    candidate_id: int,
    title: Optional[str] = None,
    details: Optional[str] = None,
    due_date: Optional[str] = None,
    due_time: Optional[str] = None,
) -> int:
    """候補を承認してタスクを登録し、その ID を返す。

    引数で渡した値は候補の内容より優先する（承認時の手直し用）。
    候補の状態更新とタスク登録は 1 トランザクションで行う。
    """
    final_due_date = normalize_due_date(due_date) if due_date is not None else None
    final_due_time = normalize_due_time(due_time) if due_time is not None else None
    with connection() as conn, conn:
        row = conn.execute(
            "SELECT * FROM task_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"候補 {candidate_id} は存在しません。")
        candidate = dict(row)
        if candidate["status"] != "pending":
            raise ValueError(f"候補 {candidate_id} は既に {candidate['status']} です。")
        final_title = _normalize_whitespace(title) if title is not None else candidate["title"]
        if not final_title:
            raise ValueError("タイトルは空にできません。")
        task_id = insert_item_in_transaction(
            conn,
            kind="task",
            raw_text=_task_text_from_candidate(candidate, details),
            tags=final_title,
            due_date=final_due_date if due_date is not None else candidate["due_date"],
            due_time=final_due_time if due_time is not None else candidate["due_time"],
            status="inbox",
            ai_category="task",
            importance=3,
            urgency=3,
        )
        conn.execute(
            """
            UPDATE task_candidates
            SET status = 'approved', decided_at = ?, task_id = ?
            WHERE id = ?
            """,
            (dt.datetime.now().isoformat(timespec="seconds"), task_id, candidate_id),
        )
        return task_id


def reject_candidate(candidate_id: int) -> None:
    """候補を却下する。却下した候補は同じ出典から再提案されても登録されない。"""
    with connection() as conn, conn:
        cur = conn.execute(
            """
            UPDATE task_candidates
            SET status = 'rejected', decided_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (dt.datetime.now().isoformat(timespec="seconds"), candidate_id),
        )
        if cur.rowcount == 0:
            raise LookupError(f"承認待ちの候補 {candidate_id} は存在しません。")
