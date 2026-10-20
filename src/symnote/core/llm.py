"""Gemini / OpenAI の通信・再試行・JSON 解析を集約する AI 呼び出し層。"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any
from urllib.request import Request, urlopen

from symnote.config import load_config
from symnote.core import usage_limit

GEMINI_FALLBACK_MODEL = "gemini-flash-latest"
OPENAI_FALLBACK_MODEL = "gpt-6-luna"
# 既存コード・テスト向けの名前を維持する。
FALLBACK_MODEL = GEMINI_FALLBACK_MODEL
MAX_RETRIES = 2
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


class LLMUnavailableError(RuntimeError):
    """キー・通信・サービスの問題により AI を利用できないことを表す。"""


class LLMResponseError(ValueError):
    """AI の応答が空、打ち切り、または要求した JSON 形式でないことを表す。"""


def _create_client(api_key: str, timeout_ms: int) -> Any:
    """公式 SDK クライアントを遅延生成し、SDK 自体の多重再試行を無効にする。"""
    from google import genai

    return genai.Client(
        api_key=api_key,
        http_options={
            "timeout": timeout_ms,
            "retry_options": {"attempts": 1},
        },
    )


class _OpenAIClient:
    """追加依存なしで Responses API を呼ぶ最小クライアント。"""

    def __init__(self, api_key: str, timeout_ms: int) -> None:
        self._api_key = api_key
        self._timeout = max(0.001, timeout_ms / 1000)
        self.responses = self

    def create(self, **kwargs: Any) -> dict[str, Any]:
        """Responses API の JSON 応答を辞書で返す。"""
        timeout = float(kwargs.pop("_timeout", self._timeout))
        request = Request(
            OPENAI_RESPONSES_URL,
            data=json.dumps(kwargs).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=max(0.001, timeout)) as response:
            return json.loads(response.read().decode("utf-8"))

    def close(self) -> None:
        """urllib は応答単位で接続を閉じるため、追加処理は不要。"""


def _create_openai_client(api_key: str, timeout_ms: int) -> _OpenAIClient:
    """OpenAI Responses API クライアントを生成する。"""
    return _OpenAIClient(api_key, timeout_ms)


def _error_status(error: Exception) -> int | None:
    """SDK と HTTP クライアントの例外から数値ステータスを取り出す。"""
    for value in (
        getattr(error, "code", None),
        getattr(error, "status_code", None),
        getattr(getattr(error, "response", None), "status_code", None),
    ):
        try:
            return int(value)
        except (ValueError, TypeError):
            continue
    return None


def _is_missing_model(error: Exception, status: int | None) -> bool:
    """指定モデルが見つからない失敗だけをフォールバック対象にする。"""
    message = str(error).casefold()
    return status == 404 or (
        status in {None, 400}
        and "model" in message
        and ("not found" in message or "not_found" in message)
    )


def _failure_message(provider: str, status: int | None) -> str:
    """秘密を含み得る SDK 例外本文を表示せず、日本語の復旧案を返す。"""
    service = "OpenAI" if provider == "openai" else "Gemini"
    key_name = "OPENAI_API_KEY" if provider == "openai" else "LLM_API_KEY"
    model_name = "OPENAI_MODEL" if provider == "openai" else "LLM_MODEL"
    if status in {400, 401, 403}:
        return (
            f"{service} の API キーまたは設定が無効です。"
            f"{key_name} とモデルの利用権限を確認してください。"
        )
    if status == 429:
        return f"{service} の利用上限に達しています。少し待つか API の割り当てを確認してください。"
    if status is not None and 500 <= status <= 599:
        return f"{service} 側で障害が発生しています。時間をおいて再度お試しください。"
    if status == 404:
        return (
            f"{service} の指定モデルと代替モデルが利用できません。{model_name} を確認してください。"
        )
    return f"{service} に接続できませんでした。通信状況とタイムアウト設定を確認してください。"


def _response_text(response: Any) -> str:
    """安全性ブロックやトークン上限による不完全な応答を拒否する。"""
    for candidate in getattr(response, "candidates", None) or []:
        reason = getattr(candidate, "finish_reason", None)
        reason_name = getattr(reason, "name", str(reason or ""))
        if reason_name and reason_name not in {"STOP", "FINISH_REASON_UNSPECIFIED"}:
            raise LLMResponseError(
                "AI の応答が途中で停止しました。入力を短くするか MAX_TOKENS を増やしてください。"
            )
    text = getattr(response, "text", None)
    if not isinstance(text, str) or not text.strip():
        raise LLMResponseError(
            "AI から応答を取得できませんでした。入力内容を確認して再度お試しください。"
        )
    return text.strip()


def _openai_response_text(response: Any) -> str:
    """Responses API の完了済みテキストだけを取り出す。"""
    status = (
        response.get("status") if isinstance(response, dict) else getattr(response, "status", None)
    )
    if status in {"failed", "cancelled", "incomplete"}:
        raise LLMResponseError(
            "AI の応答が途中で停止しました。入力を短くするか MAX_TOKENS を増やしてください。"
        )
    text = (
        response.get("output_text")
        if isinstance(response, dict)
        else getattr(response, "output_text", None)
    )
    if isinstance(text, str) and text.strip():
        return text.strip()
    output = (
        response.get("output", [])
        if isinstance(response, dict)
        else getattr(response, "output", [])
    )
    parts: list[str] = []
    for item in output or []:
        content = (
            item.get("content", []) if isinstance(item, dict) else getattr(item, "content", [])
        )
        for part in content or []:
            part_type = part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
            if part_type == "refusal":
                raise LLMResponseError(
                    "AI がこの入力への応答を生成できませんでした。入力内容を確認してください。"
                )
            if part_type == "output_text":
                value = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
                if isinstance(value, str):
                    parts.append(value)
    joined = "".join(parts).strip()
    if not joined:
        raise LLMResponseError(
            "AI から応答を取得できませんでした。入力内容を確認して再度お試しください。"
        )
    return joined


def _generate(
    prompt: str,
    *,
    timeout: float,
    json_output: bool,
    schema: dict[str, Any] | None = None,
    temperature: float | None = None,
    client_factory: Callable[[str, int], Any] | None = None,
) -> str:
    """全試行の時間予算内で最大二回再試行し、モデル欠落時は一度だけ代替する。"""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("AI に渡す入力が空です。")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("タイムアウトは正の有限秒数を指定してください。")
    config = load_config()
    provider = config.llm_provider
    if provider not in {"gemini", "openai"}:
        raise LLMUnavailableError(
            "LLM_PROVIDER は gemini または openai を指定してください。手動編集は利用できます。"
        )
    if not config.llm_api_key.strip():
        key_name = "OPENAI_API_KEY" if provider == "openai" else "LLM_API_KEY"
        service = "OpenAI" if provider == "openai" else "Gemini"
        raise LLMUnavailableError(
            f"{key_name} が設定されていません。{service} の API キーを設定してください。"
            "手動編集は利用できます。"
        )
    try:
        usage_limit.check_prompt_size(prompt)
    except usage_limit.UsageLimitExceeded as exc:
        raise LLMUnavailableError(str(exc)) from None
    deadline = time.monotonic() + timeout
    factory = client_factory or (_create_openai_client if provider == "openai" else _create_client)
    client: Any = None
    model = config.llm_model
    retry_count = 0
    fallback_model = OPENAI_FALLBACK_MODEL if provider == "openai" else GEMINI_FALLBACK_MODEL
    used_fallback = model == fallback_model
    try:
        client = factory(config.llm_api_key, max(1, int(timeout * 1000)))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LLMUnavailableError(
                    "AI の応答がタイムアウトしました。時間をおいて再度お試しください。"
                )
            try:
                # 再試行・代替モデルも課金対象の 1 回として数える。
                usage_limit.acquire()
            except usage_limit.UsageLimitExceeded as exc:
                raise LLMUnavailableError(str(exc)) from None
            try:
                if provider == "openai":
                    request: dict[str, Any] = {
                        "model": model,
                        "input": prompt,
                        "max_output_tokens": config.max_tokens,
                        "store": False,
                        "_timeout": remaining,
                    }
                    if json_output:
                        response_format: dict[str, Any] = {"type": "json_object"}
                        if schema is not None:
                            response_format = {
                                "type": "json_schema",
                                "name": "symnote_response",
                                "schema": schema,
                            }
                        request["text"] = {"format": response_format}
                    response = client.responses.create(**request)
                    return _openai_response_text(response)
                generation_config: dict[str, Any] = {
                    "max_output_tokens": config.max_tokens,
                    "http_options": {"timeout": max(1, int(remaining * 1000))},
                }
                if temperature is not None:
                    generation_config["temperature"] = temperature
                if json_output:
                    generation_config["response_mime_type"] = "application/json"
                    if schema is not None:
                        generation_config["response_json_schema"] = schema
                response = client.models.generate_content(
                    model=model, contents=prompt, config=generation_config
                )
                return _response_text(response)
            except LLMResponseError:
                raise
            except Exception as exc:
                status = _error_status(exc)
                if not used_fallback and _is_missing_model(exc, status):
                    model = fallback_model
                    used_fallback = True
                    continue
                transient = status == 429 or (status is not None and 500 <= status <= 599)
                if transient and retry_count < MAX_RETRIES:
                    delay = 0.25 * (2**retry_count)
                    retry_count += 1
                    if time.monotonic() + delay >= deadline:
                        raise LLMUnavailableError(
                            "AI の応答がタイムアウトしました。再度お試しください。"
                        ) from None
                    time.sleep(delay)
                    continue
                raise LLMUnavailableError(_failure_message(provider, status)) from None
    except (LLMUnavailableError, LLMResponseError):
        raise
    except Exception:
        service = "OpenAI" if provider == "openai" else "Gemini"
        raise LLMUnavailableError(
            f"{service} クライアントを準備できませんでした。API キーと設定を確認してください。"
        ) from None
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()


def parse_json(content: str) -> Any:
    """プレーン JSON または単一の Markdown コードブロックを解析する。"""
    if not isinstance(content, str) or not content.strip():
        raise LLMResponseError("AI から有効な JSON 応答を取得できませんでした。")
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            raise LLMResponseError("AI 応答の JSON コードブロックが閉じられていません。")
        text = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        raise LLMResponseError(
            "AI から有効な JSON 応答を取得できませんでした。再度お試しください。"
        ) from None


def generate_json(
    prompt: str,
    schema: dict[str, Any] | None = None,
    *,
    hint: str = "",
    timeout: float = 45,
    client_factory: Callable[[str, int], Any] | None = None,
) -> Any:
    """JSON 出力を要求して解析する。用途固有の検証は呼び出し側が行う。"""
    request = prompt if not hint else f"{prompt}\n\n出力形式の補足:\n{hint}"
    return parse_json(
        _generate(
            request,
            timeout=timeout,
            json_output=True,
            schema=schema,
            client_factory=client_factory,
        )
    )


def generate_text(
    prompt: str,
    *,
    timeout: float = 45,
    temperature: float | None = None,
    client_factory: Callable[[str, int], Any] | None = None,
) -> str:
    """設定済みモデルからテキストを生成し、通信失敗は日本語の例外で伝える。"""
    return _generate(
        prompt,
        timeout=timeout,
        json_output=False,
        temperature=temperature,
        client_factory=client_factory,
    )
