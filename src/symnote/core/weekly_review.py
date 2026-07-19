from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import List

from symnote.core.db import fetch_weekly_review_sources


@dataclass(frozen=True)
class WeeklyReviewPayload:
    period_start: str
    period_end: str
    summary: str
    good_points: List[str]
    learnings: List[str]
    focus_next: List[str]

    def to_markdown(self) -> str:
        lines = [
            f"## 週次レポート ({self.period_start} - {self.period_end})",
            "",
            f"**今週のサマリー**: {self.summary}",
            "",
            "### 👍 良かった点",
        ]
        if self.good_points:
            lines.extend(f"- {point}" for point in self.good_points)
        else:
            lines.append("- 記録なし")
        lines.append("")
        lines.append("### 🧠 学び・改善点")
        if self.learnings:
            lines.extend(f"- {point}" for point in self.learnings)
        else:
            lines.append("- 記録なし")
        lines.append("")
        lines.append("### 🎯 来週のフォーカス")
        if self.focus_next:
            lines.extend(f"- {point}" for point in self.focus_next)
        else:
            lines.append("- 記録なし")
        return "\n".join(lines)


def default_week_range(today: date | None = None) -> tuple[str, str]:
    today = today or date.today()
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    return start.isoformat(), end.isoformat()


def generate_weekly_review(today: date | None = None) -> WeeklyReviewPayload:
    start, end = default_week_range(today)
    data = fetch_weekly_review_sources(start, end)
    tasks = data["tasks"]
    memos = data["memos"]

    completed = [t for t in tasks if t.get("status") == "done"]
    carry_over = [t for t in tasks if t.get("status") in {"today", "week"}]
    overdue = [
        t
        for t in tasks
        if t.get("due_date") and t.get("status") != "done" and t["due_date"] < end
    ]

    summary_parts: List[str] = []
    if completed:
        summary_parts.append(f"完了タスク {len(completed)} 件")
    if carry_over:
        summary_parts.append(f"未完タスク {len(carry_over)} 件")
    if memos:
        summary_parts.append(f"メモ {len(memos)} 件を追加")
    if not summary_parts:
        summary_parts.append("活動記録が少なめの週でした")
    summary = " / ".join(summary_parts)

    good_points: List[str] = []
    if completed:
        important = [t for t in completed if (t.get("importance") or 0) >= 4]
        if important:
            good_points.append(f"重要タスクを {len(important)} 件完了")
        good_points.append("完了タスクにより進捗を可視化できました")
    if memos:
        good_points.append("メモを通じて思考整理が進んだ")
    if not good_points:
        good_points.append("基盤づくりの準備期間")

    learnings: List[str] = []
    if overdue:
        learnings.append("期限が過ぎたタスクは優先度を再確認する")
    if len(carry_over) > len(completed):
        learnings.append("今週はタスクを絞り込み集中する余地あり")
    if not learnings:
        learnings.append("全体的にバランス良く進行")

    focus_next: List[str] = []
    upcoming = [
        t for t in tasks if t.get("status") in {"today", "week"} and t.get("due_date") and t["due_date"] >= end
    ]
    upcoming.sort(key=lambda t: t.get("due_date"))
    for task in upcoming[:3]:
        focus_next.append(f"ID {task['id']} {task.get('raw_text', '')[:40]}")
    if not focus_next and carry_over:
        focus_next.append("未完了タスクを '今日' ビューへ移動して着手")
    if not focus_next:
        focus_next.append("新規アイデアをタスク化して優先順位を決める")

    return WeeklyReviewPayload(
        period_start=start,
        period_end=end,
        summary=summary,
        good_points=good_points,
        learnings=learnings,
        focus_next=focus_next,
    )
