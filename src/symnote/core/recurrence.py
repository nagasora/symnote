"""繰り返しの規則と日付計算を、永続化や UI に依存せず提供する。"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from typing import Tuple

WEEKDAY_LABELS: Tuple[str, ...] = ("月", "火", "水", "木", "金", "土", "日")


@dataclass(frozen=True)
class RecurrenceRule:
    """週は月曜起点、月次は元の指定日を保持する繰り返し規則。"""

    frequency: str
    interval_days: int = 1
    weekdays: Tuple[int, ...] = ()
    month_day: int | None = None

    def __post_init__(self) -> None:
        """保存前に規則の値域を検査する。"""
        if self.frequency not in {"daily", "weekdays", "weekly", "monthly", "custom"}:
            raise ValueError("繰り返し頻度が不正です。")
        if type(self.interval_days) is not int or self.interval_days < 1:
            raise ValueError("繰り返し間隔は正の整数にしてください。")
        if self.frequency == "weekly" and self.interval_days % 7:
            raise ValueError("週の間隔は7日の倍数にしてください。")
        if any(type(day) is not int or day not in range(7) for day in self.weekdays):
            raise ValueError("曜日が不正です。")
        if self.frequency == "monthly" and (
            type(self.month_day) is not int or not 1 <= self.month_day <= 31
        ):
            raise ValueError("毎月の日付は1〜31日にしてください。")


def next_occurrence(base: dt.date, rule: RecurrenceRule) -> dt.date:
    """基準日の直後の予定日を返す。短い月では指定日を月末に丸める。"""
    if rule.frequency == "monthly":
        year, month = base.year, base.month
        day = min(rule.month_day, calendar.monthrange(year, month)[1])
        candidate = dt.date(year, month, day)
        if candidate > base:
            return candidate
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return dt.date(year, month, min(rule.month_day, calendar.monthrange(year, month)[1]))
    if rule.frequency == "weekdays":
        candidate = base + dt.timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += dt.timedelta(days=1)
        return candidate
    if rule.frequency == "weekly" and rule.weekdays:
        monday = base - dt.timedelta(days=base.weekday())
        for weekday in sorted(set(rule.weekdays)):
            candidate = monday + dt.timedelta(days=weekday)
            if candidate > base:
                return candidate
        return monday + dt.timedelta(days=rule.interval_days + min(rule.weekdays))
    return base + dt.timedelta(days=rule.interval_days)


def first_occurrence(start: dt.date, rule: RecurrenceRule) -> dt.date:
    """起点日以降で規則を満たす最初の日を返す。"""
    if rule.frequency == "monthly":
        return next_occurrence(start - dt.timedelta(days=1), rule)
    if rule.frequency == "weekdays":
        return next_occurrence(start - dt.timedelta(days=1), rule)
    if rule.frequency == "weekly" and rule.weekdays:
        for offset in range(7):
            candidate = start + dt.timedelta(days=offset)
            if candidate.weekday() in rule.weekdays:
                return candidate
    return start


def rule_label(rule: RecurrenceRule) -> str:
    """規則を日本語の短い表示名に変換する。"""
    if rule.frequency == "weekdays":
        return "平日"
    if rule.frequency == "monthly":
        return f"毎月{rule.month_day}日"
    if rule.frequency == "weekly":
        label = (
            "毎週"
            if rule.interval_days == 7
            else "隔週"
            if rule.interval_days == 14
            else f"{rule.interval_days // 7}週ごと"
        )
        if rule.weekdays:
            label += " " + "・".join(WEEKDAY_LABELS[day] for day in sorted(set(rule.weekdays)))
        return label
    if rule.frequency == "custom":
        return f"{rule.interval_days}日ごと"
    return {1: "毎日", 7: "毎週", 14: "隔週"}.get(rule.interval_days, f"{rule.interval_days}日ごと")
