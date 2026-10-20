"""SymNote Web API & SPA 配信アプリケーション（Starlette + Pydantic v2）。"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from symnote.api.ai_helpers import breakdown_task_steps, pick_next_action, split_brain_dump
from symnote.api.auth import (
    OAUTH_STATE_COOKIE_NAME,
    OAUTH_STATE_TTL_SECONDS,
    InviteRequiredError,
    apply_security_headers,
    auth_mode,
    authenticate_e2e_user,
    authenticate_passcode,
    build_google_authorization_url,
    clear_session_cookie,
    complete_google_callback,
    create_session,
    e2e_mode_enabled,
    google_oauth_config,
    is_https_request,
    resolve_request_user,
    revoke_session,
    set_session_cookie,
    verify_csrf,
)
from symnote.api.presenters import (
    goal_title_map,
    present_candidate,
    present_goal,
    present_memo,
    present_task,
    present_tasks,
    present_unsorted,
)
from symnote.api.schemas import (
    BrainDumpCommitRequest,
    BrainDumpSplitRequest,
    CandidateApproveRequest,
    CaptureCreateRequest,
    CapturePreviewRequest,
    E2ELoginRequest,
    GoalBreakdownCommitRequest,
    GoalCreateRequest,
    GoalPatchRequest,
    InviteCreateRequest,
    MemoCreateRequest,
    MemoPatchRequest,
    OnboardingRequest,
    PasscodeLoginRequest,
    TaskCreateRequest,
    TaskPatchRequest,
    TaskReopenRequest,
    UnsortedSortRequest,
)
from symnote.core import candidates as candidate_store
from symnote.core import db, goals, mindmap, usage_limit
from symnote.core.accounts import (
    UserRecord,
    complete_onboarding,
    invite_user,
    list_invites,
    list_users,
    revoke_invite,
    user_scope,
)
from symnote.core.overview import task_overview
from symnote.core.quick_parse import parse_quick_input
from symnote.core.runtime import app_passcode

WEEKDAYS_JA = ("月", "火", "水", "木", "金", "土", "日")
logger = logging.getLogger(__name__)


def _json(data: Any, status_code: int = 200) -> JSONResponse:
    resp = JSONResponse(data, status_code=status_code)
    return apply_security_headers(resp)


def _error(message: str, status_code: int = 400, *, code: str = "bad_request") -> JSONResponse:
    return _json({"error": {"code": code, "message": message}}, status_code=status_code)


async def _parse_body(request: Request, model_cls: type[BaseModel]) -> Any:
    try:
        raw = await request.body()
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception as exc:
        raise ValueError("JSON の形式が正しくありません。") from exc
    if not isinstance(payload, dict):
        raise ValueError("リクエスト本文は JSON オブジェクトである必要があります。")
    return model_cls.model_validate(payload)


def _user_dict(user: UserRecord) -> Dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
        "is_owner": user.is_owner,
        "onboarded": bool(user.onboarded_at),
    }


def _compose_task_raw_text(title: str, details: str) -> str:
    clean_title = " ".join((title or "").split())
    clean_details = (details or "").strip()
    if clean_details and clean_details != clean_title:
        return f"{clean_title}\n\n{clean_details}"
    return clean_title


def _calendar_sync_for_owner(user: UserRecord, action: str, task_id: int) -> None:
    """オーナーのタスクで Google Calendar 自動同期が有効な場合のみベストエフォートで同期する。"""
    if not user.is_owner:
        return
    try:
        from symnote.core import google_calendar

        if action == "upsert":
            google_calendar.auto_sync_task_upsert(task_id)
        elif action == "complete":
            google_calendar.auto_sync_task_complete(task_id)
        elif action == "delete":
            google_calendar.auto_sync_task_delete(task_id)
    except Exception:
        pass


def _with_auth(
    handler: Callable[[Request, UserRecord], Any],
    *,
    owner_only: bool = False,
) -> Callable[[Request], Any]:
    async def endpoint(request: Request) -> Response:
        csrf_err = verify_csrf(request)
        if csrf_err:
            return _error(csrf_err, status_code=403, code="csrf_failed")
        user = resolve_request_user(request)
        if user is None:
            return _error("ログインが必要です。", status_code=401, code="unauthenticated")
        if owner_only and not user.is_owner:
            return _error("この操作はオーナーのみ実行できます。", status_code=403, code="forbidden")
        try:
            with user_scope(user):
                result = handler(request, user)
                if hasattr(result, "__await__"):
                    result = await result
                if isinstance(result, Response):
                    return apply_security_headers(result)
                return _json(result)
        except ValidationError as exc:
            first_msg = exc.errors()[0].get("msg", "入力内容を確認してください。") if exc.errors() else str(exc)
            return _error(first_msg, status_code=400, code="validation_error")
        except candidate_store.CandidateStateError as exc:
            return _error(str(exc), status_code=409, code="conflict")
        except LookupError as exc:
            return _error(str(exc), status_code=404, code="not_found")
        except PermissionError as exc:
            return _error(str(exc), status_code=403, code="forbidden")
        except usage_limit.UsageLimitExceeded as exc:
            return _error(str(exc), status_code=429, code="rate_limited")
        except ValueError as exc:
            return _error(str(exc), status_code=400, code="bad_request")

    return endpoint


# --- Health & Auth endpoints ---


async def health_endpoint(_request: Request) -> Response:
    return _json({"ok": True})


async def auth_me_endpoint(request: Request) -> Response:
    user = resolve_request_user(request)
    return _json(
        {
            "authenticated": user is not None,
            "mode": auth_mode(),
            "google_enabled": google_oauth_config() is not None,
            "passcode_enabled": bool(app_passcode()),
            "e2e_enabled": e2e_mode_enabled(),
            "user": _user_dict(user) if user else None,
            "csrf_header": "X-SymNote",
        }
    )


async def auth_login_endpoint(request: Request) -> Response:
    csrf_err = verify_csrf(request)
    if csrf_err:
        return _error(csrf_err, status_code=403, code="csrf_failed")
    try:
        body = await _parse_body(request, PasscodeLoginRequest)
        user = authenticate_passcode(request, body.passcode)
        token = create_session(user.id)
        resp = _json({"authenticated": True, "user": _user_dict(user)})
        set_session_cookie(resp, request, token)
        return resp
    except PermissionError as exc:
        return _error(str(exc), status_code=401, code="invalid_credentials")
    except ValueError as exc:
        return _error(str(exc), status_code=400, code="bad_request")


async def auth_e2e_login_endpoint(request: Request) -> Response:
    csrf_err = verify_csrf(request)
    if csrf_err:
        return _error(csrf_err, status_code=403, code="csrf_failed")
    try:
        body = await _parse_body(request, E2ELoginRequest)
        user = authenticate_e2e_user(
            request,
            email=body.email,
            name=body.name,
            is_owner=body.is_owner,
        )
        token = create_session(user.id)
        resp = _json({"authenticated": True, "user": _user_dict(user)})
        set_session_cookie(resp, request, token)
        return resp
    except InviteRequiredError as exc:
        return _error(str(exc), status_code=403, code="not_invited")
    except PermissionError as exc:
        return _error(str(exc), status_code=403, code="forbidden")
    except (ValidationError, ValueError) as exc:
        return _error(str(exc), status_code=400, code="bad_request")


async def auth_logout_endpoint(request: Request) -> Response:
    csrf_err = verify_csrf(request)
    if csrf_err:
        return _error(csrf_err, status_code=403, code="csrf_failed")
    token = request.cookies.get("symnote_session", "")
    if token:
        revoke_session(token)
    resp = _json({"ok": True})
    clear_session_cookie(resp)
    return resp


async def auth_google_start_endpoint(request: Request) -> Response:
    try:
        url = build_google_authorization_url(request)
        resp = RedirectResponse(url, status_code=302)
        resp.set_cookie(
            OAUTH_STATE_COOKIE_NAME,
            parse_qs(urlparse(url).query)["state"][0],
            max_age=OAUTH_STATE_TTL_SECONDS,
            httponly=True,
            secure=is_https_request(request),
            samesite="lax",
            path="/api/auth/google",
        )
        return apply_security_headers(resp)
    except Exception as exc:
        return _error(str(exc), status_code=400, code="oauth_unavailable")


async def auth_google_callback_endpoint(request: Request) -> Response:
    state = request.query_params.get("state", "").strip()
    code = request.query_params.get("code", "").strip()
    err = request.query_params.get("error", "").strip()
    if err:
        return apply_security_headers(RedirectResponse("/?auth_error=cancelled", status_code=302))
    if not state or not code:
        return apply_security_headers(RedirectResponse("/?auth_error=invalid_callback", status_code=302))
    try:
        user = complete_google_callback(
            state, code, request.cookies.get(OAUTH_STATE_COOKIE_NAME, "")
        )
        token = create_session(user.id)
        resp = RedirectResponse("/", status_code=302)
        set_session_cookie(resp, request, token)
        resp.delete_cookie(OAUTH_STATE_COOKIE_NAME, path="/api/auth/google")
        return apply_security_headers(resp)
    except InviteRequiredError:
        return apply_security_headers(RedirectResponse("/?auth_error=not_invited", status_code=302))
    except Exception as exc:
        logger.warning(
            "Google login callback failed: type=%s status=%s cause=%s",
            type(exc).__name__,
            getattr(exc, "code", None),
            type(getattr(exc, "reason", None)).__name__,
        )
        return apply_security_headers(RedirectResponse("/?auth_error=failed", status_code=302))


async def auth_onboarding_handler(request: Request, user: UserRecord) -> Any:
    body: OnboardingRequest = await _parse_body(request, OnboardingRequest)
    updated_user = complete_onboarding(user.id, name=body.name)
    if body.seed_sample_tasks and not db.fetch_tasks(limit=1):
        today_iso = dt.date.today().isoformat()
        db.insert_task(
            raw_text="⌘K（または右下の＋）から思いついたことを1行で追加してみる",
            tags="⌘K（または右下の＋）から思いついたことを1行で追加してみる",
            due_date=today_iso,
            status="today",
            estimate_minutes=5,
        )
        db.insert_task(
            raw_text="「頭の中を吐き出す」に気になっていることを書いてAIでタスク分解する",
            tags="「頭の中を吐き出す」に気になっていることを書いてAIでタスク分解する",
            due_date=today_iso,
            status="today",
            estimate_minutes=10,
        )
    return {"user": _user_dict(updated_user)}


# --- Today & Secretary ---


def today_get_handler(request: Request, user: UserRecord) -> Any:
    date_param = request.query_params.get("date", "").strip()
    target_date = dt.date.fromisoformat(date_param) if date_param else dt.date.today()
    today_iso = target_date.isoformat()

    all_tasks = db.fetch_tasks(limit=500)
    overview = task_overview(all_tasks, today=target_date)

    overdue_rows: List[Dict[str, Any]] = []
    due_today_rows: List[Dict[str, Any]] = []
    seen_today_ids: set[int] = set()

    for row in all_tasks:
        if row.get("status") == "done":
            continue
        due = row.get("due_date")
        tid = int(row["id"])
        if due and due < today_iso:
            overdue_rows.append(row)
        elif due == today_iso or row.get("status") == "today":
            if tid not in seen_today_ids:
                due_today_rows.append(row)
                seen_today_ids.add(tid)

    completed_today_rows = db.fetch_tasks_completed_on(today_iso)

    combined_for_Presentation = overdue_rows + due_today_rows + completed_today_rows
    task_ids = [int(r["id"]) for r in combined_for_Presentation]
    sources = db.fetch_task_sources(task_ids)
    g_titles = goal_title_map()

    overdue_items = [present_task(r, goal_titles=g_titles, sources=sources) for r in overdue_rows]
    due_today_items = [present_task(r, goal_titles=g_titles, sources=sources) for r in due_today_rows]
    completed_today_items = [
        present_task(r, goal_titles=g_titles, sources=sources) for r in completed_today_rows
    ]

    next_action = pick_next_action(overdue_rows + due_today_rows, today=target_date)

    calendar_events: List[Dict[str, Any]] = []
    if user.is_owner:
        try:
            from symnote.core.calendar_events import list_events_for_date

            res = list_events_for_date(target_date)
            if res.ok:
                for ev in res.events:
                    calendar_events.append(
                        {
                            "id": ev.event_id,
                            "summary": ev.summary,
                            "start": ev.start_label,
                            "end": ev.end_label,
                            "is_all_day": ev.all_day,
                            "html_link": ev.html_link,
                        }
                    )
        except Exception:
            pass

    pending_candidates = candidate_store.count_pending_candidates()
    unsorted_cnt = db.count_unsorted()

    return {
        "today": today_iso,
        "weekday_ja": WEEKDAYS_JA[target_date.weekday()],
        "overview": overview,
        "next_action": next_action,
        "overdue": overdue_items,
        "due_today": due_today_items,
        "completed_today": completed_today_items,
        "calendar_events": calendar_events,
        "pending_candidates_count": pending_candidates,
        "unsorted_count": unsorted_cnt,
        "inbox_badge_count": pending_candidates + unsorted_cnt,
    }


def today_plan_handler(_request: Request, _user: UserRecord) -> Any:
    today = dt.date.today()
    all_active = db.fetch_tasks(statuses=["inbox", "today", "week"], limit=200)
    pick = pick_next_action(all_active, today=today)
    # 期限が今日または重要度が高い上位3件を「今日の3選」として提案
    top_ids: List[int] = []
    for row in all_active:
        if len(top_ids) >= 3:
            break
        if row.get("due_date") and row["due_date"] <= today.isoformat():
            top_ids.append(int(row["id"]))
    for row in all_active:
        if len(top_ids) >= 3:
            break
        tid = int(row["id"])
        if tid not in top_ids:
            top_ids.append(tid)
    return {
        "next_action": pick,
        "recommended_task_ids": top_ids,
    }


# --- Tasks ---


def tasks_list_handler(request: Request, _user: UserRecord) -> Any:
    status_param = request.query_params.get("status", "active").strip()
    q = request.query_params.get("q", "").strip()
    goal_id_str = request.query_params.get("goal_id", "").strip()
    due_from = request.query_params.get("due_from", "").strip()
    due_to = request.query_params.get("due_to", "").strip()
    include_done = request.query_params.get("include_done", "true").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    try:
        limit = max(1, min(500, int(request.query_params.get("limit", "200"))))
    except ValueError:
        limit = 200

    if due_from and due_to:
        rows = db.fetch_tasks_due_between(due_from, due_to, include_done=include_done)
    elif q:
        rows = db.search_items(q, kinds=["task"], include_done=include_done, limit=limit)
    else:
        if status_param == "active":
            statuses: Optional[List[str]] = ["inbox", "today", "week"]
        elif status_param in {"all", ""}:
            statuses = None
        else:
            statuses = [s.strip() for s in status_param.split(",") if s.strip()]
        rows = db.fetch_tasks(statuses=statuses, limit=limit)

    if goal_id_str:
        if goal_id_str == "none":
            rows = [r for r in rows if r.get("goal_id") is None]
        elif goal_id_str.isdigit():
            gid = int(goal_id_str)
            rows = [r for r in rows if r.get("goal_id") == gid]

    return {"tasks": present_tasks(rows)}


async def tasks_create_handler(request: Request, user: UserRecord) -> Any:
    body: TaskCreateRequest = await _parse_body(request, TaskCreateRequest)
    clean_title = " ".join(body.title.split())
    if not clean_title:
        raise ValueError("タイトルを入力してください。")
    raw_text = _compose_task_raw_text(clean_title, body.details)
    norm_due = candidate_store.normalize_due_date(body.due_date)
    norm_time = candidate_store.normalize_due_time(body.due_time)

    if body.recurrence is not None:
        rec = body.recurrence
        task_id = db.create_recurring_task(
            raw_text=raw_text,
            interval=rec.interval,
            due_date=norm_due or dt.date.today().isoformat(),
            due_time=norm_time,
            tags=clean_title,
            status=body.status,
            importance=body.importance,
            urgency=body.urgency,
            effort=body.effort,
            energy=body.energy,
            goal_id=body.goal_id,
            estimate_minutes=body.estimate_minutes,
            frequency=rec.frequency,
            weekdays=tuple(rec.weekdays),
            month_day=rec.month_day,
            end_date=rec.end_date,
        )
    else:
        task_id = db.insert_task(
            raw_text=raw_text,
            tags=clean_title,
            due_date=norm_due,
            due_time=norm_time,
            status=body.status,
            importance=body.importance,
            urgency=body.urgency,
            effort=body.effort,
            energy=body.energy,
            goal_id=body.goal_id,
            estimate_minutes=body.estimate_minutes,
        )
    _calendar_sync_for_owner(user, "upsert", task_id)
    row = db.fetch_item(task_id)
    assert row is not None
    return {"task": present_tasks([row])[0]}


def task_get_handler(request: Request, _user: UserRecord) -> Any:
    task_id = int(request.path_params["task_id"])
    row = db.fetch_item(task_id)
    if row is None or row.get("kind") != "task":
        raise LookupError(f"タスク #{task_id} が見つかりません。")
    return {"task": present_tasks([row])[0]}


async def task_patch_handler(request: Request, user: UserRecord) -> Any:
    task_id = int(request.path_params["task_id"])
    existing = db.fetch_item(task_id)
    if existing is None or existing.get("kind") != "task":
        raise LookupError(f"タスク #{task_id} が見つかりません。")

    body: TaskPatchRequest = await _parse_body(request, TaskPatchRequest)
    fields_set = body.model_fields_set
    updates: Dict[str, Any] = {}

    if "title" in fields_set or "details" in fields_set:
        cur_title, cur_details = present_task(existing)["title"], present_task(existing)["details"]
        new_title = " ".join((body.title if body.title is not None else cur_title).split())
        new_details = body.details if body.details is not None else cur_details
        if not new_title:
            raise ValueError("タイトルを空にはできません。")
        updates["tags"] = new_title
        updates["raw_text"] = _compose_task_raw_text(new_title, new_details)

    if "status" in fields_set and body.status is not None:
        updates["status"] = body.status
    if "due_date" in fields_set:
        updates["due_date"] = candidate_store.normalize_due_date(body.due_date)
    if "due_time" in fields_set:
        updates["due_time"] = candidate_store.normalize_due_time(body.due_time)
    if "importance" in fields_set and body.importance is not None:
        updates["importance"] = body.importance
    if "urgency" in fields_set and body.urgency is not None:
        updates["urgency"] = body.urgency
    if "effort" in fields_set:
        updates["effort"] = body.effort
    if "energy" in fields_set:
        updates["energy"] = body.energy
    if "goal_id" in fields_set:
        updates["goal_id"] = body.goal_id
    if "estimate_minutes" in fields_set:
        updates["estimate_minutes"] = body.estimate_minutes

    if updates:
        db.update_item_fields(task_id, **updates)

    if body.stop_recurrence:
        db.stop_task_recurrence(task_id)
    elif "recurrence" in fields_set and body.recurrence is not None:
        rec = body.recurrence
        interval_days = _interval_days_from_spec(rec.frequency, rec.interval)
        db.set_task_recurrence(
            task_id,
            interval_days,
            frequency=rec.frequency,
            weekdays=",".join(str(d) for d in rec.weekdays) if rec.weekdays else None,
            month_day=rec.month_day,
            end_date=rec.end_date,
        )

    _calendar_sync_for_owner(user, "upsert", task_id)
    updated_row = db.fetch_item(task_id)
    assert updated_row is not None
    return {"task": present_tasks([updated_row])[0]}


def task_delete_handler(request: Request, user: UserRecord) -> Any:
    task_id = int(request.path_params["task_id"])
    existing = db.fetch_item(task_id)
    if existing is None or existing.get("kind") != "task":
        raise LookupError(f"タスク #{task_id} が見つかりません。")
    _calendar_sync_for_owner(user, "delete", task_id)
    db.delete_item(task_id)
    return {"ok": True, "deleted_id": task_id}


def task_complete_handler(request: Request, user: UserRecord) -> Any:
    task_id = int(request.path_params["task_id"])
    existing = db.fetch_item(task_id)
    if existing is None or existing.get("kind") != "task":
        raise LookupError(f"タスク #{task_id} が見つかりません。")
    next_task_id = db.complete_task(task_id)
    _calendar_sync_for_owner(user, "complete", task_id)
    if next_task_id:
        _calendar_sync_for_owner(user, "upsert", next_task_id)
    done_row = db.fetch_item(task_id)
    next_row = db.fetch_item(next_task_id) if next_task_id else None
    rows = [r for r in (done_row, next_row) if r is not None]
    presented = present_tasks(rows)
    return {
        "task": presented[0] if presented else None,
        "next_task": presented[1] if len(presented) > 1 else None,
    }


async def task_reopen_handler(request: Request, user: UserRecord) -> Any:
    task_id = int(request.path_params["task_id"])
    body: TaskReopenRequest = await _parse_body(request, TaskReopenRequest)
    db.reopen_task(
        task_id,
        status=body.status,
        discard_next_task_id=body.discard_next_task_id,
    )
    _calendar_sync_for_owner(user, "upsert", task_id)
    row = db.fetch_item(task_id)
    assert row is not None
    return {"task": present_tasks([row])[0]}


def task_breakdown_handler(request: Request, _user: UserRecord) -> Any:
    task_id = int(request.path_params["task_id"])
    row = db.fetch_item(task_id)
    if row is None or row.get("kind") != "task":
        raise LookupError(f"タスク #{task_id} が見つかりません。")
    steps, used_ai = breakdown_task_steps(row)
    return {"subtasks": steps, "ai_used": used_ai}


# --- Memos ---


def memos_list_handler(request: Request, _user: UserRecord) -> Any:
    q = request.query_params.get("q", "").strip()
    try:
        limit = max(1, min(200, int(request.query_params.get("limit", "100"))))
    except ValueError:
        limit = 100
    if q:
        rows = db.search_items(q, kinds=["memo"], include_done=True, limit=limit)
    else:
        rows = db.fetch_memos(limit=limit)
    return {"memos": [present_memo(r) for r in rows]}


async def memos_create_handler(request: Request, _user: UserRecord) -> Any:
    body: MemoCreateRequest = await _parse_body(request, MemoCreateRequest)
    memo_id = db.insert_memo(
        raw_text=body.text.strip(),
        tags=body.tags.strip(),
        date_str=body.date,
    )
    row = db.fetch_item(memo_id)
    assert row is not None
    return {"memo": present_memo(row)}


async def memo_patch_handler(request: Request, _user: UserRecord) -> Any:
    memo_id = int(request.path_params["memo_id"])
    existing = db.fetch_item(memo_id)
    if existing is None or existing.get("kind") != "memo":
        raise LookupError(f"メモ #{memo_id} が見つかりません。")
    body: MemoPatchRequest = await _parse_body(request, MemoPatchRequest)
    updates: Dict[str, Any] = {}
    if body.text is not None:
        updates["raw_text"] = body.text.strip()
    if body.tags is not None:
        updates["tags"] = body.tags.strip()
    if updates:
        db.update_item_fields(memo_id, **updates)
    row = db.fetch_item(memo_id)
    assert row is not None
    return {"memo": present_memo(row)}


def memo_delete_handler(request: Request, _user: UserRecord) -> Any:
    memo_id = int(request.path_params["memo_id"])
    existing = db.fetch_item(memo_id)
    if existing is None or existing.get("kind") != "memo":
        raise LookupError(f"メモ #{memo_id} が見つかりません。")
    db.delete_item(memo_id)
    return {"ok": True, "deleted_id": memo_id}


def memo_to_task_handler(request: Request, user: UserRecord) -> Any:
    memo_id = int(request.path_params["memo_id"])
    existing = db.fetch_item(memo_id)
    if existing is None or existing.get("kind") != "memo":
        raise LookupError(f"メモ #{memo_id} が見つかりません。")
    parsed = parse_quick_input(str(existing.get("raw_text") or ""))
    title = parsed.clean_title or present_memo(existing)["title"]
    due_date = parsed.due_date or dt.date.today().isoformat()
    db.convert_memo_to_task(
        memo_id,
        due_date=due_date,
        due_time=parsed.due_time,
        status="today",
    )
    db.update_item_fields(memo_id, tags=title[:200])
    _calendar_sync_for_owner(user, "upsert", memo_id)
    row = db.fetch_item(memo_id)
    assert row is not None
    return {"task": present_tasks([row])[0]}


# --- Quick Capture & Brain Dump ---


async def capture_preview_handler(request: Request, _user: UserRecord) -> Any:
    body: CapturePreviewRequest = await _parse_body(request, CapturePreviewRequest)
    parsed = parse_quick_input(body.text)
    rec = None
    if parsed.recurrence is not None:
        rec = {
            "frequency": parsed.recurrence.frequency,
            "interval": parsed.recurrence.interval,
            "weekdays": list(parsed.recurrence.weekdays),
            "month_day": parsed.recurrence.month_day,
            "label": parsed.recurrence.label,
        }
    chips = [{"kind": "kind", "label": {"task": "タスク", "memo": "メモ", "unsorted": "未仕分け"}[parsed.kind]}]
    if parsed.due_date:
        chips.append({"kind": "date", "label": parsed.due_date})
    if parsed.due_time:
        chips.append({"kind": "time", "label": parsed.due_time})
    if parsed.recurrence:
        chips.append({"kind": "recurrence", "label": parsed.recurrence.label})
    return {
        "kind": parsed.kind,
        "clean_title": parsed.clean_title,
        "due_date": parsed.due_date,
        "due_time": parsed.due_time,
        "recurrence": rec,
        "chips": chips,
    }


async def capture_create_handler(request: Request, user: UserRecord) -> Any:
    body: CaptureCreateRequest = await _parse_body(request, CaptureCreateRequest)
    parsed = parse_quick_input(body.text)
    target_kind = parsed.kind if body.kind == "auto" else body.kind

    if target_kind == "unsorted":
        item_id = db.insert_unsorted(body.text.strip())
        row = db.fetch_item(item_id)
        assert row is not None
        return {"kind": "unsorted", "unsorted": present_unsorted(row)}

    if target_kind == "memo":
        memo_id = db.insert_memo(raw_text=body.text.strip())
        row = db.fetch_item(memo_id)
        assert row is not None
        return {"kind": "memo", "memo": present_memo(row)}

    title = (parsed.clean_title or body.text.strip().splitlines()[0]).strip()[:200]
    due_date = candidate_store.normalize_due_date(body.due_date or parsed.due_date)
    due_time = candidate_store.normalize_due_time(body.due_time or parsed.due_time)
    status = body.status
    if status == "today" and due_date is None:
        due_date = dt.date.today().isoformat()

    if parsed.recurrence is not None:
        rec = parsed.recurrence
        task_id = db.create_recurring_task(
            raw_text=title,
            interval=rec.interval,
            due_date=due_date or dt.date.today().isoformat(),
            due_time=due_time,
            tags=title,
            status=status,
            goal_id=body.goal_id,
            estimate_minutes=body.estimate_minutes,
            frequency=rec.frequency,
            weekdays=tuple(rec.weekdays),
            month_day=rec.month_day,
        )
    else:
        task_id = db.insert_task(
            raw_text=title,
            tags=title,
            due_date=due_date,
            due_time=due_time,
            status=status,
            goal_id=body.goal_id,
            estimate_minutes=body.estimate_minutes,
        )
    _calendar_sync_for_owner(user, "upsert", task_id)
    row = db.fetch_item(task_id)
    assert row is not None
    return {"kind": "task", "task": present_tasks([row])[0]}


async def capture_split_handler(request: Request, _user: UserRecord) -> Any:
    body: BrainDumpSplitRequest = await _parse_body(request, BrainDumpSplitRequest)
    items, used_ai = split_brain_dump(body.text)
    return {"items": items, "ai_used": used_ai}


async def capture_commit_split_handler(request: Request, user: UserRecord) -> Any:
    body: BrainDumpCommitRequest = await _parse_body(request, BrainDumpCommitRequest)
    created_ids: List[int] = []
    memo_id: Optional[int] = None
    if body.save_source_memo and body.source_text.strip():
        memo_id = db.insert_memo(raw_text=body.source_text.strip(), tags="ブレインダンプ")
    for draft in body.items:
        clean_title = " ".join(draft.title.split())
        if not clean_title:
            continue
        raw_text = _compose_task_raw_text(clean_title, draft.details)
        norm_due = candidate_store.normalize_due_date(draft.due_date)
        norm_time = candidate_store.normalize_due_time(draft.due_time)
        tid = db.insert_task(
            raw_text=raw_text,
            tags=clean_title,
            due_date=norm_due,
            due_time=norm_time,
            status=draft.status,
            importance=draft.importance,
            urgency=draft.urgency,
            goal_id=draft.goal_id,
            estimate_minutes=draft.estimate_minutes,
        )
        created_ids.append(tid)
        _calendar_sync_for_owner(user, "upsert", tid)
    rows = [r for r in (db.fetch_item(tid) for tid in created_ids) if r is not None]
    return {"tasks": present_tasks(rows), "memo_id": memo_id}


# --- Inbox (AI Candidates & Unsorted) ---


def inbox_get_handler(_request: Request, _user: UserRecord) -> Any:
    pending = [present_candidate(r) for r in candidate_store.list_candidates(status="pending", limit=100)]
    approved = [present_candidate(r) for r in candidate_store.list_candidates(status="approved", limit=30)]
    rejected = [present_candidate(r) for r in candidate_store.list_candidates(status="rejected", limit=30)]
    unsorted_items = [present_unsorted(r) for r in db.fetch_unsorted(limit=100)]
    return {
        "pending_candidates": pending,
        "approved_candidates": approved,
        "rejected_candidates": rejected,
        "unsorted_items": unsorted_items,
        "badge_count": len(pending) + len(unsorted_items),
    }


def candidates_list_handler(request: Request, _user: UserRecord) -> Any:
    status = request.query_params.get("status", "pending").strip()
    if status not in {"pending", "approved", "rejected"}:
        raise ValueError("status は pending / approved / rejected のいずれかで指定してください。")
    rows = candidate_store.list_candidates(status=status, limit=100)  # type: ignore[arg-type]
    return {"candidates": [present_candidate(r) for r in rows]}


async def candidate_approve_handler(request: Request, user: UserRecord) -> Any:
    candidate_id = int(request.path_params["candidate_id"])
    body: CandidateApproveRequest = await _parse_body(request, CandidateApproveRequest)
    due_date = body.due_date
    if body.status == "today" and due_date is None:
        existing = candidate_store.get_candidate(candidate_id)
        if existing and not existing.get("due_date"):
            due_date = dt.date.today().isoformat()
    task_id = candidate_store.approve_candidate(
        candidate_id,
        title=body.title,
        details=body.details,
        due_date=due_date,
        due_time=body.due_time,
        status=body.status,
    )
    _calendar_sync_for_owner(user, "upsert", task_id)
    task_row = db.fetch_item(task_id)
    cand_row = candidate_store.get_candidate(candidate_id)
    return {
        "candidate": present_candidate(cand_row) if cand_row else None,
        "task": present_tasks([task_row])[0] if task_row else None,
    }


def candidate_reject_handler(request: Request, _user: UserRecord) -> Any:
    candidate_id = int(request.path_params["candidate_id"])
    try:
        candidate_store.reject_candidate(candidate_id)
    except LookupError as exc:
        existing = candidate_store.get_candidate(candidate_id)
        if existing is not None:
            raise candidate_store.CandidateStateError(
                f"候補 {candidate_id} は既に {existing['status']} です。"
            ) from exc
        raise
    cand_row = candidate_store.get_candidate(candidate_id)
    return {"candidate": present_candidate(cand_row) if cand_row else None}


def candidate_restore_handler(request: Request, _user: UserRecord) -> Any:
    candidate_id = int(request.path_params["candidate_id"])
    candidate_store.restore_candidate(candidate_id)
    cand_row = candidate_store.get_candidate(candidate_id)
    return {"candidate": present_candidate(cand_row) if cand_row else None}


def unsorted_list_handler(_request: Request, _user: UserRecord) -> Any:
    return {"unsorted": [present_unsorted(r) for r in db.fetch_unsorted(limit=100)]}


async def unsorted_sort_handler(request: Request, user: UserRecord) -> Any:
    item_id = int(request.path_params["item_id"])
    body: UnsortedSortRequest = await _parse_body(request, UnsortedSortRequest)
    due_date = candidate_store.normalize_due_date(body.due_date)
    due_time = candidate_store.normalize_due_time(body.due_time)
    if body.as_kind == "task" and body.status == "today" and due_date is None:
        due_date = dt.date.today().isoformat()
    db.sort_unsorted_item(
        item_id,
        as_kind=body.as_kind,
        title=body.title,
        due_date=due_date,
        due_time=due_time,
        status=body.status,
    )
    if body.as_kind == "task":
        _calendar_sync_for_owner(user, "upsert", item_id)
    row = db.fetch_item(item_id)
    assert row is not None
    if body.as_kind == "task":
        return {"kind": "task", "task": present_tasks([row])[0]}
    return {"kind": "memo", "memo": present_memo(row)}


def unsorted_delete_handler(request: Request, _user: UserRecord) -> Any:
    item_id = int(request.path_params["item_id"])
    existing = db.fetch_item(item_id)
    if existing is None:
        raise LookupError(f"項目 #{item_id} が見つかりません。")
    db.delete_item(item_id)
    return {"ok": True, "deleted_id": item_id}


# --- Goals ---


def goals_list_handler(request: Request, _user: UserRecord) -> Any:
    include_archived = request.query_params.get("include_archived", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    progress_list = goals.list_goals_with_progress(include_archived=include_archived)
    g_titles = {g.id: g.title for g in progress_list}
    return {"goals": [present_goal(g, goal_titles=g_titles) for g in progress_list]}


def _goal_context(goal_id: int) -> Dict[str, Any]:
    try:
        return goals.get_goal_context(goal_id)
    except ValueError as exc:
        raise LookupError("ゴールが見つかりません。") from exc


def goal_get_handler(request: Request, _user: UserRecord) -> Any:
    context = _goal_context(int(request.path_params["goal_id"]))
    return {"goal": {
        **context,
        "open_tasks": present_tasks(context["open_tasks"]),
        "done_tasks": present_tasks(context["done_tasks"]),
    }}


async def goal_breakdown_commit_handler(request: Request, _user: UserRecord) -> Any:
    goal_id = int(request.path_params["goal_id"])
    _goal_context(goal_id)
    body: GoalBreakdownCommitRequest = await _parse_body(request, GoalBreakdownCommitRequest)
    breakdown = goals.GoalBreakdown(
        [goals.ProposedGoal(**row.model_dump()) for row in body.subgoals],
        [goals.ProposedTask(**row.model_dump()) for row in body.tasks],
    )
    goal_ids, task_ids = goals.apply_goal_breakdown(
        goal_id, breakdown, body.selected_subgoals, body.selected_tasks,
    )
    return {"goal_ids": goal_ids, "task_ids": task_ids}


async def goals_create_handler(request: Request, _user: UserRecord) -> Any:
    body: GoalCreateRequest = await _parse_body(request, GoalCreateRequest)
    goal_id = goals.create_goal(
        title=body.title,
        why=body.why,
        success_criteria=body.success_criteria,
        target_date=body.target_date,
        parent_id=body.parent_id,
    )
    progress_list = goals.list_goals_with_progress(include_archived=True)
    g_titles = {g.id: g.title for g in progress_list}
    created = next((g for g in progress_list if g.id == goal_id), None)
    return {"goal": present_goal(created, goal_titles=g_titles) if created else {"id": goal_id}}


async def goal_patch_handler(request: Request, _user: UserRecord) -> Any:
    goal_id = int(request.path_params["goal_id"])
    body: GoalPatchRequest = await _parse_body(request, GoalPatchRequest)
    fields_set = body.model_fields_set
    updates: Dict[str, Any] = {}
    for key in ("title", "why", "success_criteria", "target_date", "status", "parent_id"):
        if key in fields_set:
            updates[key] = getattr(body, key)
    if updates:
        goals.update_goal(goal_id, **updates)
    progress_list = goals.list_goals_with_progress(include_archived=True)
    g_titles = {g.id: g.title for g in progress_list}
    updated = next((g for g in progress_list if g.id == goal_id), None)
    if updated is None:
        raise LookupError(f"ゴール #{goal_id} が見つかりません。")
    return {"goal": present_goal(updated, goal_titles=g_titles)}


def goal_delete_handler(request: Request, _user: UserRecord) -> Any:
    goal_id = int(request.path_params["goal_id"])
    goals.delete_goal(goal_id)
    return {"ok": True, "deleted_id": goal_id}


def goal_breakdown_handler(request: Request, _user: UserRecord) -> Any:
    goal_id = int(request.path_params["goal_id"])
    ctx = _goal_context(goal_id)
    title = str(ctx.get("title") or "")
    try:
        bd = goals.propose_goal_breakdown(goal_id)
        return {
            "subgoals": [{"title": sg.title, "why": sg.why} for sg in bd.subgoals],
            "tasks": [
                {
                    "title": t.title,
                    "why": t.why,
                    "due_hint": t.due_hint or "今日",
                    "estimate_minutes": t.estimate_minutes or 15,
                }
                for t in bd.tasks
            ],
            "ai_used": True,
        }
    except Exception:
        return {
            "subgoals": [],
            "tasks": [
                {
                    "title": f"「{title}」の達成条件と現状のギャップを書き出す",
                    "why": "現在地を明確にするため",
                    "due_hint": "今日",
                    "estimate_minutes": 15,
                },
                {
                    "title": f"「{title}」で今週完了できる最小の1ステップを決める",
                    "why": "今日すぐ動ける粒度に落とすため",
                    "due_hint": "今日",
                    "estimate_minutes": 15,
                },
            ],
            "ai_used": False,
        }


# --- Ideas (Mindmap) ---


def _serialize_mindmap_node(node: mindmap.MindmapNode) -> Dict[str, Any]:
    return {
        "id": node.id,
        "project_id": node.project_id,
        "parent_id": node.parent_id,
        "kind": node.kind,
        "title": node.title,
        "body": node.body,
        "depth": node.depth,
        "order_index": node.order_index,
        "linked_item_id": node.linked_item_id,
        "children": [_serialize_mindmap_node(c) for c in node.children],
    }


def ideas_get_handler(request: Request, _user: UserRecord) -> Any:
    sessions = db.fetch_idea_sessions()
    session_id_str = request.query_params.get("session_id", "").strip()
    active_id: Optional[int] = None
    if session_id_str.isdigit():
        active_id = int(session_id_str)
    elif sessions:
        active_id = int(sessions[0]["id"])

    roots: List[Dict[str, Any]] = []
    if active_id is not None:
        tree = mindmap.get_nodes_by_project(active_id)
        roots = [_serialize_mindmap_node(n) for n in tree]
    return {
        "sessions": [
            {
                "id": int(s["id"]),
                "title": s["title"],
                "summary": s.get("summary") or "",
                "created_at": s.get("created_at"),
                "updated_at": s.get("updated_at"),
            }
            for s in sessions
        ],
        "active_session_id": active_id,
        "tree": roots,
    }


async def ideas_create_session_handler(request: Request, _user: UserRecord) -> Any:
    raw = await request.body()
    payload = json.loads(raw.decode("utf-8")) if raw else {}
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("テーマのタイトルを入力してください。")
    session_id = db.insert_idea_session(title=title[:200], summary=str(payload.get("summary") or ""))
    mindmap.create_node(project_id=session_id, kind="root", title=title[:200])
    return ideas_get_handler(request, _user) | {"active_session_id": session_id}


async def ideas_add_node_handler(request: Request, _user: UserRecord) -> Any:
    session_id = int(request.path_params["session_id"])
    raw = await request.body()
    payload = json.loads(raw.decode("utf-8")) if raw else {}
    parent_id = int(payload["parent_id"])
    title = str(payload.get("title") or "").strip()
    kind = str(payload.get("kind") or "idea").strip()
    if kind not in {"idea", "task", "detail", "note"}:
        kind = "idea"
    node_id = mindmap.create_node(
        project_id=session_id,
        parent_id=parent_id,
        kind=kind,  # type: ignore[arg-type]
        title=title,
        body=str(payload.get("body") or ""),
    )
    tree = mindmap.get_nodes_by_project(session_id)
    return {"node_id": node_id, "tree": [_serialize_mindmap_node(n) for n in tree]}


async def ideas_node_to_task_handler(request: Request, user: UserRecord) -> Any:
    node_id = int(request.path_params["node_id"])
    raw = await request.body()
    payload = json.loads(raw.decode("utf-8")) if raw else {}
    due_date = payload.get("due_date") or dt.date.today().isoformat()
    task_id = mindmap.create_task_from_node(node_id, due_date=due_date)
    db.update_item_fields(task_id, status="today")
    _calendar_sync_for_owner(user, "upsert", task_id)
    row = db.fetch_item(task_id)
    assert row is not None
    return {"task": present_tasks([row])[0]}


# --- Settings & Export ---


def settings_get_handler(_request: Request, user: UserRecord) -> Any:
    snap = usage_limit.usage_snapshot()
    cal_connected = False
    if user.is_owner:
        try:
            from symnote.core.calendar_settings import load_calendar_settings

            cal_settings = load_calendar_settings()
            cal_connected = bool(cal_settings.enabled)
        except Exception:
            cal_connected = False

    invites_data: List[Dict[str, Any]] = []
    users_data: List[Dict[str, Any]] = []
    if user.is_owner:
        invites_data = [
            {
                "email": inv.email,
                "is_owner": inv.is_owner,
                "note": inv.note,
                "invited_at": inv.invited_at,
                "accepted_at": inv.accepted_at,
            }
            for inv in list_invites()
        ]
        users_data = [
            {
                "id": u.id,
                "email": u.email,
                "name": u.name,
                "is_owner": u.is_owner,
                "last_login_at": u.last_login_at,
            }
            for u in list_users()
        ]

    return {
        "user": _user_dict(user),
        "ai_usage": {
            "today_used": snap.today_scope,
            "daily_limit": snap.limits.daily_per_user,
            "remaining": max(0, snap.limits.daily_per_user - snap.today_scope),
            "per_minute_limit": snap.limits.per_minute,
        },
        "calendar_connected": cal_connected,
        "owner_features_enabled": user.is_owner,
        "invites": invites_data,
        "users": users_data,
    }


async def settings_invite_create_handler(request: Request, _user: UserRecord) -> Any:
    body: InviteCreateRequest = await _parse_body(request, InviteCreateRequest)
    inv = invite_user(body.email, note=body.note)
    return {
        "invite": {
            "email": inv.email,
            "is_owner": inv.is_owner,
            "note": inv.note,
            "invited_at": inv.invited_at,
            "accepted_at": inv.accepted_at,
        }
    }


def settings_invite_delete_handler(request: Request, _user: UserRecord) -> Any:
    email = unquote(str(request.path_params.get("email") or "")).strip()
    removed = revoke_invite(email)
    return {"ok": True, "removed": removed, "email": email}


def export_get_handler(_request: Request, user: UserRecord) -> Any:
    tasks = present_tasks(db.fetch_tasks(limit=2000))
    memos = [present_memo(m) for m in db.fetch_memos(limit=2000)]
    progress_list = goals.list_goals_with_progress(include_archived=True)
    g_titles = {g.id: g.title for g in progress_list}
    goals_data = [present_goal(g, goal_titles=g_titles) for g in progress_list]
    return {
        "exported_at": dt.datetime.now().isoformat(timespec="seconds"),
        "user": {"email": user.email, "name": user.name},
        "tasks": tasks,
        "memos": memos,
        "goals": goals_data,
    }


# --- Static SPA fallback ---


def _default_static_dir() -> Path:
    explicit = os.getenv("SYMNOTE_WEB_DIST", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (Path(__file__).resolve().parents[3] / "web" / "dist").resolve()


def create_app(*, static_dir: Optional[Path] = None) -> Starlette:
    """SymNote Web の Starlette アプリケーションを生成する。"""
    dist_dir = (static_dir or _default_static_dir()).resolve()

    async def spa_fallback(request: Request) -> Response:
        rel = request.path_params.get("full_path", "").lstrip("/")
        if rel.startswith("api/"):
            return _error("API エンドポイントが見つかりません。", status_code=404, code="not_found")
        if dist_dir.is_dir():
            if rel:
                candidate_file = (dist_dir / rel).resolve()
                if (
                    str(candidate_file).startswith(str(dist_dir))
                    and candidate_file.is_file()
                ):
                    return apply_security_headers(FileResponse(str(candidate_file)))
            index_html = dist_dir / "index.html"
            if index_html.is_file():
                return apply_security_headers(FileResponse(str(index_html)))
        return _json(
            {
                "name": "SymNote Web API",
                "status": "ok",
                "message": "Frontend build not found in web/dist. Run `npm --prefix web run build`.",
            }
        )

    allowed_hosts_raw = os.getenv("SYMNOTE_ALLOWED_HOSTS", "").strip()
    middleware: List[Middleware] = []
    if allowed_hosts_raw:
        hosts = [h.strip() for h in allowed_hosts_raw.split(",") if h.strip()]
        if hosts:
            middleware.append(Middleware(TrustedHostMiddleware, allowed_hosts=hosts))

    routes = [
        Route("/api/health", health_endpoint, methods=["GET"]),
        Route("/api/auth/me", auth_me_endpoint, methods=["GET"]),
        Route("/api/auth/login", auth_login_endpoint, methods=["POST"]),
        Route("/api/auth/e2e-login", auth_e2e_login_endpoint, methods=["POST"]),
        Route("/api/auth/logout", auth_logout_endpoint, methods=["POST"]),
        Route("/api/auth/google/start", auth_google_start_endpoint, methods=["GET"]),
        Route("/api/auth/google/callback", auth_google_callback_endpoint, methods=["GET"]),
        Route("/api/auth/onboarding", _with_auth(auth_onboarding_handler), methods=["POST"]),
        Route("/api/today", _with_auth(today_get_handler), methods=["GET"]),
        Route("/api/today/plan", _with_auth(today_plan_handler), methods=["POST"]),
        Route("/api/tasks", _with_auth(tasks_list_handler), methods=["GET"]),
        Route("/api/tasks", _with_auth(tasks_create_handler), methods=["POST"]),
        Route("/api/tasks/{task_id:int}", _with_auth(task_get_handler), methods=["GET"]),
        Route("/api/tasks/{task_id:int}", _with_auth(task_patch_handler), methods=["PATCH"]),
        Route("/api/tasks/{task_id:int}", _with_auth(task_delete_handler), methods=["DELETE"]),
        Route("/api/tasks/{task_id:int}/complete", _with_auth(task_complete_handler), methods=["POST"]),
        Route("/api/tasks/{task_id:int}/reopen", _with_auth(task_reopen_handler), methods=["POST"]),
        Route("/api/tasks/{task_id:int}/breakdown", _with_auth(task_breakdown_handler), methods=["POST"]),
        Route("/api/memos", _with_auth(memos_list_handler), methods=["GET"]),
        Route("/api/memos", _with_auth(memos_create_handler), methods=["POST"]),
        Route("/api/memos/{memo_id:int}", _with_auth(memo_patch_handler), methods=["PATCH"]),
        Route("/api/memos/{memo_id:int}", _with_auth(memo_delete_handler), methods=["DELETE"]),
        Route("/api/memos/{memo_id:int}/to-task", _with_auth(memo_to_task_handler), methods=["POST"]),
        Route("/api/capture/preview", _with_auth(capture_preview_handler), methods=["POST"]),
        Route("/api/capture", _with_auth(capture_create_handler), methods=["POST"]),
        Route("/api/capture/split", _with_auth(capture_split_handler), methods=["POST"]),
        Route("/api/capture/commit-split", _with_auth(capture_commit_split_handler), methods=["POST"]),
        Route("/api/inbox", _with_auth(inbox_get_handler), methods=["GET"]),
        Route("/api/candidates", _with_auth(candidates_list_handler), methods=["GET"]),
        Route("/api/candidates/{candidate_id:int}/approve", _with_auth(candidate_approve_handler), methods=["POST"]),
        Route("/api/candidates/{candidate_id:int}/reject", _with_auth(candidate_reject_handler), methods=["POST"]),
        Route("/api/candidates/{candidate_id:int}/restore", _with_auth(candidate_restore_handler), methods=["POST"]),
        Route("/api/unsorted", _with_auth(unsorted_list_handler), methods=["GET"]),
        Route("/api/unsorted/{item_id:int}/sort", _with_auth(unsorted_sort_handler), methods=["POST"]),
        Route("/api/unsorted/{item_id:int}", _with_auth(unsorted_delete_handler), methods=["DELETE"]),
        Route("/api/goals", _with_auth(goals_list_handler), methods=["GET"]),
        Route("/api/goals", _with_auth(goals_create_handler), methods=["POST"]),
        Route("/api/goals/{goal_id:int}", _with_auth(goal_get_handler), methods=["GET"]),
        Route("/api/goals/{goal_id:int}", _with_auth(goal_patch_handler), methods=["PATCH"]),
        Route("/api/goals/{goal_id:int}", _with_auth(goal_delete_handler), methods=["DELETE"]),
        Route("/api/goals/{goal_id:int}/breakdown", _with_auth(goal_breakdown_handler), methods=["POST"]),
        Route("/api/goals/{goal_id:int}/breakdown/commit", _with_auth(goal_breakdown_commit_handler), methods=["POST"]),
        Route("/api/ideas", _with_auth(ideas_get_handler), methods=["GET"]),
        Route("/api/ideas/sessions", _with_auth(ideas_create_session_handler), methods=["POST"]),
        Route("/api/ideas/sessions/{session_id:int}/nodes", _with_auth(ideas_add_node_handler), methods=["POST"]),
        Route("/api/ideas/nodes/{node_id:int}/to-task", _with_auth(ideas_node_to_task_handler), methods=["POST"]),
        Route("/api/settings", _with_auth(settings_get_handler), methods=["GET"]),
        Route("/api/settings/invites", _with_auth(settings_invite_create_handler, owner_only=True), methods=["POST"]),
        Route(
            "/api/settings/invites/{email:path}",
            _with_auth(settings_invite_delete_handler, owner_only=True),
            methods=["DELETE"],
        ),
        Route("/api/export", _with_auth(export_get_handler), methods=["GET"]),
        Route("/{full_path:path}", spa_fallback, methods=["GET"]),
    ]

    return Starlette(routes=routes, middleware=middleware)
