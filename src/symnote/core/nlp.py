from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, List, Mapping, Sequence, Dict

import google.generativeai as genai
import json
from symnote.config import load_config


Category = str
Effort = str
Energy = str
StatusSuggestion = str
TaskRow = Mapping[str, Any]

DUE_SOON_DAYS = 2


@dataclass(frozen=True)
class ClassificationResult:
    ai_category: Category
    importance: int
    urgency: int
    effort: Effort
    energy: Energy
    status_suggestion: StatusSuggestion


@dataclass(frozen=True)
class TodaySuggestion:
    task_id: int
    raw_text: str
    reason: str
    score: float
    recommended_status: StatusSuggestion
    due: str | None


def _score_importance(text: str) -> int:
    text_lower = text.lower()
    score = 3
    for keyword in ["deadline", "締切", "due", "発表", "presentation", "提出"]:
        if keyword in text_lower:
            score += 1
    if "research" in text_lower or "論文" in text_lower:
        score += 1
    return max(1, min(5, score))


def _score_urgency(text: str) -> int:
    text_lower = text.lower()
    score = 2
    for keyword in ["today", "今日", "asap", "urgent", "急", "明日", "tomorrow"]:
        if keyword in text_lower:
            score += 2
    for keyword in ["来週", "next week", "来月", "later"]:
        if keyword in text_lower:
            score -= 1
    return max(1, min(5, score))


def _estimate_effort(text: str) -> Effort:
    length = len(text.strip())
    if length < 40:
        return "short"
    if length < 120:
        return "medium"
    return "long"


def _estimate_energy(text: str) -> Energy:
    text_lower = text.lower()
    if any(k in text_lower for k in ["write", "draft", "設計", "設計書", "brainstorm", "分析"]):
        return "high"
    if any(k in text_lower for k in ["email", "連絡", "返信", "整理", "片付け"]):
        return "low"
    return "mid"


def _status_from_priority(importance: int, urgency: int) -> StatusSuggestion:
    score = importance + urgency
    if score >= 8:
        return "today"
    if score >= 6:
        return "week"
    return "inbox"


def classify_text_rule_based(text: str) -> ClassificationResult:
    """Rule based classification for MVP."""
    text_lower = text.lower()
    if any(k in text_lower for k in ["idea", "アイデア", "構想", "maybe"]):
        ai_category: Category = "idea"
    elif any(k in text_lower for k in ["いつか", "someday", "そのうち"]):
        ai_category = "someday"
    else:
        ai_category = "task"

    importance = _score_importance(text)
    urgency = _score_urgency(text)
    effort = _estimate_effort(text)
    energy = _estimate_energy(text)
    status_suggestion = _status_from_priority(importance, urgency)

    return ClassificationResult(
        ai_category=ai_category,
        importance=importance,
        urgency=urgency,
        effort=effort,
        energy=energy,
        status_suggestion=status_suggestion,
    )


def priority_score(importance: int, urgency: int, effort: Effort, energy: Energy) -> float:
    """Calculate priority score based on the weighted attributes."""
    base = importance * urgency
    effort_penalty = {"short": 1.0, "medium": 0.9, "long": 0.75}[effort]
    energy_bonus = {"low": 0.95, "mid": 1.0, "high": 1.05}[energy]
    return base * effort_penalty * energy_bonus


def _parse_due_date(date_str: str | None) -> date | None:
    if not date_str:
        return None
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        return None


def suggest_today_tasks(
    tasks: Sequence[TaskRow],
    today: date | None = None,
    limit: int = 3,
) -> List[TodaySuggestion]:
    """Return AI-suggested tasks for today with reasons."""
    today = today or date.today()
    candidates: List[TodaySuggestion] = []
    for task in tasks:
        if task.get("kind") != "task":
            continue
        status = (task.get("status") or "inbox").lower()
        if status == "done":
            continue
        task_id = int(task["id"])
        importance = int(task.get("importance") or 3)
        urgency = int(task.get("urgency") or 3)
        effort: Effort = task.get("effort") or "medium"
        energy: Energy = task.get("energy") or "mid"
        raw_text = task.get("raw_text", "")
        due = _parse_due_date(task.get("date"))

        score = priority_score(importance, urgency, effort, energy)
        reasons: List[str] = []
        if due:
            if due < today:
                score *= 1.25
                reasons.append("期限を過ぎている")
            elif due == today:
                score *= 1.15
                reasons.append("期限が今日")
            elif due <= today + timedelta(days=DUE_SOON_DAYS):
                score *= 1.08
                reasons.append("期限がまもなく到来")
        if importance >= 4 or urgency >= 4:
            reasons.append("重要度/緊急度が高い")
        if effort == "short":
            reasons.append("短時間で片付けられる")
        if status != "today":
            reasons.append("まだ今日タスクに割り当てていない")
        if not reasons:
            reasons.append("バランスが良いタスク")

        recommended_status: StatusSuggestion = "today"
        if due and due > today + timedelta(days=1) and score < 10:
            recommended_status = "week"
        elif not due and score < 8:
            recommended_status = "week" if status == "inbox" else status

        candidates.append(
            TodaySuggestion(
                task_id=task_id,
                raw_text=raw_text,
                reason=" / ".join(reasons),
                score=score,
                recommended_status=recommended_status,
                due=due.isoformat() if due else None,
            )
        )

    ordered = sorted(candidates, key=lambda c: c.score, reverse=True)
    return ordered[:limit]


def summarize_and_extract_tasks_from_text(text: str) -> Dict[str, Any]:
    """Use Gemini to summarize and extract tasks from text."""
    config = load_config()
    genai.configure(api_key=config.llm_api_key)
    generation_config = genai.GenerationConfig(max_output_tokens=config.max_tokens)
    model = genai.GenerativeModel(config.llm_model, generation_config=generation_config)

    prompt = f"""
    以下のテキストを日本語で要約し、関連するタスクを抽出してください。

    テキスト:
    {text}

    出力は必ず以下のJSONフォーマットでお願いします。
    {{
        "summary": "ここに要約を記述",
        "tasks": [
            "タスク1",
            "タスク2",
            ...
        ]
    }}
    """

    try:
        response = model.generate_content(prompt)
        
        # Extract the json string from the response
        # It might be enclosed in ```json ... ```
        content = response.text
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        
        return json.loads(content)

    except Exception as e:
        print(f"Error calling Gemini API: {e}")
        return {"summary": "", "tasks": []}


def generate_todos_from_idea(idea_text: str) -> List[str]:
    """Generate a list of actionable ToDos from an idea using Gemini."""
    config = load_config()
    genai.configure(api_key=config.llm_api_key)
    generation_config = genai.GenerationConfig(max_output_tokens=config.max_tokens)
    model = genai.GenerativeModel(config.llm_model, generation_config=generation_config)

    prompt = f"""
    以下のアイデアや実現したいことから、具体的なアクション可能なToDoリスト（タスクリスト）を生成してください。
    
    アイデア/目標:
    {idea_text}
    
    出力は以下のJSONフォーマットのみでお願いします。
    {{
        "todos": [
            "具体的なタスク1",
            "具体的なタスク2",
            ...
        ]
    }}
    """

    try:
        response = model.generate_content(prompt)
        content = response.text
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        
        data = json.loads(content)
        return data.get("todos", [])
    except Exception as e:
        print(f"Error generating todos: {e}")
        return []
def analyze_source_and_generate_title(text: str) -> Dict[str, Any]:
    """Analyze source text to generate a title, summary, and initial ideas."""
    config = load_config()
    genai.configure(api_key=config.llm_api_key)
    generation_config = genai.GenerationConfig(max_output_tokens=config.max_tokens)
    model = genai.GenerativeModel(config.llm_model, generation_config=generation_config)

    prompt = f"""
    以下のテキストを分析し、プロジェクトやテーマを表す短い「タイトル」、要約、そしてそこから考えられる「アイデア/タスク」のリストを生成してください。
    アイデアは**厳選して3つだけ**提案してください。
    
    テキスト:
    {text[:10000]}  # Limit context window if needed

    出力は以下のJSONフォーマットのみでお願いします。
    {{
        "title": "短いタイトル（例: 新規アプリ開発、旅行計画）",
        "summary": "要約",
        "initial_ideas": [
            "アイデア1",
            "アイデア2",
            ...
        ]
    }}
    """

    try:
        response = model.generate_content(prompt)
        content = response.text
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        return json.loads(content)
    except Exception as e:
        print(f"Error analyzing source: {e}")
        return {"title": "無題のプロジェクト", "summary": "エラーが発生しました", "initial_ideas": []}


def brainstorm_ideas(context: str, user_query: str) -> List[str]:
    """Brainstorm more ideas based on context and user query."""
    config = load_config()
    genai.configure(api_key=config.llm_api_key)
    generation_config = genai.GenerationConfig(max_output_tokens=config.max_tokens)
    model = genai.GenerativeModel(config.llm_model, generation_config=generation_config)

    prompt = f"""
    以下のコンテキスト（背景情報）を踏まえて、ユーザーの要望に応じた具体的なアイデアやタスクをリストアップしてください。
    アイデアは**厳選して3つだけ**提案してください。
    
    コンテキスト:
    {context[:10000]}
    
    ユーザーの要望:
    {user_query}
    
    出力は以下のJSONフォーマットのみでお願いします。
    {{
        "ideas": [
            "アイデア1",
            "アイデア2",
            ...
        ]
    }}
    """

    try:
        response = model.generate_content(prompt)
        content = response.text
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        data = json.loads(content)
        return data.get("ideas", [])
    except Exception as e:
        print(f"Error brainstorming: {e}")
        return []
