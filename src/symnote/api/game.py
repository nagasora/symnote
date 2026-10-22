"""Authenticated game views over the existing local-first domain services."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, StrictInt
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from symnote.api.presenters import present_memo
from symnote.core import db, delegation, focus, goals, quest, secretary, weekly_review


def jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [jsonable(item) for item in value]
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def player_payload(today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    return {
        **asdict(quest.get_player_stats(today)),
        "rest_days_used_this_week": quest.streak_info(today).rest_days_used_this_week,
        "buddy": asdict(secretary.load_settings()),
    }


def reward_payload(task_id: int) -> dict[str, Any] | None:
    reward = quest.reward_for_task(task_id)
    if reward is None or not quest.mark_reward_seen(task_id):
        return None
    result = asdict(reward)
    titles = {item.code: item.title for item in quest.list_achievements()}
    result["new_achievements"] = [
        {"code": code, "title": titles.get(code, code)} for code in reward.new_achievements
    ]
    return result


def daily_state(today: dt.date) -> dict[str, Any]:
    day = today.isoformat()
    with db.connection() as conn:
        remaining = int(
            conn.execute(
                "SELECT COUNT(*) FROM items WHERE kind = 'task' AND status != 'done' "
                "AND (status = 'today' OR due_date <= ?)",
                (day,),
            ).fetchone()[0]
        )
        done = int(
            conn.execute(
                "SELECT COUNT(*) FROM items WHERE kind = 'task' AND status = 'done' "
                "AND substr(completed_at,1,10) = ?",
                (day,),
            ).fetchone()[0]
        )
        claimed = (
            conn.execute(
                "SELECT 1 FROM quest_bonus_log WHERE kind='daily_clear' AND ref=?", (day,)
            ).fetchone()
            is not None
        )
    return {"target": remaining + done, "done": done, "claimed": claimed}


def calendar_payload(user: Any, today: dt.date) -> tuple[list[dict[str, Any]], int | None]:
    from symnote.core import calendar_events, calendar_settings, google_calendar
    from symnote.core.runtime import is_test_mode

    if not user.is_owner or is_test_mode() or not google_calendar.is_connected():
        return [], None
    config = calendar_settings.effective_config()
    result = calendar_events.fetch_events(today, today + dt.timedelta(days=1), config=config)
    if result.fetched_at is None:
        return [], None
    zone = ZoneInfo(config.google_calendar_timezone)
    begin = dt.datetime.combine(today, dt.time.min, zone)
    finish = dt.datetime.combine(today + dt.timedelta(days=1), dt.time.min, zone)
    events, busy = [], []
    for event in result.events:
        if event.get("status") == "cancelled" or calendar_events.is_symnote_generated(event):
            continue
        try:
            start, end = calendar_events.event_bounds(event, config.google_calendar_timezone)
        except (KeyError, ValueError, TypeError):
            continue
        if end <= begin or start >= finish:
            continue
        events.append(
            {
                "id": event.get("id"),
                "summary": event.get("summary") or "予定",
                "start": start.strftime("%H:%M"),
                "end": end.strftime("%H:%M"),
                "is_all_day": "date" in event["start"],
                "html_link": event.get("htmlLink"),
            }
        )
        declined = any(
            person.get("self") and person.get("responseStatus") == "declined"
            for person in event.get("attendees", [])
        )
        if event.get("transparency") != "transparent" and not declined:
            busy.append((max(start, begin), min(end, finish)))
    cursor, occupied = begin, 0.0
    for start, end in sorted(busy):
        if end > cursor:
            occupied += (end - max(cursor, start)).total_seconds()
            cursor = end
    return events, max(0, int(((finish - begin).total_seconds() - occupied) / 60))


def today_extras(
    payload: dict[str, Any], today: dt.date, *, free_minutes: int | None = None
) -> dict[str, Any]:
    now = dt.datetime.now()
    player = player_payload(today)
    tasks = payload["overdue"] + payload["due_today"]
    planned = sum(task.get("estimate_minutes") or 15 for task in tasks)
    # An empty/unavailable calendar is not evidence of an entirely free day.
    free = free_minutes
    buddy = player["buddy"]
    with db.connection() as conn:
        last = conn.execute("SELECT MAX(completed_at) FROM quest_log").fetchone()[0]
        earned = conn.execute(
            "SELECT COALESCE(SUM(xp),0) FROM quest_log WHERE substr(completed_at,1,10)=?",
            (today.isoformat(),),
        ).fetchone()[0]
    period = "morning" if now.hour < 12 else "evening" if now.hour >= 18 else "day"
    if last and (today - dt.date.fromisoformat(str(last)[:10])).days >= 3:
        line = "おかえり。まずは1つだけ、いっしょにやろう"
    elif payload["overdue"]:
        line = f"持ち越しが{len(payload['overdue'])}つ。小さくしてもいいよ"
    elif free is not None and planned > free:
        line = "ちょっと多いかも。1つ あしたにする？"
    elif period == "evening":
        line = f"おつかれさま。きょうは{player['completed_today']}つクリア、+{earned} XP"
    else:
        greeting = "おはよう" if period == "morning" else "いっしょに進もう"
        line = f"{greeting}、{buddy['user_name']}。きょうは{min(3, len(tasks))}つで十分だよ"
    if buddy["tone"] == "執事":
        line = line.replace("いいよ", "よろしいですよ").replace("やろう", "進めましょう")
    elif buddy["tone"] == "元気":
        line += "！"
    return {
        "player": player,
        "capacity": {"planned_minutes": planned, "free_minutes": free},
        "daily_clear": daily_state(today),
        "period": period,
        "buddy_line": line,
    }


class RewardCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    cost: StrictInt = Field(ge=1, le=1_000_000)


class SeedCreate(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


class SeedPlanRequest(BaseModel):
    why: str = Field(default="", max_length=4000)
    done_when: str = Field(default="", max_length=4000)
    target_date: str | None = None


class SeedAdopt(BaseModel):
    plan: dict[str, Any]
    today_first: bool = False


class BuddyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    tone: Literal["やさしい", "元気", "クール", "執事"] | None = None
    user_name: str | None = Field(default=None, min_length=1, max_length=40)
    sound_enabled: bool | None = None


class FocusStart(BaseModel):
    task_id: StrictInt = Field(gt=0)
    minutes: Literal[15, 25, 50] = 25


class FocusFinish(BaseModel):
    complete_task: bool = False


async def body(request: Request, model: type[BaseModel]) -> Any:
    return model.model_validate(await request.json())


def player_get(_request: Request, _user: Any) -> Any:
    return player_payload()


def achievements_get(_request: Request, _user: Any) -> Any:
    return {
        "achievements": [
            {key: value for key, value in asdict(item).items() if key != "icon"}
            for item in quest.list_achievements()
        ]
    }


def rewards_get(_request: Request, _user: Any) -> Any:
    return {
        "rewards": [
            {"id": item.id, "title": item.title, "cost": item.cost} for item in quest.list_rewards()
        ]
    }


async def rewards_create(request: Request, _user: Any) -> Any:
    data = await body(request, RewardCreate)
    reward_id = quest.add_reward(data.title, data.cost)
    return {"id": reward_id, **rewards_get(request, _user)}


def rewards_redeem(request: Request, _user: Any) -> Any:
    try:
        balance = quest.redeem_reward(int(request.path_params["reward_id"]))
    except quest.InsufficientGoldError as exc:
        return JSONResponse({"code": "insufficient_gold", "error": str(exc)}, status_code=409)
    return {"gold": balance, "player": player_payload()}


def rewards_delete(request: Request, _user: Any) -> Any:
    with db.connection() as conn, conn:
        changed = conn.execute(
            "UPDATE rewards SET active=0 WHERE id=? AND active=1",
            (int(request.path_params["reward_id"]),),
        )
        if not changed.rowcount:
            raise LookupError("ごほうびが見つかりません。")
    return {"ok": True}


def history_get(request: Request, _user: Any) -> Any:
    days = int(request.query_params.get("days", "28"))
    if not 1 <= days <= 366:
        raise ValueError("days は1〜366です。")
    return {"days": quest.quest_history(days)}


def reports_get(_request: Request, _user: Any) -> Any:
    return {"reports": delegation.list_pending_reports()}


def report_review(request: Request, _user: Any) -> Any:
    report_id = int(request.path_params["report_id"])
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM agent_reports WHERE id=?", (report_id,)).fetchone()
    if row is None:
        raise LookupError("報告が見つかりません。")
    accepted = request.url.path.endswith("/approve")
    changed = (
        delegation.approve_report(report_id) if accepted else delegation.reject_report(report_id)
    )
    if not changed:
        return JSONResponse(
            {"code": "conflict", "error": "この報告は確認済みです。"}, status_code=409
        )
    return {"ok": True, "triage_bonus": jsonable(quest.award_triage(f"report:{report_id}"))}


def seeds_get(_request: Request, _user: Any) -> Any:
    return {"seeds": [present_memo(row) for row in db.fetch_seeds()]}


async def seeds_create(request: Request, _user: Any) -> Any:
    data = await body(request, SeedCreate)
    if not data.text.strip():
        raise ValueError("たねを書いてね。")
    item_id = db.insert_item(kind="memo", raw_text=data.text.strip(), ai_category="idea")
    return {"seed": present_memo(db.fetch_item(item_id))}


def memo_to_seed(request: Request, _user: Any) -> Any:
    item_id = int(request.path_params["memo_id"])
    with db.connection() as conn, conn:
        changed = conn.execute(
            "UPDATE items SET ai_category='idea' WHERE id=? AND kind='memo' AND goal_id IS NULL",
            (item_id,),
        )
        if not changed.rowcount:
            raise LookupError("まだ地図になっていないメモを選んでね。")
    return {"seed": present_memo(db.fetch_item(item_id))}


def seed_row(seed_id: int) -> dict[str, Any]:
    row = db.fetch_item(seed_id)
    if (
        row is None
        or row["kind"] != "memo"
        or row.get("goal_id") is not None
        or row.get("ai_category") not in {"idea", "someday"}
    ):
        raise LookupError("たねが見つかりません。")
    return row


async def seeds_plan(request: Request, _user: Any) -> Any:
    data = await body(request, SeedPlanRequest)
    seed = seed_row(int(request.path_params["seed_id"]))
    plan = goals.propose_seed_plan(
        present_memo(seed)["title"],
        data.why,
        data.done_when,
        data.target_date,
        today=dt.date.today(),
    )
    return jsonable(plan)


async def seeds_adopt(request: Request, _user: Any) -> Any:
    data = await body(request, SeedAdopt)
    return goals.adopt_seed_plan(
        int(request.path_params["seed_id"]), data.plan, today_first=data.today_first
    )


def maps_get(_request: Request, _user: Any) -> Any:
    maps = []
    for item in goals.list_goals_with_progress():
        if item.parent_id is None:
            maps.append(map_payload(item.id, item.stale_days))
    return {"maps": jsonable(maps)}


def map_get(request: Request, _user: Any) -> Any:
    goal_id = int(request.path_params["goal_id"])
    progress = next((item for item in goals.list_goals_with_progress() if item.id == goal_id), None)
    return map_payload(goal_id, progress.stale_days if progress else None)


def map_payload(goal_id: int, stale_days: int | None) -> dict[str, Any]:
    view = jsonable(goals.list_map(goal_id))
    stages = view["stages"]
    current = next(
        (index + 1 for index, stage in enumerate(stages) if stage["is_current"]), len(stages)
    )
    return {
        **view,
        "id": goal_id,
        "progress_percent": round(view["progress"] * 100),
        "stale_days": stale_days,
        "current_stage_number": current,
        "stage_count": len(stages),
    }


def review_get(_request: Request, _user: Any) -> Any:
    return {"review": jsonable(weekly_review.generate_weekly_review())}


async def buddy_patch(request: Request, _user: Any) -> Any:
    data = await body(request, BuddyPatch)
    values = asdict(secretary.load_settings())
    values.update(data.model_dump(exclude_none=True))
    secretary.save_settings(secretary.SecretarySettings(**values))
    return {"buddy": asdict(secretary.load_settings())}


def focus_get(_request: Request, _user: Any) -> Any:
    return {"session": jsonable(focus.active_session())}


async def focus_start(request: Request, _user: Any) -> Any:
    data = await body(request, FocusStart)
    focus.start_session(data.task_id, data.minutes)
    return focus_get(request, _user)


async def focus_end(request: Request, _user: Any) -> Any:
    session_id = int(request.path_params["session_id"])
    completed = False
    if request.url.path.endswith("/stop"):
        result = focus.stop_session(session_id)
    else:
        data = await body(request, FocusFinish)
        result = focus.finish_session(session_id, complete_task=data.complete_task)
        completed = data.complete_task
    reward = (
        reward_payload(result.session.task_id) if completed and result.session.task_id else None
    )
    daily = quest.award_daily_clear_if_eligible(dt.date.today()) if completed else None
    return {
        **jsonable(result),
        "reward": reward,
        "daily_clear": jsonable(daily),
        "player": player_payload(),
    }


def routes(with_auth: Callable[..., Any]) -> list[Route]:
    specs = [
        ("/api/player", player_get, ["GET"]),
        ("/api/achievements", achievements_get, ["GET"]),
        ("/api/rewards", rewards_get, ["GET"]),
        ("/api/rewards", rewards_create, ["POST"]),
        ("/api/rewards/{reward_id:int}/redeem", rewards_redeem, ["POST"]),
        ("/api/rewards/{reward_id:int}", rewards_delete, ["DELETE"]),
        ("/api/quests/history", history_get, ["GET"]),
        ("/api/reports", reports_get, ["GET"]),
        ("/api/reports/{report_id:int}/approve", report_review, ["POST"]),
        ("/api/reports/{report_id:int}/reject", report_review, ["POST"]),
        ("/api/seeds", seeds_get, ["GET"]),
        ("/api/seeds", seeds_create, ["POST"]),
        ("/api/memos/{memo_id:int}/to-seed", memo_to_seed, ["POST"]),
        ("/api/seeds/{seed_id:int}/plan", seeds_plan, ["POST"]),
        ("/api/seeds/{seed_id:int}/adopt", seeds_adopt, ["POST"]),
        ("/api/maps", maps_get, ["GET"]),
        ("/api/maps/{goal_id:int}", map_get, ["GET"]),
        ("/api/review/weekly", review_get, ["GET"]),
        ("/api/settings/buddy", buddy_patch, ["PATCH"]),
        ("/api/focus", focus_get, ["GET"]),
        ("/api/focus", focus_start, ["POST"]),
        ("/api/focus/{session_id:int}/finish", focus_end, ["POST"]),
        ("/api/focus/{session_id:int}/stop", focus_end, ["POST"]),
    ]
    return [Route(path, with_auth(handler), methods=methods) for path, handler, methods in specs]
