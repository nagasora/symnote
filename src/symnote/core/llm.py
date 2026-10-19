"""Gemini の通信・再試行・JSON 解析を集約する小さな AI 呼び出し層。"""

from __future__ import annotations

from contextlib import suppress
import json
import math
import time
from typing import Any, Callable, Dict

from symnote.config import load_config
from symnote.core import usage_limit

FALLBACK_MODEL = "gemini-flash-latest"
MAX_RETRIES = 2


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


def _failure_message(status: int | None) -> str:
    """秘密を含み得る SDK 例外本文を表示せず、日本語の復旧案を返す。"""
    if status in {400, 401, 403}:
        return "Gemini の API キーまたは設定が無効です。LLM_API_KEY とモデルの利用権限を確認してください。"
    if status == 429:
        return "Gemini の利用上限に達しています。少し待つか API の割り当てを確認してください。"
    if status is not None and 500 <= status <= 599:
        return "Gemini 側で障害が発生しています。時間をおいて再度お試しください。"
    if status == 404:
        return "Gemini の指定モデルと代替モデルが利用できません。LLM_MODEL を確認してください。"
    return "Gemini に接続できませんでした。通信状況とタイムアウト設定を確認してください。"


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
        raise LLMResponseError("AI から応答を取得できませんでした。入力内容を確認して再度お試しください。")
    return text.strip()


def _generate(
    prompt: str,
    *,
    timeout: float,
    json_output: bool,
    schema: Dict[str, Any] | None = None,
    client_factory: Callable[[str, int], Any] | None = None,
) -> str:
    """全試行の時間予算内で最大二回再試行し、モデル欠落時は一度だけ代替する。"""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("AI に渡す入力が空です。")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("タイムアウトは正の有限秒数を指定してください。")
    config = load_config()
    if not config.llm_api_key.strip():
        raise LLMUnavailableError(
            "LLM_API_KEY が設定されていません。Gemini の API キーを設定してください。手動編集は利用できます。"
        )
    try:
        usage_limit.check_prompt_size(prompt)
    except usage_limit.UsageLimitExceeded as exc:
        raise LLMUnavailableError(str(exc)) from None
    deadline = time.monotonic() + timeout
    factory = client_factory or _create_client
    client: Any = None
    model = config.llm_model
    retry_count = 0
    used_fallback = model == FALLBACK_MODEL
    try:
        client = factory(config.llm_api_key, max(1, int(timeout * 1000)))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LLMUnavailableError("Gemini の応答がタイムアウトしました。時間をおいて再度お試しください。")
            generation_config: Dict[str, Any] = {
                "max_output_tokens": config.max_tokens,
                "http_options": {"timeout": max(1, int(remaining * 1000))},
            }
            if json_output:
                generation_config["response_mime_type"] = "application/json"
                if schema is not None:
                    generation_config["response_json_schema"] = schema
            try:
                # 再試行・代替モデルも課金対象の 1 回として数える。
                usage_limit.acquire()
            except usage_limit.UsageLimitExceeded as exc:
                raise LLMUnavailableError(str(exc)) from None
            try:
                response = client.models.generate_content(
                    model=model, contents=prompt, config=generation_config
                )
                return _response_text(response)
            except LLMResponseError:
                raise
            except Exception as exc:
                status = _error_status(exc)
                if not used_fallback and _is_missing_model(exc, status):
                    model = FALLBACK_MODEL
                    used_fallback = True
                    continue
                if (status == 429 or (status is not None and 500 <= status <= 599)) and retry_count < MAX_RETRIES:
                    delay = 0.25 * (2**retry_count)
                    retry_count += 1
                    if time.monotonic() + delay >= deadline:
                        raise LLMUnavailableError("Gemini の応答がタイムアウトしました。再度お試しください。") from None
                    time.sleep(delay)
                    continue
                raise LLMUnavailableError(_failure_message(status)) from None
    except (LLMUnavailableError, LLMResponseError):
        raise
    except Exception:
        raise LLMUnavailableError("Gemini クライアントを準備できませんでした。API キーと設定を確認してください。") from None
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
        raise LLMResponseError("AI から有効な JSON 応答を取得できませんでした。再度お試しください。") from None


def generate_json(
    prompt: str,
    schema: Dict[str, Any] | None = None,
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
    client_factory: Callable[[str, int], Any] | None = None,
) -> str:
    """設定済みモデルからテキストを生成し、通信失敗は日本語の例外で伝える。"""
    return _generate(prompt, timeout=timeout, json_output=False, client_factory=client_factory)
