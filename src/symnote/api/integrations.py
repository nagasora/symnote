"""利用者の情報源を承認待ち候補へ取り込むWeb入口。"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from symnote.config import load_config
from symnote.core import chatgpt_import, task_extraction

MAX_TEXT = 12000
MAX_BODY_BYTES = 100000


class ConversationImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    consent: StrictBool


def status(_request: Request, _user: Any) -> dict[str, Any]:
    config = load_config()
    return {
        "ai": {
            "configured": bool(config.llm_api_key.strip()),
            "provider": config.llm_provider,
            "connection_verified": False,
        },
        "conversation": {"available": True, "mode": "selected_text", "max_characters": MAX_TEXT},
        "gmail": {"connected": False, "available": False, "reason": "利用者別のGmail接続は準備中です。Googleログインとは別の承認が必要です。"},
        "slack": {"connected": False, "available": False, "reason": "利用者別のSlack接続は準備中です。対象チャンネルの指定が必要です。"},
    }


async def import_conversation(request: Request, _user: Any) -> JSONResponse:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY_BYTES:
            return JSONResponse({"error": {"code": "too_large", "message": "会話テキストが大きすぎます。"}}, status_code=413)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("会話テキストをJSONで送信してください。") from None
    body = ConversationImport.model_validate(payload)
    if not body.consent:
        raise ValueError("選択した会話をAIへ送信することに同意してください。")
    item = chatgpt_import.parse_pasted_text(body.text)
    if not load_config().llm_api_key.strip():
        return JSONResponse({"error": {"code": "ai_unavailable", "message": "提案機能の接続設定がありません。管理者に確認してください。"}}, status_code=503)
    # 同期の外部通信で他利用者のAPIを止めない。threadpoolは利用者のContextVarを引き継ぐ。
    result = await run_in_threadpool(task_extraction.extract_task_candidates, "chat", [item])
    data = {
        "created_ids": result.created_ids,
        "created_count": result.created_count,
        "duplicate_count": result.duplicate_count,
        "discarded_count": result.discarded_count,
        "partial": bool(result.errors and result.processed_refs),
        "message": result.message if not result.errors else "候補作成を完了できませんでした。利用枠と接続状態を確認して再試行してください。",
    }
    if result.errors:
        return JSONResponse({**data, "error": {"code": "extraction_failed", "message": data["message"]}}, status_code=502)
    return JSONResponse(data)


def routes(with_auth: Any) -> list[Route]:
    return [
        Route("/api/integrations", with_auth(status), methods=["GET"]),
        Route("/api/integrations/conversation", with_auth(import_conversation), methods=["POST"]),
    ]
