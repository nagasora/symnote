"""DB 行・ドメインモデルをフロントエンド向け JSON 辞書に整形するモジュール。"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Mapping, Optional, Sequence

from symnote.core.db import fetch_task_sources, recurrence_label
from symnote.core.goals import GoalProgress, list_goals_with_progress
from symnote.core.quick_parse import parse_quick_input
from symnote.core.titles import memo_title_and_body, task_title_and_details


def goal_title_map() -> Dict[int, str]:
    """ゴール ID → ゴールタイトルの辞書を返す。"""
    try:
        return {g.id: g.title for g in list_goals_with_progress(include_archived=True)}
    except Exception:
        return {}


def _recurrence_dict(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    rule_id = row.get("recurrence_rule_id")
    interval_days = row.get("recurrence_interval_days")
    if not rule_id or not interval_days:
        return None
    freq = row.get("recurrence_frequency") or ("weekly" if interval_days in (7, 14) else "daily")
    weekdays_str = str(row.get("recurrence_weekdays") or "")
    weekdays = [int(d) for d in weekdays_str.split(",") if d.strip().isdigit()]
    interval = max(1, int(interval_days) // 7) if freq == "weekly" else int(interval_days)
    return {
        "rule_id": int(rule_id),
        "frequency": freq,
        "interval": interval,
        "interval_days": int(interval_days),
        "weekdays": weekdays,
        "month_day": row.get("recurrence_month_day"),
        "end_date": row.get("recurrence_end_date"),
        "active": bool(row.get("recurrence_active", 1)),
        "label": recurrence_label(dict(row)),
    }


def present_task(
    row: Mapping[str, Any],
    *,
    goal_titles: Optional[Mapping[int, str]] = None,
    sources: Optional[Mapping[int, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """タスク行を API レスポンス用の辞書へ変換する。"""
    task_id = int(row["id"])
    title, details = task_title_and_details(row)
    gid = row.get("goal_id")
    goal_id = int(gid) if gid is not None else None
    g_titles = goal_titles if goal_titles is not None else {}
    src_map = sources if sources is not None else {}
    rec = _recurrence_dict(row)
    return {
        "id": task_id,
        "kind": "task",
        "title": title,
        "details": details,
        "raw_text": str(row.get("raw_text") or ""),
        "status": row.get("status") or "inbox",
        "due_date": row.get("due_date"),
        "due_time": row.get("due_time"),
        "date": row.get("date"),
        "created_at": row.get("created_at"),
        "completed_at": row.get("completed_at"),
        "importance": int(row.get("importance") or 3),
        "urgency": int(row.get("urgency") or 3),
        "effort": row.get("effort"),
        "energy": row.get("energy"),
        "goal_id": goal_id,
        "goal_title": g_titles.get(goal_id) if goal_id is not None else None,
        "estimate_minutes": int(row["estimate_minutes"])
        if row.get("estimate_minutes") is not None
        else None,
        "delegated_to": row.get("delegated_to"),
        "recurrence_label": rec["label"] if rec else None,
        "recurrence": rec,
        "source": src_map.get(task_id),
    }


def present_tasks(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """複数タスク行の出典・ゴール名を一括解決して整形する。"""
    if not rows:
        return []
    task_ids = [int(r["id"]) for r in rows]
    sources = fetch_task_sources(task_ids)
    goals = goal_title_map()
    return [present_task(r, goal_titles=goals, sources=sources) for r in rows]


def present_memo(row: Mapping[str, Any]) -> Dict[str, Any]:
    """メモ行を API レスポンス用の辞書へ変換する。"""
    title, body = memo_title_and_body(row)
    raw_tags = str(row.get("tags") or "").strip()
    tag_list = [t.strip() for t in raw_tags.split(",") if t.strip()]
    return {
        "id": int(row["id"]),
        "kind": "memo",
        "title": title,
        "body": body,
        "text": str(row.get("raw_text") or ""),
        "tags": tag_list,
        "tags_text": raw_tags,
        "date": row.get("date"),
        "created_at": row.get("created_at"),
        "ai_category": row.get("ai_category") or "memo",
    }


def present_candidate(row: Mapping[str, Any]) -> Dict[str, Any]:
    """ToDo 候補行を API レスポンス用の辞書へ変換する。"""
    return {
        "id": int(row["id"]),
        "source": str(row.get("source") or "other"),
        "source_ref": str(row.get("source_ref") or ""),
        "source_url": row.get("source_url"),
        "title": str(row.get("title") or ""),
        "details": str(row.get("details") or ""),
        "excerpt": str(row.get("excerpt") or ""),
        "due_date": row.get("due_date"),
        "due_time": row.get("due_time"),
        "confidence": float(row["confidence"])
        if row.get("confidence") is not None
        else None,
        "status": str(row.get("status") or "pending"),
        "created_at": row.get("created_at"),
        "decided_at": row.get("decided_at"),
        "task_id": int(row["task_id"]) if row.get("task_id") is not None else None,
    }


def present_unsorted(
    row: Mapping[str, Any], *, today: Optional[dt.date] = None
) -> Dict[str, Any]:
    """未仕分けアイテムをルールベースの提案付きで辞書へ変換する。"""
    raw_text = str(row.get("raw_text") or "")
    parsed = parse_quick_input(raw_text, today=today)
    return {
        "id": int(row["id"]),
        "text": raw_text,
        "created_at": row.get("created_at"),
        "date": row.get("date"),
        "ai_category": row.get("ai_category"),
        "suggested_kind": parsed.kind,
        "suggested_title": parsed.clean_title or (raw_text.splitlines()[0] if raw_text.strip() else ""),
        "suggested_due_date": parsed.due_date,
        "suggested_due_time": parsed.due_time,
    }


def present_goal(
    goal: GoalProgress,
    *,
    goal_titles: Optional[Mapping[int, str]] = None,
    sources: Optional[Mapping[int, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """進捗付きゴールを API レスポンス用の辞書へ変換する。"""
    next_task = (
        present_task(goal.next_task, goal_titles=goal_titles, sources=sources)
        if goal.next_task
        else None
    )
    pct = int(round(float(goal.progress or 0.0) * 100))
    return {
        "id": int(goal.id),
        "parent_id": int(goal.parent_id) if goal.parent_id is not None else None,
        "title": goal.title,
        "why": goal.why,
        "target_date": goal.target_date,
        "status": goal.status,
        "depth": goal.depth,
        "open_tasks": int(goal.open_count),
        "done_tasks": int(goal.done_count),
        "progress": float(goal.progress or 0.0),
        "progress_percent": pct,
        "last_activity": goal.last_activity,
        "stale_days": goal.stale_days,
        "next_task": next_task,
    }
