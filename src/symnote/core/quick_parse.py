"""クイック入力（⌘K・＋ボタン）向けの日本語ルールベース日時・繰り返し・種別抽出。

AI を呼ばずにミリ秒単位で動作し、プレビューと保存の両方で同じ結果を返す。
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import re
from typing import Callable, List, Literal, Optional, Tuple

from symnote.core.nlp import classify_text_rule_based

QuickKind = Literal["task", "memo", "unsorted"]
WEEKDAYS: Tuple[str, ...] = ("月", "火", "水", "木", "金", "土", "日")
URGENT_WORDS: Tuple[str, ...] = ("至急", "緊急", "重要", "必ず", "絶対", "!!", "！！")
_MEMO_PREFIX = re.compile(r"^\s*(?:メモ[:：]|#memo\b|#メモ\b)\s*", re.IGNORECASE)


@dataclass(frozen=True)
class QuickRecurrence:
    """クイック入力から読み取った繰り返し指定。"""

    frequency: Literal["daily", "weekly", "weekdays", "monthly"]
    interval: int = 1
    weekdays: Tuple[int, ...] = ()
    month_day: Optional[int] = None
    label: str = ""


@dataclass(frozen=True)
class QuickParseResult:
    """クイック入力の解析結果。"""

    kind: QuickKind
    clean_title: str
    details: str
    due_date: Optional[str]
    due_time: Optional[str]
    recurrence: Optional[QuickRecurrence]
    importance: int = 3
    urgency: int = 3


_WEEKLY_MULTI_RE = re.compile(
    r"(毎週|隔週)\s*([月火水木金土日](?:曜(?:日)?)?(?:[・,、/／][月火水木金土日](?:曜(?:日)?)?)*)"
)
_MONTHLY_DAY_RE = re.compile(r"毎月\s*(\d{1,2})日")
_WEEKDAYS_RE = re.compile(r"(?:毎平日|平日毎日)")
_DAILY_RE = re.compile(r"毎日")
_BIWEEKLY_RE = re.compile(r"隔週")
_WEEKLY_RE = re.compile(r"毎週")
_MONTHLY_RE = re.compile(r"毎月")

_NOON_PATTERN = re.compile(r"正午")
_TIME_PATTERN = re.compile(
    r"(午前|午後|朝|夜|夕方)?\s*(?<!\d)(\d{1,2})(?::(\d{2})(?!\d)|時(?!間)(?:(\d{1,2})分|(半))?)"
)
_CLEANUP_EDGE = re.compile(
    r"^(?:まで|までに|に|の|で|から|へ|は)+|(?:までに|まで|に|の|で)$"
)


def _extract_recurrence(
    text: str, today: dt.date
) -> Tuple[Optional[QuickRecurrence], Optional[str], str]:
    m_wd = _WEEKLY_MULTI_RE.search(text)
    if m_wd is not None:
        prefix = m_wd.group(1)
        interval = 2 if prefix == "隔週" else 1
        wd_chars = [ch for ch in m_wd.group(2) if ch in WEEKDAYS]
        wds: List[int] = []
        for ch in wd_chars:
            idx = WEEKDAYS.index(ch)
            if idx not in wds:
                wds.append(idx)
        wds_tuple = tuple(wds)
        first_due = _resolve_weekday(today, wds_tuple[0], "").isoformat() if wds_tuple else None
        wd_label = "・".join(WEEKDAYS[i] for i in wds_tuple)
        label = f"{prefix}{wd_label}曜" if wd_label else prefix
        rest = text[: m_wd.start()] + " " + text[m_wd.end() :]
        return (
            QuickRecurrence(
                frequency="weekly",
                interval=interval,
                weekdays=wds_tuple,
                label=label,
            ),
            first_due,
            rest,
        )

    m_md = _MONTHLY_DAY_RE.search(text)
    if m_md is not None:
        day_num = max(1, min(31, int(m_md.group(1))))
        due = _resolve_month_day(today.month, day_num, today)
        if due is None:
            next_m = 1 if today.month == 12 else today.month + 1
            due = _resolve_month_day(next_m, day_num, today)
        rest = text[: m_md.start()] + " " + text[m_md.end() :]
        return (
            QuickRecurrence(
                frequency="monthly",
                interval=1,
                month_day=day_num,
                label=f"毎月{day_num}日",
            ),
            due.isoformat() if due else None,
            rest,
        )

    for pattern, spec in (
        (_WEEKDAYS_RE, QuickRecurrence(frequency="weekdays", interval=1, label="毎平日")),
        (_DAILY_RE, QuickRecurrence(frequency="daily", interval=1, label="毎日")),
        (_BIWEEKLY_RE, QuickRecurrence(frequency="weekly", interval=2, label="隔週")),
        (_WEEKLY_RE, QuickRecurrence(frequency="weekly", interval=1, label="毎週")),
        (_MONTHLY_RE, QuickRecurrence(frequency="monthly", interval=1, label="毎月")),
    ):
        match = pattern.search(text)
        if match is not None:
            rest = text[: match.start()] + " " + text[match.end() :]
            return spec, None, rest
    return None, None, text


def _extract_time(text: str) -> Tuple[Optional[str], str]:
    noon = _NOON_PATTERN.search(text)
    if noon is not None:
        return "12:00", text[: noon.start()] + " " + text[noon.end() :]

    match = _TIME_PATTERN.search(text)
    if match is None:
        return None, text
    period, hour_text, colon_minute, kanji_minute, half = match.groups()
    hour = int(hour_text)
    minute = int(colon_minute or kanji_minute or (30 if half else 0))
    if period in {"午後", "夜", "夕方"} and hour < 12:
        hour += 12
    elif period == "午前" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None, text
    return f"{hour:02d}:{minute:02d}", text[: match.start()] + " " + text[match.end() :]


def _resolve_weekday(today: dt.date, target_wd: int, prefix: str) -> dt.date:
    if prefix == "来週":
        monday = today - dt.timedelta(days=today.weekday()) + dt.timedelta(days=7)
        return monday + dt.timedelta(days=target_wd)
    if prefix == "今週":
        monday = today - dt.timedelta(days=today.weekday())
        candidate = monday + dt.timedelta(days=target_wd)
        if candidate >= today:
            return candidate
    delta = (target_wd - today.weekday()) % 7
    if delta == 0:
        delta = 7
    return today + dt.timedelta(days=delta)


def _resolve_month_day(month: int, day: int, today: dt.date) -> Optional[dt.date]:
    for year in (today.year, today.year + 1):
        try:
            candidate = dt.date(year, month, day)
        except ValueError:
            return None
        if candidate >= today:
            return candidate
    return None


DateResolver = Callable[[re.Match[str], dt.date], Optional[dt.date]]

_DATE_PATTERNS: Tuple[Tuple[re.Pattern[str], DateResolver], ...] = (
    (
        re.compile(r"(\d{4})[/\-年](\d{1,2})[/\-月](\d{1,2})日?"),
        lambda m, _t: _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3))),
    ),
    (
        re.compile(r"(\d{1,2})月(\d{1,2})日"),
        lambda m, t: _resolve_month_day(int(m.group(1)), int(m.group(2)), t),
    ),
    (
        re.compile(r"(?<![\d:])(\d{1,2})/(\d{1,2})(?![\d/])"),
        lambda m, t: _resolve_month_day(int(m.group(1)), int(m.group(2)), t),
    ),
    (
        re.compile(r"(\d{1,2})週間後"),
        lambda m, t: t + dt.timedelta(days=int(m.group(1)) * 7),
    ),
    (
        re.compile(r"(\d{1,3})日後"),
        lambda m, t: t + dt.timedelta(days=int(m.group(1))),
    ),
    (
        re.compile(r"(来週|今週)?の?([月火水木金土日])曜日?"),
        lambda m, t: _resolve_weekday(t, WEEKDAYS.index(m.group(2)), m.group(1) or ""),
    ),
    (
        re.compile(r"(来週)?の?週末"),
        lambda m, t: _resolve_weekday(t, 5, "来週" if m.group(1) == "来週" else ""),
    ),
    (
        re.compile(r"明後日|あさって|明日|あした|今日|きょう|本日"),
        lambda m, t: t
        + dt.timedelta(
            days={
                "今日": 0,
                "きょう": 0,
                "本日": 0,
                "明日": 1,
                "あした": 1,
                "明後日": 2,
                "あさって": 2,
            }[m.group(0)]
        ),
    ),
    (
        re.compile(r"来週"),
        lambda _m, t: t + dt.timedelta(days=7),
    ),
)


def _safe_date(year: int, month: int, day: int) -> Optional[dt.date]:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _extract_date(text: str, today: dt.date) -> Tuple[Optional[str], str]:
    for pattern, resolver in _DATE_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        resolved = resolver(match, today)
        if resolved is None:
            continue
        return resolved.isoformat(), text[: match.start()] + " " + text[match.end() :]
    return None, text


def _clean_title(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip(" 　、。,.・-")
    prev = None
    while prev != cleaned:
        prev = cleaned
        cleaned = _CLEANUP_EDGE.sub("", cleaned).strip(" 　、。,.・-")
    return cleaned[:200]


def parse_quick_input(text: str, today: Optional[dt.date] = None) -> QuickParseResult:
    """入力文字列から種別・タイトル・詳細・期限日・期限時刻・繰り返しを抽出する。"""
    today = today or dt.date.today()
    raw = (text or "").strip()
    if not raw:
        return QuickParseResult(
            kind="task",
            clean_title="",
            details="",
            due_date=None,
            due_time=None,
            recurrence=None,
        )

    first_line, has_newline, rest = raw.partition("\n")
    memo_match = _MEMO_PREFIX.match(first_line)
    if memo_match is not None:
        stripped = first_line[memo_match.end() :].strip()
        return QuickParseResult(
            kind="memo",
            clean_title=_clean_title(stripped) or stripped[:200],
            details=rest.strip(),
            due_date=None,
            due_time=None,
            recurrence=None,
        )

    if has_newline and rest.strip():
        return QuickParseResult(
            kind="memo",
            clean_title=_clean_title(first_line) or first_line.strip()[:200],
            details=rest.strip(),
            due_date=None,
            due_time=None,
            recurrence=None,
        )

    recurrence, rec_due_date, remaining = _extract_recurrence(first_line, today)
    due_time, remaining = _extract_time(remaining)
    due_date, remaining = _extract_date(remaining, today)
    if due_date is None and rec_due_date is not None:
        due_date = rec_due_date

    if (due_time is not None or recurrence is not None) and due_date is None:
        due_date = today.isoformat()

    cleaned_for_title = remaining
    for word in URGENT_WORDS:
        cleaned_for_title = cleaned_for_title.replace(word, " ")
    clean_title = _clean_title(cleaned_for_title) or first_line.strip()[:200]

    classification = classify_text_rule_based(raw)
    has_schedule_signal = any((due_date, due_time, recurrence))
    if has_schedule_signal or classification.ai_category == "task":
        kind: QuickKind = "task"
    else:
        kind = "task" if len(clean_title) <= 60 else "memo"

    importance = max(
        classification.importance,
        5 if any(word in raw for word in URGENT_WORDS) else 3,
    )
    return QuickParseResult(
        kind=kind,
        clean_title=clean_title,
        details=rest.strip(),
        due_date=due_date,
        due_time=due_time,
        recurrence=recurrence,
        importance=importance,
        urgency=classification.urgency,
    )
