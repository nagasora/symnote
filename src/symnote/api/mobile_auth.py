"""Native iOS login endpoints layered over the existing browser Google OAuth flow."""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs, urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from symnote.api.auth import (
    OAUTH_STATE_COOKIE_NAME,
    OAUTH_STATE_TTL_SECONDS,
    apply_security_headers,
    build_google_authorization_url,
    create_session,
    is_https_request,
    verify_csrf,
)
from symnote.core.accounts import SESSION_TTL_SECONDS, InviteRequiredError
from symnote.core.mobile_auth import (
    exchange_mobile_code,
    register_mobile_flow,
    valid_app_state,
    valid_code_challenge,
)

logger = logging.getLogger(__name__)
_GENERIC_EXCHANGE_MESSAGE = "認証情報を確認できません。もう一度ログインしてください。"


def no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return apply_security_headers(response)


def _error(message: str, status_code: int, code: str) -> JSONResponse:
    return no_store(
        JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)
    )


async def mobile_start_endpoint(request: Request) -> Response:
    challenge = request.query_params.get("challenge", "").strip()
    app_state = request.query_params.get("app_state", "").strip()
    if not valid_code_challenge(challenge) or not valid_app_state(app_state):
        return _error("モバイルログインを開始できません。", 400, "invalid_mobile_request")
    try:
        url = build_google_authorization_url(request)
        oauth_state = parse_qs(urlparse(url).query)["state"][0]
        register_mobile_flow(oauth_state, challenge, app_state)
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(
            OAUTH_STATE_COOKIE_NAME,
            oauth_state,
            max_age=OAUTH_STATE_TTL_SECONDS,
            httponly=True,
            secure=is_https_request(request),
            samesite="lax",
            path="/api/auth/google",
        )
        return no_store(response)
    except Exception as exc:
        logger.warning("Mobile Google login start failed: type=%s", type(exc).__name__)
        return _error(
            "モバイルログインを開始できません。時間をおいて再試行してください。",
            503,
            "oauth_unavailable",
        )


async def mobile_exchange_endpoint(request: Request) -> Response:
    csrf_error = verify_csrf(request)
    if csrf_error or request.headers.get("origin", "").strip():
        return _error(_GENERIC_EXCHANGE_MESSAGE, 403, "invalid_exchange")
    try:
        raw = await request.body()
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(payload, dict):
            raise ValueError("invalid payload")
        code = payload.get("code")
        verifier = payload.get("verifier")
        if not isinstance(code, str) or not isinstance(verifier, str):
            raise ValueError("invalid payload")
        user_id = exchange_mobile_code(code.strip(), verifier.strip())
        if user_id is None:
            return _error(_GENERIC_EXCHANGE_MESSAGE, 401, "invalid_exchange")
        session_token = create_session(user_id)
        return no_store(
            JSONResponse(
                {
                    "session_token": session_token,
                    "token_type": "Bearer",
                    "expires_in": SESSION_TTL_SECONDS,
                }
            )
        )
    except InviteRequiredError:
        return _error(_GENERIC_EXCHANGE_MESSAGE, 401, "invalid_exchange")
    except Exception as exc:
        logger.warning("Mobile code exchange failed: type=%s", type(exc).__name__)
        return _error(_GENERIC_EXCHANGE_MESSAGE, 400, "invalid_exchange")


__all__ = ["mobile_exchange_endpoint", "mobile_start_endpoint", "no_store"]
