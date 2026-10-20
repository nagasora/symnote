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


def _streak(completed_days: List[date], today: date) -> int:
    """今日未完了でも昨日から続く連続完了日数を維持する。"""
    days = set(completed_days)
    cursor = today if today in days else today - timedelta(days=1)
    count = 0
    while cursor in days:
        count += 1
        cursor -= timedelta(days=1)
    return count


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
        "(SELECT COALESCE(SUM(bonus_gold), 0) FROM focus_sessions WHERE outcome = 'done' AND ended_at IS NOT NULL) - "
        "(SELECT COALESCE(SUM(cost), 0) FROM reward_redemptions)"
    ).fetchone()[0])


def _stats(conn: sqlite3.Connection, today: date) -> PlayerStats:
    """接続内の同一スナップショットからプレイヤー情報を組み立てる。"""
    xp_total = int(conn.execute(
        "SELECT (SELECT COALESCE(SUM(xp), 0) FROM quest_log) + "
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
    return QuestReward(task_id, str(task.get("raw_text") or "クエスト"), xp, gold, bonuses, before, after.level, achievements)


def pop_unseen_rewards() -> List[QuestReward]:
    """未表示報酬を原子的に取り出して既読にし、再実行による二重演出を防ぐ。"""
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT * FROM quest_log WHERE seen = 0 ORDER BY id").fetchall()
        rewards: List[QuestReward] = []
        for row in rows:
            meta = _metadata(row["bonus"])
            rewards.append(QuestReward(row["task_id"], row["title"], row["xp"], row["gold"],
                                       meta.get("bonuses", []), meta.get("level_before", 1),
                                       meta.get("level_after", 1), meta.get("new_achievements", [])))
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
