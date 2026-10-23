"""利用者の情報源を承認待ち候補へ取り込むWeb入口。"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from symnote.api.auth import SESSION_COOKIE_NAME, request_session_token
from symnote.config import load_config
from symnote.core import chatgpt_import, llm, task_extraction
from symnote.core import source_connections as store
from symnote.core import source_providers as providers

MAX_TEXT = 12000
MAX_BODY_BYTES = 100000


class ConversationImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    consent: StrictBool


def status(_request: Request, _user: Any):
    config = load_config()
    return no_store(
        JSONResponse(
            {
                "ai": {
                    "configured": bool(config.llm_api_key.strip()),
                    "provider": config.llm_provider,
                    "connection_verified": False,
                },
                "conversation": {
                    "available": True,
                    "mode": "selected_text",
                    "max_characters": MAX_TEXT,
                },
                "gmail": connection_status("gmail"),
                "slack": connection_status("slack"),
            }
        )
    )


async def import_conversation(request: Request, _user: Any) -> JSONResponse:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY_BYTES:
            return JSONResponse(
                {"error": {"code": "too_large", "message": "会話テキストが大きすぎます。"}},
                status_code=413,
            )
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("会話テキストをJSONで送信してください。") from None
    body = ConversationImport.model_validate(payload)
    if not body.consent:
        raise ValueError("選択した会話をAIへ送信することに同意してください。")
    item = chatgpt_import.parse_pasted_text(body.text)
    if not load_config().llm_api_key.strip():
        return JSONResponse(
            {
                "error": {
                    "code": "ai_unavailable",
                    "message": "提案機能の接続設定がありません。管理者に確認してください。",
                }
            },
            status_code=503,
        )
    # 同期の外部通信で他利用者のAPIを止めない。threadpoolは利用者のContextVarを引き継ぐ。
    result = await run_in_threadpool(task_extraction.extract_task_candidates, "chat", [item])
    data = {
        "created_ids": result.created_ids,
        "created_count": result.created_count,
        "duplicate_count": result.duplicate_count,
        "discarded_count": result.discarded_count,
        "partial": bool(result.errors and result.processed_refs),
        "message": result.message
        if not result.errors
        else "候補作成を完了できませんでした。利用枠と接続状態を確認して再試行してください。",
    }
    if result.errors:
        return JSONResponse(
            {**data, "error": {"code": "extraction_failed", "message": data["message"]}},
            status_code=502,
        )
    return JSONResponse(data)


def connection_status(provider: str) -> dict:
    row = store.load(provider)
    return {
        "available": providers.ready(provider),
        "connected": row is not None,
        "account": row["account"] if row else "",
        "workspace": row["workspace"] if row else "",
        "channels": json.loads(row["channels"]) if row else [],
        "verified_at": row["verified_at"] if row else None,
        "last_fetch_at": row["last_fetch_at"] if row else None,
        "last_success_at": row["last_success_at"] if row else None,
        "last_error": row["last_error"] if row else "",
        "reason": ""
        if providers.ready(provider)
        else "管理者によるOAuth設定・固定HTTPS URL・暗号鍵の設定が必要です。",
    }


def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def start(request: Request, _user):
    provider = store.provider_name(request.path_params["provider"])
    if not providers.ready(provider):
        raise ValueError("情報源の接続設定が完了していません。管理者に確認してください。")
    if not request.cookies.get(SESSION_COOKIE_NAME):
        raise PermissionError("Safariなどのブラウザでログインしてから情報源を接続してください。")
    verifier = secrets.token_urlsafe(48)
    redirect = providers.callback_url(provider)
    state = store.save_state(
        provider, request_session_token(request), {"redirect_uri": redirect, "verifier": verifier}
    )
    client_id, _ = providers.provider_config(provider)
    query = {"client_id": client_id, "redirect_uri": redirect, "state": state}
    if provider == "gmail":
        query.update(
            response_type="code",
            scope=providers.GMAIL_SCOPE,
            access_type="offline",
            prompt="consent",
            code_challenge_method="S256",
            code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("="),
        )
        url = "https://accounts.google.com/o/oauth2/v2/auth"
    else:
        query["user_scope"] = ",".join(sorted(providers.SLACK_SCOPES))
        url = "https://slack.com/oauth/v2/authorize"
    return no_store(JSONResponse({"authorization_url": url + "?" + urlencode(query)}))


async def callback(request: Request, _user):
    provider = store.provider_name(request.path_params["provider"])
    payload = store.consume_state(
        provider, request_session_token(request), request.query_params.get("state", "")
    )
    if request.query_params.get("error"):
        return no_store(RedirectResponse("/me/settings?integration=cancelled", status_code=303))
    code = request.query_params.get("code", "")
    if not code or len(code) > 4096 or payload["redirect_uri"] != providers.callback_url(provider):
        raise ValueError("接続の応答を確認できません。設定からやり直してください。")
    try:
        credentials, account, workspace = await run_in_threadpool(
            providers.exchange, provider, code, payload
        )
        store.save_connection(
            provider, credentials, account, workspace, state_hash=payload["state_hash"]
        )
    except providers.ProviderError:
        return no_store(RedirectResponse("/me/settings?integration=failed", status_code=303))
    return no_store(RedirectResponse("/me/settings?integration=connected", status_code=303))


async def read_payload(request: Request) -> dict:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 10000:
            raise ValueError("リクエストが大きすぎます。")
    try:
        value = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("JSONで送信してください。") from None
    if not isinstance(value, dict):
        raise ValueError("JSONオブジェクトで送信してください。")
    return value


class ChannelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channels: list[str] = Field(max_length=5)


class ImportApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consent: StrictBool


def operate(provider: str, action):
    row, run = store.acquire(provider)
    try:
        with providers.authorized_run(lambda: store.renew(provider, row, run)):
            token = providers.access(provider, row, run)
            result = action(row, run, token)
        store.finish(provider, row, run)
        return result
    except (ValueError, PermissionError):
        store.finish(provider, row, run, "情報源の権限・接続または選択範囲を確認してください。")
        raise
    except Exception:
        store.finish(provider, row, run, "情報源の処理を完了できませんでした。")
        raise ValueError(
            "情報源の処理を完了できませんでした。時間をおいて再試行してください。"
        ) from None


async def list_channels(request: Request, _user):
    return await run_in_threadpool(
        operate,
        "slack",
        lambda _row, _run, token: providers.channels(token, request.query_params.get("cursor", "")),
    )


async def select_channels(request: Request, _user):
    body = ChannelSelection.model_validate(await read_payload(request))
    selected = list(dict.fromkeys(body.channels))

    def action(row, run, token):
        for channel in selected:
            providers.public_channel(token, channel)
        store.set_channels(row, run, selected)
        return {"channels": selected}

    return await run_in_threadpool(operate, "slack", action)


def import_source(provider: str):
    row, run = store.acquire(provider)
    try:
        with providers.authorized_run(lambda: store.renew(provider, row, run)):
            token = providers.access(provider, row, run)
            known = store.processed(provider)
            fetched = (
                providers.gmail_messages(
                    token, row["account"], known, json.loads(row["checkpoint"])
                )
                if provider == "gmail"
                else providers.slack_messages(token, row, known)
            )
            store.fetched(provider, row, run)

            def generate(prompt):
                store.renew(provider, row, run)
                return llm.generate_json(prompt)

            result = task_extraction.extract_task_candidates(
                provider,
                fetched.items,
                generate,
                save_batch=lambda proposals, refs: store.save_batch(
                    provider, row, run, proposals, refs
                ),
            )
        failed = bool(result.errors)
        if not failed and fetched.checkpoint is not None:
            store.save_checkpoint(provider, row, run, fetched.checkpoint)
        message = (
            "候補作成を完了できませんでした。利用枠と接続を確認して再試行してください。"
            if failed
            else result.message
        )
        if fetched.limited:
            message += (
                " 取得上限または続きのページがあるため一部のみ確認しました。"
                "時間をおいて再実行してください。"
            )
        store.finish(
            provider,
            row,
            run,
            message if failed or fetched.limited else "",
            success=not failed and not fetched.limited,
        )
        body = {
            "created_count": result.created_count,
            "duplicate_count": result.duplicate_count,
            "fetched_count": len(fetched.items),
            "limited": fetched.limited,
            "partial": failed or fetched.limited,
            "message": message,
        }
        if failed:
            body["error"] = {"code": "extraction_failed", "message": message}
        return body, 502 if failed else 200
    except (ValueError, PermissionError):
        store.finish(provider, row, run, "情報源の接続・権限またはAI利用枠を確認してください。")
        raise
    except Exception:
        store.finish(provider, row, run, "情報源の取り込みを完了できませんでした。")
        raise ValueError(
            "情報源の取り込みを完了できませんでした。時間をおいて再試行してください。"
        ) from None


async def import_remote(request: Request, _user):
    provider = store.provider_name(request.path_params["provider"])
    body = ImportApproval.model_validate(await read_payload(request))
    if not body.consent:
        raise ValueError("指定した情報源をAIへ送信することに同意してください。")
    if not load_config().llm_api_key.strip():
        raise ValueError("提案機能の接続設定がありません。管理者に確認してください。")
    result, code = await run_in_threadpool(import_source, provider)
    return no_store(JSONResponse(result, status_code=code))


def remove(request: Request, _user):
    store.disconnect(store.provider_name(request.path_params["provider"]))
    return {
        "disconnected": True,
        "message": (
            "SymNote内の接続を削除しました。"
            "外部サービス側の許可は各サービスの管理画面で取り消せます。"
        ),
    }


def routes(with_auth: Any) -> list[Route]:
    return [
        Route("/api/integrations", with_auth(status), methods=["GET"]),
        Route("/api/integrations/conversation", with_auth(import_conversation), methods=["POST"]),
        Route("/api/integrations/slack/channels", with_auth(list_channels), methods=["GET"]),
        Route("/api/integrations/slack/channels", with_auth(select_channels), methods=["PUT"]),
        Route("/api/integrations/{provider}/start", with_auth(start), methods=["POST"]),
        Route("/api/integrations/{provider}/callback", with_auth(callback), methods=["GET"]),
        Route("/api/integrations/{provider}/import", with_auth(import_remote), methods=["POST"]),
        Route("/api/integrations/{provider}", with_auth(remove), methods=["DELETE"]),
    ]
