"""自然文からのタスク入力と、AI による今日の計画づくり。

どの機能も AI が使えない（API キー未設定・オフライン・応答不正）場合はルールベースの
結果へ落ちるため、タスク管理そのものは止まらない。AI の応答は信頼できない入力として
扱い、日付・時刻・ID を検証してから返す。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple

from symnote.core import llm
from symnote.core.nlp import suggest_today_tasks

TaskRow = Dict[str, Any]
ParseSource = Literal["rule", "ai"]

WEEKDAYS: Tuple[str, ...] = ("月", "火", "水", "木", "金", "土", "日")
URGENT_WORDS: Tuple[str, ...] = ("至急", "緊急", "重要", "必ず", "絶対", "!!", "！！")
MAX_TITLE_LENGTH = 200
MAX_PLAN_TASKS = 40
MAX_SUBTASKS = 8


@dataclass(frozen=True)
class ParsedTask:
    """クイック入力 1 行から読み取ったタスク。"""

    title: str
    details: str = ""
    due_date: Optional[str] = None
    due_time: Optional[str] = None
    importance: int = 3
    source: ParseSource = "rule"


@dataclass(frozen=True)
class PlanItem:
    """今日の計画の 1 行。"""

    task_id: int
    title: str
    reason: str
    start_time: Optional[str] = None


@dataclass(frozen=True)
class DayPlan:
    """今日の計画。AI が使えなかった場合は source が rule になる。"""

    summary: str
    items: List[PlanItem] = field(default_factory=list)
    source: ParseSource = "rule"
    notice: str = ""


# --- クイック入力（ルールベース） -------------------------------------------------


def _next_weekday(today: dt.date, weekday: int, *, next_week: bool, this_week: bool) -> dt.date:
    """指定曜日の日付を返す。「来週」は翌週の同曜日、無指定は今日以降で最も近い日。"""
    if next_week:
        monday = today - dt.timedelta(days=today.weekday()) + dt.timedelta(days=7)
        return monday + dt.timedelta(days=weekday)
    if this_week:
        monday = today - dt.timedelta(days=today.weekday())
        return monday + dt.timedelta(days=weekday)
    delta = (weekday - today.weekday()) % 7
    return today + dt.timedelta(days=delta)


def _month_day(month: int, day: int, today: dt.date) -> Optional[dt.date]:
    """年なしの月日を、過ぎていれば翌年として解釈する。"""
    for year in (today.year, today.year + 1):
        try:
            candidate = dt.date(year, month, day)
        except ValueError:
            return None
        if candidate >= today:
            return candidate
    return None


def _match_relative(match: re.Match[str], today: dt.date) -> Optional[dt.date]:
    offsets = {"今日": 0, "きょう": 0, "本日": 0, "明日": 1, "あした": 1, "明後日": 2, "あさって": 2}
    return today + dt.timedelta(days=offsets[match.group(0)])


def _match_days_later(match: re.Match[str], today: dt.date) -> Optional[dt.date]:
    return today + dt.timedelta(days=int(match.group(1)))


def _match_weekday(match: re.Match[str], today: dt.date) -> Optional[dt.date]:
    prefix = match.group(1) or ""
    return _next_weekday(
        today,
        WEEKDAYS.index(match.group(2)),
        next_week=prefix == "来週",
        this_week=prefix == "今週",
    )


def _match_weekend(match: re.Match[str], today: dt.date) -> Optional[dt.date]:
    return _next_weekday(today, 5, next_week=match.group(1) == "来週", this_week=False)


def _match_full_date(match: re.Match[str], today: dt.date) -> Optional[dt.date]:
    try:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _match_month_day(match: re.Match[str], today: dt.date) -> Optional[dt.date]:
    return _month_day(int(match.group(1)), int(match.group(2)), today)


DateResolver = Callable[[re.Match[str], dt.date], Optional[dt.date]]
_DATE_PATTERNS: Tuple[Tuple[str, DateResolver], ...] = (
    (r"(\d{4})[/\-年](\d{1,2})[/\-月](\d{1,2})日?", _match_full_date),
    (r"(\d{1,2})月(\d{1,2})日", _match_month_day),
    (r"(?<![\d:])(\d{1,2})/(\d{1,2})(?![\d/])", _match_month_day),
    (r"(\d{1,3})日後", _match_days_later),
    (r"(来週|今週)?の?([月火水木金土日])曜日?", _match_weekday),
    (r"(来週)?の?週末", _match_weekend),
    (r"明後日|あさって|明日|あした|今日|きょう|本日", _match_relative),
)

_TIME_PATTERN = re.compile(
    r"(午前|午後|朝|夜|夕方)?\s*(?<!\d)(\d{1,2})(?::(\d{2})(?!\d)|時(?!間)(?:(\d{1,2})分|(半))?)"
)
_CLEANUP_PATTERN = re.compile(r"^(?:まで|までに|に|の|で|から)+|(?:までに|まで)$")


def _extract_time(text: str) -> Tuple[Optional[str], str]:
    """時刻表現を取り出し、(HH:MM, 残りの文字列) を返す。"""
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
    return f"{hour:02d}:{minute:02d}", text[: match.start()] + " " + text[match.end():]


def _extract_date(text: str, today: dt.date) -> Tuple[Optional[str], str]:
    """日付表現を取り出し、(YYYY-MM-DD, 残りの文字列) を返す。"""
    for pattern, resolver in _DATE_PATTERNS:
        match = re.search(pattern, text)
        if match is None:
            continue
        resolved = resolver(match, today)
        if resolved is None:
            continue
        return resolved.isoformat(), text[: match.start()] + " " + text[match.end():]
    return None, text


def _clean_title(text: str) -> str:
    """日時を取り除いた残りから、前後の助詞や記号を落としてタイトルにする。"""
    title = re.sub(r"\s+", " ", text).strip(" 　、。,.")
    previous = None
    while previous != title:
        previous = title
        title = _CLEANUP_PATTERN.sub("", title).strip(" 　、。,.")
    return title[:MAX_TITLE_LENGTH]


def parse_quick_task_rule_based(text: str, today: Optional[dt.date] = None) -> ParsedTask:
    """「明日15時までに資料提出」のような 1 行を、AI を使わずにタスクへ分解する。"""
    today = today or dt.date.today()
    source_text = text.strip()
    if not source_text:
        raise ValueError("タスクの内容を入力してください。")
    first_line, _, rest = source_text.partition("\n")
    due_time, remaining = _extract_time(first_line)
    due_date, remaining = _extract_date(remaining, today)
    if due_time and not due_date:
        due_date = today.isoformat()
    for word in URGENT_WORDS:
        remaining = remaining.replace(word, " ")
    title = _clean_title(remaining) or first_line[:MAX_TITLE_LENGTH]
    importance = 5 if any(word in source_text for word in URGENT_WORDS) else 3
    return ParsedTask(
        title=title,
        details=rest.strip(),
        due_date=due_date,
        due_time=due_time,
        importance=importance,
        source="rule",
    )


# --- AI ------------------------------------------------------------------------------


def _valid_date(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def _valid_time(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.time.fromisoformat(value.strip()).strftime("%H:%M")
    except ValueError:
        return None


def _bigrams(text: str) -> set[str]:
    # ひらがな（「する」「を」など）は内容を表さないので除き、漢字・カタカナ・英数字だけで比べる。
    compact = re.sub(r"[\s\u3040-\u309f、。・]+", "", text)
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def _shares_content(candidate: str, reference: str) -> bool:
    """AI のタイトルが元の入力の語句を少しでも含んでいるか（2 文字単位の重なり）。"""
    reference_grams = _bigrams(reference)
    return not reference_grams or bool(_bigrams(candidate) & reference_grams)


def parse_quick_task(
    text: str,
    today: Optional[dt.date] = None,
    *,
    use_ai: bool = True,
    generate_json: Callable[..., Any] = llm.generate_json,
) -> ParsedTask:
    """自然文をタスクへ分解する。AI が使えればそれを優先し、だめならルールで読む。"""
    today = today or dt.date.today()
    fallback = parse_quick_task_rule_based(text, today)
    if not use_ai:
        return fallback
    prompt = f"""
あなたは日本語のタスク管理アシスタントです。次の入力を 1 件のタスクに整理してください。
今日は {today.isoformat()}（{WEEKDAYS[today.weekday()]}曜日）です。

- title: 動詞で終わる簡潔なタスク名（日時の表現は含めない）
- details: 補足（なければ空文字）
- due_date: 期限日 YYYY-MM-DD。入力に日付の手掛かりがなければ null
- due_time: 期限時刻 HH:MM。時刻の手掛かりがなければ null
- importance: 1〜5（至急・重要などの語や内容から判断。普通は 3）

入力:
{text.strip()[:2000]}
""".strip()
    hint = '{"title": "...", "details": "...", "due_date": "YYYY-MM-DD" または null, "due_time": "HH:MM" または null, "importance": 3}'
    try:
        data = generate_json(prompt, hint=hint, timeout=20)
    except Exception:
        return fallback
    if not isinstance(data, dict):
        return fallback
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        return fallback
    if not _shares_content(title, fallback.title):
        # Why not AI のタイトルをそのまま採用: 軽量モデルは「至急対応する」のように中身を落とすことがある。
        title = fallback.title
    importance = data.get("importance")
    if isinstance(importance, bool) or not isinstance(importance, int) or not 1 <= importance <= 5:
        importance = fallback.importance
    details = data.get("details") if isinstance(data.get("details"), str) else ""
    # Why not AI の値を常に優先: 応答の欠落・不正値で、文中に明示された日時が消えてしまう。
    # ルールで確実に読めた日時を正とし、AI はルールで読めなかった分だけを補う。
    due_date = fallback.due_date or _valid_date(data.get("due_date"))
    due_time = fallback.due_time or _valid_time(data.get("due_time"))
    if due_time and not due_date:
        due_date = fallback.due_date or today.isoformat()
    return ParsedTask(
        title=title.strip()[:MAX_TITLE_LENGTH],
        details=(details or fallback.details).strip(),
        due_date=due_date,
        due_time=due_time,
        importance=importance,
        source="ai",
    )


def task_title(task: TaskRow) -> str:
    """タスク行の表示名（タイトル列が空なら本文の 1 行目）。"""
    title = (task.get("tags") or "").strip()
    if title:
        return title
    body = (task.get("raw_text") or "").strip()
    return body.splitlines()[0] if body else f"タスク #{task.get('id')}"


def _rule_based_plan(tasks: Sequence[TaskRow], today: dt.date, notice: str) -> DayPlan:
    """AI を使わずに優先度スコアで上位を並べた計画を返す。"""
    by_id = {int(task["id"]): task for task in tasks}
    suggestions = suggest_today_tasks(tasks, today=today, limit=5)
    items = [
        PlanItem(task_id=s.task_id, title=task_title(by_id[s.task_id]), reason=s.reason)
        for s in suggestions
        if s.task_id in by_id
    ]
    summary = "期限と優先度から、今日取り組むとよいタスクを選びました。" if items else "今日やるべき未完了タスクはありません。"
    return DayPlan(summary=summary, items=items, source="rule", notice=notice)


def _format_events(events: Sequence[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for event in events[:30]:
        start = str(event.get("start") or "終日")
        end = str(event.get("end") or "")
        title = str(event.get("summary") or event.get("title") or "予定")[:100]
        lines.append(f"- {start}〜{end} {title}")
    return "\n".join(lines) or "（予定なし）"


def plan_day(
    tasks: Sequence[TaskRow],
    events: Sequence[Dict[str, Any]] = (),
    today: Optional[dt.date] = None,
    *,
    use_ai: bool = True,
    generate_json: Callable[..., Any] = llm.generate_json,
) -> DayPlan:
    """未完了タスクと今日の予定から、今日の進め方を提案する。

    AI には未完了タスクの ID・タイトル・期限だけを渡し、応答の task_id は渡した集合に
    含まれるものだけを採用する（存在しないタスクを作らせない）。
    """
    today = today or dt.date.today()
    open_tasks = [t for t in tasks if t.get("kind") == "task" and (t.get("status") or "") != "done"]
    if not open_tasks:
        return DayPlan(summary="今日やるべき未完了タスクはありません。")
    if not use_ai:
        return _rule_based_plan(open_tasks, today, "")
    ranked = sorted(
        open_tasks,
        key=lambda t: (t.get("due_date") or "9999-12-31", -(int(t.get("importance") or 3))),
    )[:MAX_PLAN_TASKS]
    task_lines = "\n".join(
        f"- id={int(t['id'])} / {task_title(t)[:120]} / 期限: {t.get('due_date') or 'なし'}"
        f"{' ' + t['due_time'] if t.get('due_time') else ''} / 重要度: {t.get('importance') or 3}"
        for t in ranked
    )
    prompt = f"""
あなたは優秀なパーソナル秘書です。今日 {today.isoformat()}（{WEEKDAYS[today.weekday()]}曜日）の
計画を立ててください。期限切れ・今日期限・重要度を優先し、予定の空き時間に収まる量（最大 6 件）に絞ります。

今日の予定:
{_format_events(events)}

未完了タスク:
{task_lines}

出力: summary は今日の方針を 1〜2 文で。items は実行順に並べ、各 item に task_id（上の id のみ）、
reason（なぜ今日やるか、30 字程度）、start_time（予定の空きに合わせた開始目安 HH:MM、決められなければ null）。
""".strip()
    hint = '{"summary": "...", "items": [{"task_id": 1, "reason": "...", "start_time": "HH:MM" または null}]}'
    try:
        data = generate_json(prompt, hint=hint, timeout=30)
    except llm.LLMUnavailableError as exc:
        return _rule_based_plan(open_tasks, today, str(exc))
    except Exception as exc:
        return _rule_based_plan(open_tasks, today, f"AI の計画づくりに失敗したため、ルールで選びました（{exc}）")
    by_id = {int(t["id"]): t for t in ranked}
    items: List[PlanItem] = []
    seen: set[int] = set()
    raw_items = data.get("items") if isinstance(data, dict) else None
    for raw in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(raw, dict):
            continue
        task_id = raw.get("task_id")
        if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id not in by_id or task_id in seen:
            continue
        seen.add(task_id)
        reason = raw.get("reason") if isinstance(raw.get("reason"), str) else ""
        items.append(
            PlanItem(
                task_id=task_id,
                title=task_title(by_id[task_id]),
                reason=reason.strip()[:120],
                start_time=_valid_time(raw.get("start_time")),
            )
        )
        if len(items) >= 6:
            break
    if not items:
        return _rule_based_plan(open_tasks, today, "AI の応答から計画を読み取れなかったため、ルールで選びました。")
    summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
    return DayPlan(summary=summary.strip()[:300] or "今日の計画です。", items=items, source="ai")


def breakdown_task(
    title: str,
    details: str = "",
    *,
    generate_json: Callable[..., Any] = llm.generate_json,
) -> List[str]:
    """タスクを 15〜60 分で終わる具体的なサブタスクへ分解する。失敗時は例外を送出する。"""
    prompt = f"""
次のタスクを、15〜60 分で終わる具体的な作業ステップ 3〜{MAX_SUBTASKS} 個に分解してください。
各ステップは動詞で終わる短い文にします。

タスク: {title[:200]}
補足: {details[:2000] or 'なし'}
""".strip()
    data = generate_json(prompt, hint='{"steps": ["...", "..."]}', timeout=30)
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        raise llm.LLMResponseError("AI から分解結果を取得できませんでした。")
    result = [s.strip()[:MAX_TITLE_LENGTH] for s in steps if isinstance(s, str) and s.strip()]
    if not result:
        raise llm.LLMResponseError("AI から分解結果を取得できませんでした。")
    return result[:MAX_SUBTASKS]
