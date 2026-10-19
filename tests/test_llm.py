"""ネットワークを使わず AI 呼び出しの再試行と失敗表示を保証する。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from symnote.core import llm


class FakeAPIError(Exception):
    """数値ステータスを持つ API 例外を模倣する。"""

    def __init__(self, code: int, message: str = "秘密の API キー") -> None:
        """試行条件に対応するステータスを設定する。"""
        super().__init__(message)
        self.code = code


class FakeClient:
    """順に応答・例外を返して通信引数を記録する。"""

    def __init__(self, outcomes: List[Any]) -> None:
        """応答列と呼び出し履歴を初期化する。"""
        self.outcomes = list(outcomes)
        self.calls: List[Dict[str, Any]] = []
        self.models = self
        self.closed = False

    def generate_content(self, **kwargs: Any) -> Any:
        """次の応答を返すか、指定された例外を送出する。"""
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome)

    def close(self) -> None:
        """リソース解放を記録する。"""
        self.closed = True


def configure(monkeypatch: pytest.MonkeyPatch) -> None:
    """実キーを使わず設定と待ち時間を固定する。"""
    monkeypatch.setenv("LLM_API_KEY", "fake-key")
    monkeypatch.setenv("LLM_MODEL", "custom-model")
    monkeypatch.setenv("MAX_TOKENS", "1234")
    monkeypatch.setattr(llm.time, "sleep", lambda _: None)


def test_json_uses_config_schema_hint_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON モード・トークン数・スキーマ・タイムアウトを SDK へ渡す。"""
    configure(monkeypatch)
    client = FakeClient(['{"ok": true}'])
    factory_args: List[Any] = []

    def factory(key: str, timeout: int) -> FakeClient:
        """クライアントの生成引数を記録する。"""
        factory_args.extend([key, timeout])
        return client

    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    assert llm.generate_json("入力", schema, hint="補足", timeout=12, client_factory=factory) == {"ok": True}
    assert factory_args == ["fake-key", 12000]
    call = client.calls[0]
    assert call["model"] == "custom-model"
    assert "補足" in call["contents"]
    assert call["config"]["response_mime_type"] == "application/json"
    assert call["config"]["response_json_schema"] == schema
    assert call["config"]["max_output_tokens"] == 1234
    assert 0 < call["config"]["http_options"]["timeout"] <= 12000
    assert client.closed


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_errors_retry_at_most_twice(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """一時障害は最大二回の再試行で復旧できる。"""
    configure(monkeypatch)
    client = FakeClient([FakeAPIError(status), FakeAPIError(status), "成功"])
    assert llm.generate_text("入力", client_factory=lambda *_: client) == "成功"
    assert len(client.calls) == 3
    assert "response_mime_type" not in client.calls[-1]["config"]
    assert client.closed


def test_retry_exhaustion_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    """障害が続く場合は三試行で終了し、例外本文のキーを漏らさない。"""
    configure(monkeypatch)
    client = FakeClient([FakeAPIError(503)] * 4)
    with pytest.raises(llm.LLMUnavailableError, match="障害") as error:
        llm.generate_text("入力", client_factory=lambda *_: client)
    assert "秘密" not in str(error.value)
    assert len(client.calls) == 3
    assert client.closed


@pytest.mark.parametrize("status,message", [(404, "not found"), (400, "model custom-model not found")])
def test_missing_model_falls_back_once(monkeypatch: pytest.MonkeyPatch, status: int, message: str) -> None:
    """モデル欠落は flash-latest に一度だけ切り替える。"""
    configure(monkeypatch)
    client = FakeClient([FakeAPIError(status, message), "代替応答"])
    assert llm.generate_text("入力", client_factory=lambda *_: client) == "代替応答"
    assert [call["model"] for call in client.calls] == ["custom-model", "gemini-flash-latest"]


def test_fallback_model_not_found_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """代替モデルも欠落している場合は無限ループしない。"""
    configure(monkeypatch)
    client = FakeClient([FakeAPIError(404), FakeAPIError(404)])
    with pytest.raises(llm.LLMUnavailableError, match="代替モデル"):
        llm.generate_text("入力", client_factory=lambda *_: client)
    assert len(client.calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403])
def test_permanent_errors_do_not_retry(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """認証・入力の恒久障害は再試行しない。"""
    configure(monkeypatch)
    client = FakeClient([FakeAPIError(status)])
    with pytest.raises(llm.LLMUnavailableError, match="API キー"):
        llm.generate_json("入力", client_factory=lambda *_: client)
    assert len(client.calls) == 1


def test_missing_key_fails_before_client_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    """キー未設定なら通信クライアントを作らない。"""
    monkeypatch.setenv("LLM_API_KEY", " ")

    def forbidden_factory(*args: Any) -> Any:
        """通信の開始を禁止する。"""
        pytest.fail("クライアントが生成されました")

    with pytest.raises(llm.LLMUnavailableError, match="LLM_API_KEY"):
        llm.generate_text("入力", client_factory=forbidden_factory)


@pytest.mark.parametrize("text", ['{"a":1}', '```json\n{"a":1}\n```'])
def test_json_parser_accepts_json_and_fences(text: str) -> None:
    """有効な JSON と単一コードブロックを解析する。"""
    assert llm.parse_json(text) == {"a": 1}


@pytest.mark.parametrize("text", ["", "invalid", "```json\n{}", "```json\n\n```"])
def test_json_parser_rejects_invalid_response(text: str) -> None:
    """不正または空の JSON を例外で通知する。"""
    with pytest.raises(llm.LLMResponseError):
        llm.parse_json(text)


def test_blocked_and_truncated_responses_are_rejected() -> None:
    """有効なテキストがあっても打ち切られた応答は採用しない。"""
    response = SimpleNamespace(text='{"ok":true}', candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")])
    with pytest.raises(llm.LLMResponseError, match="途中で停止"):
        llm._response_text(response)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_timeout_must_be_positive_and_finite(timeout: float) -> None:
    """不正な時間予算は通信前に拒否する。"""
    with pytest.raises(ValueError, match="タイムアウト"):
        llm.generate_text("入力", timeout=timeout)


def test_time_budget_exhaustion_stops_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """クライアント準備で時間予算を使い切った場合も通信しない。"""
    configure(monkeypatch)
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(llm.time, "monotonic", lambda: next(ticks))
    client = FakeClient([])
    with pytest.raises(llm.LLMUnavailableError, match="タイムアウト"):
        llm.generate_text("入力", timeout=1, client_factory=lambda *_: client)
    assert not client.calls
    assert client.closed
