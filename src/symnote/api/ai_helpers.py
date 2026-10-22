"""SymNote Web 向けの AI 補助機能（ブレインダンプ分解・次の一手選出・タスク分解）。

Gemini API キーが設定されている場合は LLM で高精度に分解し、
未設定または上限到達時はルールベース（quick_parse）へ自動フォールバックする。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from symnote.config import load_config
from symnote.core import llm, task_ai
def priority_score(importance: int, urgency: int, effort: str | None = None, energy: str | None = None) -> float:
    base = float(importance) * 1.4 + float(urgency) * 1.6
    if effort == "short":
        base += 0.6
    elif effort == "long":
        base -= 0.2
    if energy == "low":
        base += 0.2
    return base
from symnote.core.quick_parse import parse_quick_input
from symnote.core.titles import task_title

_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*・•●◦]|\d+[.)、．])\s*")
_SPLIT_SENTENCE_RE = re.compile(r"[\r\n]+|(?<=[。！？])\s*")


def _extract_json_array(raw: str) -> List[Dict[str, Any]]:
    cleaned = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", cleaned)
    if fence:
        cleaned = fence.group(1)
    else:
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start != -1 and end != -1 and end > start:
            cleaned = cleaned[start : end + 1]
    data = json.loads(cleaned)
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _rule_based_brain_dump_split(
    text: str, *, today: Optional[dt.date] = None
) -> List[Dict[str, Any]]:
    today = today or dt.date.today()
    raw_chunks = _SPLIT_SENTENCE_RE.split(text or "")
    items: List[Dict[str, Any]] = []
    for chunk in raw_chunks:
        line = _BULLET_PREFIX_RE.sub("", chunk).strip().rstrip("。.")
        if not line:
            continue
        parsed = parse_quick_input(line, today=today)
        title = (parsed.clean_title or line).strip()
        if not title:
            continue
        items.append(
            {
                "title": title[:200],
                "kind": "seed" if line.startswith(("たね:", "たね：", "アイデア:", "アイデア：")) or line.endswith(("したい", "作りたい")) else parsed.kind,
                "details": "",
                "due_date": parsed.due_date or today.isoformat(),
                "due_time": parsed.due_time,
                "estimate_minutes": 15 if len(title) <= 20 else 30,
                "importance": 4 if ("重要" in line or "必須" in line) else 3,
                "urgency": 4 if (parsed.due_date == today.isoformat() or "急" in line or "至急" in line) else 3,
                "status": "today",
            }
        )
    return items[:20]


def split_brain_dump(
    text: str, *, today: Optional[dt.date] = None
) -> Tuple[List[Dict[str, Any]], bool]:
    """自由記述の頭の中メモを複数の実行可能なタスク案へ分解する。

    Returns:
      (items, used_ai)
    """
    today = today or dt.date.today()
    cfg = load_config()
    if cfg.llm_api_key:
        prompt = (
            f"今日は {today.isoformat()} です。\n"
            "以下のユーザーの頭の中のメモ（ブレインダンプ）から、実行可能なタスクを漏れなく抽出し、"
            "JSON 配列のみで返してください。\n"
            "各要素は次のキーを持つオブジェクトにしてください:\n"
            '- "title": 動詞で終わる具体的な行動タイトル（40文字以内）\n'
            '- "kind": 行動は "task"、育てたいアイデアは "seed"、記録は "memo"\n'
            '- "details": 補足メモ（不要なら空文字）\n'
            '- "due_date": 期限日 "YYYY-MM-DD"（今日やるべきなら今日の年月日、不明なら null）\n'
            '- "due_time": 時刻 "HH:MM"（不明なら null）\n'
            '- "estimate_minutes": 所要時間の目安（5, 10, 15, 25, 30, 45, 60 のいずれか）\n'
            '- "importance": 1〜5 の整数\n'
            '- "urgency": 1〜5 の整数\n\n'
            f"入力:\n{text.strip()}"
        )
        try:
            raw = llm.generate_text(prompt, temperature=0.2)
            parsed_list = _extract_json_array(raw)
            normalized: List[Dict[str, Any]] = []
            for entry in parsed_list[:20]:
                raw_title = str(entry.get("title") or "").strip()
                if not raw_title:
                    continue
                fallback = parse_quick_input(raw_title, today=today)
                due_date = entry.get("due_date") or fallback.due_date
                due_time = entry.get("due_time") or fallback.due_time
                est = entry.get("estimate_minutes")
                try:
                    est_int = max(5, min(240, int(est))) if est is not None else 15
                except (TypeError, ValueError):
                    est_int = 15
                normalized.append(
                    {
                        "title": raw_title[:200],
                        "kind": entry.get("kind") if entry.get("kind") in {"task", "seed", "memo"} else fallback.kind,
                        "details": str(entry.get("details") or "")[:1000],
                        "due_date": str(due_date) if due_date else today.isoformat(),
                        "due_time": str(due_time) if due_time else None,
                        "estimate_minutes": est_int,
                        "importance": max(1, min(5, int(entry.get("importance") or 3))),
                        "urgency": max(1, min(5, int(entry.get("urgency") or 3))),
                        "status": "today",
                    }
                )
            if normalized:
                return normalized, True
        except Exception:
            pass
    return _rule_based_brain_dump_split(text, today=today), False


def breakdown_task_steps(task_row: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
    """1件のタスクを3〜5個の小さな最初の一歩に分解する。"""
    title = task_title(task_row)
    details = str(task_row.get("raw_text") or "")
    cfg = load_config()
    if cfg.llm_api_key:
        try:
            steps = task_ai.breakdown_task(title, details=details)
            if steps:
                return (
                    [
                        {
                            "title": str(s.get("title") or s).strip(),
                            "estimate_minutes": int(s.get("estimate_minutes") or 10)
                            if isinstance(s, dict)
                            else 10,
                        }
                        for s in steps
                        if (s.get("title") if isinstance(s, dict) else str(s)).strip()
                    ],
                    True,
                )
        except Exception:
            pass
    return (
        [
            {"title": f"「{title}」に必要な情報・資料を開く", "estimate_minutes": 5},
            {"title": f"「{title}」の最初のたたき台を10分だけ進める", "estimate_minutes": 10},
            {"title": f"「{title}」を見直して完了にする", "estimate_minutes": 10},
        ],
        False,
    )


def pick_next_action(
    tasks: Sequence[Mapping[str, Any]], *, today: Optional[dt.date] = None
) -> Optional[Dict[str, Any]]:
    """今日・期限超過タスクの中から「いま着手すべき1件」と理由を返す。"""
    today = today or dt.date.today()
    today_iso = today.isoformat()
    candidates = [t for t in tasks if t.get("status") != "done"]
    if not candidates:
        return None

    def _sort_key(row: Mapping[str, Any]) -> Tuple[int, float, str, int]:
        due = str(row.get("due_date") or "9999-12-31")
        is_overdue = 0 if (due < today_iso) else (1 if due == today_iso else 2)
        imp = int(row.get("importance") or 3)
        urg = int(row.get("urgency") or 3)
        eff = row.get("effort") or "medium"
        eng = row.get("energy") or "mid"
        score = priority_score(imp, urg, eff, eng)
        due_time = str(row.get("due_time") or "99:99")
        return (is_overdue, -score, due_time, int(row["id"]))

    best = sorted(candidates, key=_sort_key)[0]
    due = str(best.get("due_date") or "")
    due_time = best.get("due_time")
    est = best.get("estimate_minutes")
    if due and due < today_iso:
        reason = f"期限（{due}）を過ぎているため、最優先で片付けましょう"
    elif due_time:
        reason = f"本日 {due_time} の予定・締切に向けて今すぐ着手するのがおすすめです"
    elif est and int(est) <= 15:
        reason = f"約 {int(est)} 分で完了できるため、最初の勢いづけに最適です"
    elif int(best.get("importance") or 3) >= 4:
        reason = "重要度が高いタスクです。集中できるうちに一歩進めましょう"
    else:
        reason = "今日やるリストの中で最も優先度が高いタスクです"
    return {
        "task_id": int(best["id"]),
        "title": task_title(best),
        "reason": reason,
        "estimate_minutes": est,
        "due_date": best.get("due_date"),
        "due_time": due_time,
    }
