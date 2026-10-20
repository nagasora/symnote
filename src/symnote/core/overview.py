"""タスク件数の集計（未完了・今日・期限切れ・完了）。"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Mapping, Optional, Sequence


def task_overview(
    tasks: Sequence[Mapping[str, Any]],
    today: Optional[dt.date] = None,
) -> Dict[str, int]:
    """タスク一覧から表示用の件数集計を返す。データ自体は変更しない。"""
    today = today or dt.date.today()
    overview = {"active": 0, "today": 0, "overdue": 0, "done": 0}
    for task in tasks:
        if task.get("status") == "done":
            overview["done"] += 1
            continue
        overview["active"] += 1
        due_str = task.get("due_date")
        try:
            due = dt.date.fromisoformat(due_str) if due_str else None
        except (ValueError, TypeError):
            due = None
        if due and due < today:
            overview["overdue"] += 1
        if task.get("status") == "today" or due == today:
            overview["today"] += 1
    return overview
