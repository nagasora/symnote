"""集中セッションの開始・終了・ボーナスと日別集計を管理する。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from operator import attrgetter
from typing import List, Optional, Tuple

from symnote.core.db import _complete_task_in_transaction, connection
from symnote.core.quest import QuestReward, _stats

ALLOWED_MINUTES: Tuple[int, ...] = (15, 25, 50)
BONUS_REQUIRED_RATIO: float = 0.8
FOCUS_BONUS_XP: int = 10
FOCUS_BONUS_GOLD: int = 5


@dataclass(frozen=True)
class FocusSession:
    """開始時点のタスク名と予定時間を保持する集中記録。"""

    id: int
    task_id: Optional[int]
    title: str
    planned_minutes: int
    started_at: datetime
    ended_at: Optional[datetime]
    outcome: Optional[str]
    bonus_xp: int
    bonus_gold: int


@dataclass(frozen=True)
class FocusResult:
    """確定済みセッションと、今回だけ表示する追加報酬を返す。"""

    session: FocusSession
    bonus_reward: Optional[QuestReward]
    next_task_id: Optional[int] = None


@dataclass(frozen=True)
class FocusSummary:
    """当日の集中分数・終了件数・直近の連続完了件数。"""

    total_minutes: int
    completed_sessions: int
    consecutive_sessions: int


class FocusSessionActiveError(ValueError):
    """別の集中が進行中なので新たな開始を受け付けない。"""


class FocusSessionEndedError(ValueError):
    """終了済みセッションへの再操作を受け付けない。"""


def _local_now(now: Optional[datetime]) -> datetime:
    """注入時刻をDBと同じローカルのタイムゾーンなし日時に揃える。"""
    value = now if now is not None else datetime.now()
    return value.astimezone().replace(tzinfo=None) if value.tzinfo is not None else value


def _session(row: sqlite3.Row) -> FocusSession:
    """DB行の日時を解析して不変のセッションを構築する。"""
    return FocusSession(
        id=row["id"],
        task_id=row["task_id"],
        title=row["title"],
        planned_minutes=row["planned_minutes"],
        started_at=datetime.fromisoformat(row["started_at"]),
        ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
        outcome=row["outcome"],
        bonus_xp=row["bonus_xp"],
        bonus_gold=row["bonus_gold"],
    )


def start_session(task_id: int, minutes: int, *, now: Optional[datetime] = None) -> int:
    """未完了タスクの集中を開始する。進行中なら例外とし、同時開始を直列化する。"""
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes not in ALLOWED_MINUTES:
        raise ValueError("集中時間は15・25・50分から選んでください。")
    started = _local_now(now)
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM focus_sessions WHERE ended_at IS NULL LIMIT 1").fetchone():
            raise FocusSessionActiveError("進行中の集中を終えてから、次を始められます。")
        task = conn.execute(
            "SELECT * FROM items WHERE id = ? AND kind = 'task' AND status != 'done'", (task_id,)
        ).fetchone()
        if task is None:
            raise ValueError("集中する未完了タスクが見つかりません。")
        cursor = conn.execute(
            "INSERT INTO focus_sessions(task_id, title, planned_minutes, started_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, task["raw_text"], minutes, started.isoformat()),
        )
        return int(cursor.lastrowid)


def active_session() -> Optional[FocusSession]:
    """進行中の集中を返し、なければNoneを返す。期限到達だけでは終了しない。"""
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM focus_sessions WHERE ended_at IS NULL ORDER BY id LIMIT 1"
        ).fetchone()
        return _session(row) if row is not None else None


def _end_session(
    session_id: int, *, stopped: bool, complete_task: bool, now: Optional[datetime]
) -> FocusResult:
    """終了・通常報酬・追加報酬を同一トランザクションで一度だけ確定する。"""
    ended = _local_now(now)
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM focus_sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise ValueError("集中セッションが見つかりません。")
        session = _session(row)
        if session.ended_at is not None:
            raise FocusSessionEndedError("この集中はすでに終了しています。")
        if ended < session.started_at:
            raise ValueError("終了時刻は開始時刻以降にしてください。")
        eligible = not stopped and (ended - session.started_at).total_seconds() >= (
            session.planned_minutes * 60 * BONUS_REQUIRED_RATIO
        )
        xp, gold = (FOCUS_BONUS_XP, FOCUS_BONUS_GOLD) if eligible else (0, 0)
        next_task_id: Optional[int] = None
        if complete_task:
            task = conn.execute(
                "SELECT kind FROM items WHERE id = ?", (session.task_id,)
            ).fetchone()
            if session.task_id is None or task is None or task["kind"] != "task":
                raise ValueError("タスクが削除されています。中断して通常画面に戻れます。")
            # 公開APIは別接続でcommitするため、既存の接続内完了経路を使って原子性を保つ。
            next_task_id = _complete_task_in_transaction(conn, session.task_id)
        before = _stats(conn, ended.date()).level
        updated = conn.execute(
            "UPDATE focus_sessions SET ended_at = ?, outcome = ?, bonus_xp = ?, bonus_gold = ? "
            "WHERE id = ? AND ended_at IS NULL",
            (ended.isoformat(), "stopped" if stopped else "done", xp, gold, session_id),
        )
        if not updated.rowcount:
            raise FocusSessionEndedError("この集中はすでに終了しています。")
        bonus_reward = (
            QuestReward(
                session.task_id or 0,
                session.title,
                xp,
                gold,
                [f"フォーカスボーナス +{xp}XP / +{gold}G"],
                before,
                _stats(conn, ended.date()).level,
                [],
            )
            if eligible
            else None
        )
        saved = conn.execute("SELECT * FROM focus_sessions WHERE id = ?", (session_id,)).fetchone()
        return FocusResult(_session(saved), bonus_reward, next_task_id)


def finish_session(
    session_id: int, *, complete_task: bool, now: Optional[datetime] = None
) -> FocusResult:
    """集中を完了し、予定の80%以上なら固定ボーナスを付与する。

    complete_task=Trueなら繰り返し生成を含む既存のタスク完了経路も通す。
    Falseはタスクを未完了のまま集中だけ終了する。短時間でも責めずdoneを記録する。
    bonus_rewardは通常報酬とは別にUIから一度だけ祝賀表示へ渡す。
    """
    return _end_session(session_id, stopped=False, complete_task=complete_task, now=now)


def stop_session(session_id: int, *, now: Optional[datetime] = None) -> FocusResult:
    """途中までの集中をstoppedで記録し、タスク完了もボーナス付与もしない。"""
    return _end_session(session_id, stopped=True, complete_task=False, now=now)


def today_focus_summary(today: date, *, now: Optional[datetime] = None) -> FocusSummary:
    """当日と重なる実測秒を合算して分に切り捨て、当日終了の完了数を返す。

    放置時間の水増しを避けて各セッションは予定時間で打ち切る。進行中・中断も
    集中分数に含み、日跨ぎは当日分だけ算入する。連続数は当日の直近中断以降。
    """
    current = _local_now(now)
    day_start = datetime.combine(today, time.min)
    day_end = day_start + timedelta(days=1)
    with connection() as conn:
        sessions: List[FocusSession] = [
            _session(row)
            for row in conn.execute(
                "SELECT * FROM focus_sessions WHERE started_at < ? "
                "AND (ended_at IS NULL OR ended_at >= ?) ORDER BY started_at, id",
                (day_end.isoformat(), day_start.isoformat()),
            )
        ]
    seconds = 0.0
    completed = consecutive = 0
    for session in sessions:
        end = min(
            session.ended_at or current,
            session.started_at + timedelta(minutes=session.planned_minutes),
            day_end,
            current,
        )
        seconds += max(0.0, (end - max(session.started_at, day_start)).total_seconds())
    finished = sorted(
        (
            session
            for session in sessions
            if session.ended_at is not None
            and day_start <= session.ended_at < day_end
            and session.ended_at <= current
        ),
        key=attrgetter("ended_at", "id"),
    )
    for session in finished:
        if session.outcome == "done":
            completed += 1
            consecutive += 1
        else:
            consecutive = 0
    return FocusSummary(int(seconds // 60), completed, consecutive)
