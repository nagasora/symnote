"""タスク完了を冪等なクエスト報酬として保存するローカルゲーム機能。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

from symnote.core.db import connection


@dataclass(frozen=True)
class PlayerStats:
    """獲得済み経験値と交換後の残高から計算するプレイヤー情報。"""

    level: int
    title: str
    xp_total: int
    xp_into_level: int
    xp_for_next: int
    gold: int
    streak_days: int
    completed_today: int


@dataclass(frozen=True)
class StreakInfo:
    """Current streak length and the automatic rest used in this ISO week."""

    streak_days: int
    rest_days_used_this_week: int


@dataclass(frozen=True)
class QuestPreview:
    """Read-only reward and effort preview for one unfinished task."""

    xp: int
    gold: int
    stars: int
    is_boss: bool


@dataclass(frozen=True)
class BonusReward:
    """An idempotent reward granted outside normal task completion."""

    kind: str
    ref: str
    xp: int
    gold: int
    created_at: str


@dataclass(frozen=True)
class QuestReward:
    """一回の完了に対する報酬と、その時点で発生した演出情報。"""

    task_id: int
    title: str
    xp: int
    gold: int
    bonuses: List[str]
    level_before: int
    level_after: int
    new_achievements: List[str]


@dataclass(frozen=True)
class Achievement:
    """未解除も含む実績の表示定義と解除日時。"""

    code: str
    title: str
    description: str
    icon: str
    unlocked_at: Optional[str]


@dataclass(frozen=True)
class Reward:
    """ユーザーが登録した交換可能なご褒美。"""

    id: int
    title: str
    cost: int
    icon: str


class InsufficientGoldError(ValueError):
    """ご褒美交換に必要なゴールドが不足している。"""


ACHIEVEMENTS: List[Tuple[str, str, str, str]] = [
    ("first_quest", "冒険のはじまり", "初めてクエストをクリアする", "🌱"),
    ("daily_5", "充実した一日", "一日に5件クリアする", "☀️"),
    ("streak_3", "小さな習慣", "3日連続でクリアする", "🔥"),
    ("streak_7", "一週間の冒険", "7日連続でクリアする", "📅"),
    ("streak_30", "続ける力", "30日連続でクリアする", "🏅"),
    ("total_10", "十の足跡", "累計10件クリアする", "👣"),
    ("total_50", "頼れる冒険者", "累計50件クリアする", "🛡️"),
    ("total_100", "百戦の勇者", "累計100件クリアする", "🏆"),
    ("early_bird", "朝の一歩", "午前9時より前にクリアする", "🐦"),
    ("on_time_10", "約束の達人", "期限内に10件クリアする", "⏰"),
]


def _level_progress(xp_total: int) -> Tuple[int, int, int]:
    """レベル L の必要経験値 50 + 25*(L-1) を累積値から差し引く。"""
    level = 1
    remaining = max(0, xp_total)
    while remaining >= 50 + 25 * (level - 1):
        remaining -= 50 + 25 * (level - 1)
        level += 1
    return level, remaining, 50 + 25 * (level - 1)


def _title_for_level(level: int) -> str:
    """到達レベルに応じた責めない冒険者の称号を返す。"""
    for threshold, title in [(30, "伝説の勇者"), (20, "星を巡る冒険者"), (10, "熟練の旅人"), (5, "駆け出しの旅人")]:
        if level >= threshold:
            return title
    return "見習い冒険者"


def _streak_details(completed_days: List[date], today: date) -> Tuple[int, set[date]]:
    """Reserve each week's first eligible rest chronologically and return the current suffix."""
    days = {day for day in completed_days if day <= today}
    if not days:
        return 0, set()
    target = today if today in days else today - timedelta(days=1)
    relevant_days = {day for day in days if day <= target}
    if not relevant_days:
        return 0, set()
    cursor = min(relevant_days)
    count = 0
    rest_days: set[date] = set()
    rest_weeks: set[Tuple[int, int]] = set()
    previous_was_completed = False
    while cursor <= target:
        if cursor in relevant_days:
            count += 1
            previous_was_completed = True
        else:
            iso = cursor.isocalendar()
            week = (iso.year, iso.week)
            if previous_was_completed and week not in rest_weeks:
                # Reserve the coupon when the blank is first encountered. A
                # later completion cannot move it to a more convenient day.
                rest_days.add(cursor)
                rest_weeks.add(week)
                count += 1
            else:
                count = 0
            # A rest day is still a blank, so the following blank cannot also
            # consume a coupon even when it belongs to another ISO week.
            previous_was_completed = False
        cursor += timedelta(days=1)
    return count, rest_days


def _streak(completed_days: List[date], today: date) -> int:
    """Keep the historical integer interface while applying automatic rest days."""
    return _streak_details(completed_days, today)[0]


def _metadata(raw: str) -> Dict[str, Any]:
    """古い空のボーナス表現も安全に読み取る。"""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _gold_balance(conn: sqlite3.Connection) -> int:
    """獲得額から交換済み総額を引いた残高を取得する。"""
    return int(conn.execute(
        "SELECT (SELECT COALESCE(SUM(gold), 0) FROM quest_log) + "
        "(SELECT COALESCE(SUM(gold), 0) FROM quest_bonus_log) + "
        "(SELECT COALESCE(SUM(bonus_gold), 0) FROM focus_sessions WHERE outcome = 'done' AND ended_at IS NOT NULL) - "
        "(SELECT COALESCE(SUM(cost), 0) FROM reward_redemptions)"
    ).fetchone()[0])


def _stats(conn: sqlite3.Connection, today: date) -> PlayerStats:
    """接続内の同一スナップショットからプレイヤー情報を組み立てる。"""
    xp_total = int(conn.execute(
        "SELECT (SELECT COALESCE(SUM(xp), 0) FROM quest_log) + "
        "(SELECT COALESCE(SUM(xp), 0) FROM quest_bonus_log) + "
        "(SELECT COALESCE(SUM(bonus_xp), 0) FROM focus_sessions WHERE outcome = 'done' AND ended_at IS NOT NULL)"
    ).fetchone()[0])
    days = [date.fromisoformat(row[0]) for row in conn.execute(
        "SELECT substr(completed_at, 1, 10) FROM quest_log"
    )]
    level, progress, needed = _level_progress(xp_total)
    return PlayerStats(level, _title_for_level(level), xp_total, progress, needed,
                       _gold_balance(conn), _streak(days, today), days.count(today))


def get_player_stats(today: Optional[date] = None) -> PlayerStats:
    """全完了履歴から情報を返す。完了の取り消しでも経験値は返還しない。"""
    with connection() as conn, conn:
        conn.execute("BEGIN")
        return _stats(conn, today or date.today())


def streak_info(today: Optional[date] = None) -> StreakInfo:
    """Return the current streak and rest ticket usage without changing state."""
    target = today or date.today()
    with connection() as conn:
        days = [date.fromisoformat(row[0]) for row in conn.execute(
            "SELECT substr(completed_at, 1, 10) FROM quest_log"
        )]
    streak_days, rest_days = _streak_details(days, target)
    target_iso = target.isocalendar()
    used = sum(
        day.isocalendar().year == target_iso.year and day.isocalendar().week == target_iso.week
        for day in rest_days
    )
    return StreakInfo(streak_days, used)


def _calculate_reward(task: Mapping[str, Any], completed: datetime) -> Tuple[int, int, List[str], bool]:
    """基本10×重要度係数に所要時間加点し、期限内なら全体を1.3倍する。"""
    importance = min(5, max(1, int(task.get("importance") or 1)))
    xp = 10 * (0.8 + 0.2 * importance)
    bonuses: List[str] = []
    minutes = task.get("estimate_minutes")
    effort = str(task.get("effort") or "short").lower()
    extra = min(30, max(0, int(minutes)) // 15 * 3) if minutes else {"long": 10, "medium": 5}.get(effort, 0)
    if extra:
        xp += extra
        bonuses.append(f"じっくり挑戦 +{extra}XP")
    on_time = False
    if task.get("due_date"):
        try:
            on_time = completed.date() <= date.fromisoformat(str(task["due_date"]))
        except ValueError:
            pass
    if on_time:
        xp *= 1.3
        bonuses.append("期限内クリア +30%")
    earned = max(1, int(xp + 0.5))
    return earned, max(1, (earned + 1) // 2), bonuses, on_time


def quest_preview(task: Mapping[str, Any], now: datetime) -> QuestPreview:
    """Calculate the reward and weight shown before completion without writing."""
    xp, gold, _, _ = _calculate_reward(task, now)
    minutes = task.get("estimate_minutes")
    stars: int
    if minutes is not None:
        try:
            estimate = int(minutes)
        except (TypeError, ValueError):
            estimate = 0
        stars = 1 if estimate <= 15 else 2 if estimate <= 60 else 3
    else:
        stars = {"medium": 2, "long": 3}.get(str(task.get("effort") or "short").lower(), 1)
    return QuestPreview(xp, gold, stars, stars == 3)


def _unlock_achievements(conn: sqlite3.Connection, completed: datetime) -> List[str]:
    """完了後の履歴から条件を判定し、新規解除コードだけを返す。"""
    stats = _stats(conn, completed.date())
    rows = conn.execute("SELECT bonus FROM quest_log").fetchall()
    total = len(rows)
    timely = sum(bool(_metadata(row[0]).get("on_time")) for row in rows)
    eligible = {
        "first_quest": total >= 1,
        "daily_5": stats.completed_today >= 5,
        "early_bird": completed.hour < 9,
        "on_time_10": timely >= 10,
    }
    for threshold in (3, 7, 30):
        eligible[f"streak_{threshold}"] = stats.streak_days >= threshold
    for threshold in (10, 50, 100):
        eligible[f"total_{threshold}"] = total >= threshold
    unlocked: List[str] = []
    for code, _, _, _ in ACHIEVEMENTS:
        if eligible.get(code):
            cursor = conn.execute("INSERT OR IGNORE INTO achievements(code, unlocked_at) VALUES (?, ?)",
                                  (code, completed.isoformat(timespec="seconds")))
            if cursor.rowcount:
                unlocked.append(code)
    return unlocked


def _quest_title(task: Mapping[str, Any]) -> str:
    """祝賀・履歴に出す短い題名。本文全体（追記された進捗メモ等）は載せない。"""
    tags = str(task.get("tags") or "").strip()
    first_line = next(iter(str(task.get("raw_text") or "").strip().splitlines()), "")
    return (tags or first_line or "クエスト")[:120]


def award_for_completion_in_transaction(conn: sqlite3.Connection, task_row: Mapping[str, Any]) -> Optional[QuestReward]:
    """呼び出し元の書込トランザクション内で一度だけ付与する。取り消し時も返還しない。

    items の完了更新と同じ接続で呼ぶ。ここでは commit せず、失敗時は
    完了・報酬・実績すべてを呼び出し元でロールバックできるようにする。
    """
    task = dict(task_row)
    task_id = int(task["id"])
    if task.get("kind") != "task" or conn.execute("SELECT 1 FROM quest_log WHERE task_id = ?", (task_id,)).fetchone():
        return None
    completed = datetime.fromisoformat(task["completed_at"]) if task.get("completed_at") else datetime.now()
    before = _stats(conn, completed.date()).level
    xp, gold, bonuses, on_time = _calculate_reward(task, completed)
    metadata: Dict[str, Any] = {"on_time": on_time}
    cursor = conn.execute(
        "INSERT OR IGNORE INTO quest_log(task_id, title, xp, gold, bonus, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, _quest_title(task), xp, gold, json.dumps(metadata), completed.isoformat(timespec="seconds")),
    )
    if not cursor.rowcount:
        return None
    after = _stats(conn, completed.date())
    if after.streak_days >= 3:
        bonuses.append(f"連続{after.streak_days}日")
    achievements = _unlock_achievements(conn, completed)
    metadata.update(bonuses=bonuses, level_before=before, level_after=after.level, new_achievements=achievements)
    conn.execute("UPDATE quest_log SET bonus = ? WHERE task_id = ?", (json.dumps(metadata, ensure_ascii=False), task_id))
    return QuestReward(task_id, _quest_title(task), xp, gold, bonuses, before, after.level, achievements)


def _reward_from_row(row: Mapping[str, Any]) -> QuestReward:
    """Rebuild the completion-time reward response from persisted metadata."""
    meta = _metadata(str(row["bonus"]))
    return QuestReward(
        int(row["task_id"]),
        str(row["title"]),
        int(row["xp"]),
        int(row["gold"]),
        list(meta.get("bonuses", [])),
        int(meta.get("level_before", 1)),
        int(meta.get("level_after", 1)),
        list(meta.get("new_achievements", [])),
    )


def reward_for_task(task_id: int) -> Optional[QuestReward]:
    """Read one persisted task reward without consuming its unseen marker."""
    with connection() as conn:
        row = conn.execute("SELECT * FROM quest_log WHERE task_id = ?", (task_id,)).fetchone()
    return None if row is None else _reward_from_row(row)


def mark_reward_seen(task_id: int) -> bool:
    """Atomically claim a reward for presentation; return false if already claimed."""
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE quest_log SET seen = 1 WHERE task_id = ? AND seen = 0", (task_id,)
        )
        return bool(cursor.rowcount)


def _bonus_from_row(row: Mapping[str, Any]) -> BonusReward:
    return BonusReward(
        str(row["kind"]), str(row["ref"]), int(row["xp"]), int(row["gold"]),
        str(row["created_at"]),
    )


def award_daily_clear_if_eligible(today: date) -> Optional[BonusReward]:
    """Award the once-per-day chest after the final eligible task is complete."""
    day = today.isoformat()
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        unfinished = conn.execute(
            """
            SELECT COUNT(*) FROM items
            WHERE kind = 'task'
              AND status != 'done'
              AND (status = 'today' OR (due_date IS NOT NULL AND due_date <= ?))
            """,
            (day,),
        ).fetchone()[0]
        completed = conn.execute(
            "SELECT COUNT(*) FROM quest_log WHERE substr(completed_at, 1, 10) = ?",
            (day,),
        ).fetchone()[0]
        if int(unfinished) != 0 or int(completed) == 0:
            return None
        created_at = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO quest_bonus_log(kind, ref, xp, gold, created_at)
            VALUES ('daily_clear', ?, 20, 10, ?)
            """,
            (day, created_at),
        )
        if not cursor.rowcount:
            return None
        row = conn.execute(
            "SELECT * FROM quest_bonus_log WHERE kind = 'daily_clear' AND ref = ?", (day,)
        ).fetchone()
        return _bonus_from_row(row)


def award_triage(ref: str, *, now: Optional[datetime] = None) -> Optional[BonusReward]:
    """Award up to twenty unique triage actions per local calendar day."""
    normalized = str(ref).strip()
    if not normalized:
        raise ValueError("triage ref must not be empty")
    awarded_at = now or datetime.now()
    day = awarded_at.date().isoformat()
    created_at = awarded_at.isoformat(timespec="seconds")
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM quest_bonus_log WHERE kind = 'triage' AND ref = ?", (normalized,)
        ).fetchone()
        if existing is not None:
            return None
        count = conn.execute(
            "SELECT COUNT(*) FROM quest_bonus_log "
            "WHERE kind = 'triage' AND substr(created_at, 1, 10) = ?",
            (day,),
        ).fetchone()[0]
        if int(count) >= 20:
            return None
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO quest_bonus_log(kind, ref, xp, gold, created_at)
            VALUES ('triage', ?, 2, 0, ?)
            """,
            (normalized, created_at),
        )
        if not cursor.rowcount:
            return None
        row = conn.execute(
            "SELECT * FROM quest_bonus_log WHERE kind = 'triage' AND ref = ?", (normalized,)
        ).fetchone()
        return _bonus_from_row(row)


def quest_history(days: int = 28, *, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Return daily completion counts and inferred rest days for footprints."""
    span = max(1, min(int(days), 366))
    end = today or date.today()
    start = end - timedelta(days=span - 1)
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT substr(completed_at, 1, 10) AS day, COUNT(*) AS count
            FROM quest_log
            WHERE substr(completed_at, 1, 10) <= ?
            GROUP BY substr(completed_at, 1, 10)
            """,
            (end.isoformat(),),
        ).fetchall()
    counts = {date.fromisoformat(row["day"]): int(row["count"]) for row in rows}
    _, rest_days = _streak_details(list(counts), end)
    return [
        {
            "day": (current := start + timedelta(days=offset)).isoformat(),
            "count": counts.get(current, 0),
            "rest_day": current in rest_days,
        }
        for offset in range(span)
    ]


def pop_unseen_rewards() -> List[QuestReward]:
    """未表示報酬を原子的に取り出して既読にし、再実行による二重演出を防ぐ。"""
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT * FROM quest_log WHERE seen = 0 ORDER BY id").fetchall()
        rewards: List[QuestReward] = []
        for row in rows:
            rewards.append(_reward_from_row(row))
        conn.execute("UPDATE quest_log SET seen = 1 WHERE seen = 0")
        return rewards


def list_rewards() -> List[Reward]:
    """交換可能な有効なご褒美を登録順で返す。"""
    with connection() as conn:
        return [Reward(row["id"], row["title"], row["cost"], row["icon"])
                for row in conn.execute("SELECT * FROM rewards WHERE active = 1 ORDER BY id")]


def add_reward(title: str, cost: int, icon: str = "🎁") -> int:
    """空でない名前と正の整数価格のご褒美を登録し、IDを返す。"""
    if not title.strip() or not isinstance(cost, int) or isinstance(cost, bool) or cost <= 0:
        raise ValueError("ご褒美の名前と1G以上の整数価格を入力してください。")
    with connection() as conn, conn:
        cursor = conn.execute("INSERT INTO rewards(title, cost, icon, created_at) VALUES (?, ?, ?, ?)",
                              (title.strip(), cost, icon.strip() or "🎁", datetime.now().isoformat(timespec="seconds")))
        return int(cursor.lastrowid)


def redeem_reward(reward_id: int) -> int:
    """残高確認と交換記録を直列化し、多重操作でも残高を負にせず返す。"""
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM rewards WHERE id = ? AND active = 1", (reward_id,)).fetchone()
        if row is None:
            raise ValueError("交換できるご褒美が見つかりません。")
        balance = _gold_balance(conn)
        if balance < row["cost"]:
            raise InsufficientGoldError("ゴールドを貯めて、また交換しに来てください。")
        conn.execute("INSERT INTO reward_redemptions(reward_id, title, cost, redeemed_at) VALUES (?, ?, ?, ?)",
                     (reward_id, row["title"], row["cost"], datetime.now().isoformat(timespec="seconds")))
        return balance - row["cost"]


def list_achievements() -> List[Achievement]:
    """全実績を返し、未解除の解除日時は None にする。"""
    with connection() as conn:
        unlocked = {row["code"]: row["unlocked_at"] for row in conn.execute("SELECT * FROM achievements")}
    return [Achievement(code, title, description, icon, unlocked.get(code))
            for code, title, description, icon in ACHIEVEMENTS]


def list_recent_quests(limit: int = 20) -> List[Dict[str, Any]]:
    """直近のクエスト履歴を新しい順で返す。"""
    with connection() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM quest_log ORDER BY id DESC LIMIT ?", (max(0, limit),))]
