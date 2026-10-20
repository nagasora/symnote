"""AI 呼び出し上限（1 分・1 日・全体・入力長）が通信前に効くことを保証する。"""

from __future__ import annotations

import datetime as dt
from typing import Any, List

import pytest

from symnote.core import llm, usage_limit
from symnote.core.usage_limit import UsageLimitExceeded, UsageLimits, acquire, usage_snapshot

NOW = dt.datetime(2026, 10, 16, 12, 0).timestamp()


def test_per_minute_limit_blocks_bursts_and_recovers_after_a_minute() -> None:
    limits = UsageLimits(per_minute=2, daily_per_user=0, daily_global=0)
    acquire("a", now=NOW, limits=limits)
    acquire("a", now=NOW + 1, limits=limits)

    with pytest.raises(UsageLimitExceeded):
        acquire("a", now=NOW + 2, limits=limits)
    acquire("b", now=NOW + 2, limits=limits)
    acquire("a", now=NOW + 61, limits=limits)


def test_daily_limit_is_per_user_and_resets_next_day() -> None:
    limits = UsageLimits(per_minute=0, daily_per_user=2, daily_global=0)
    acquire("a", now=NOW, limits=limits)
    acquire("a", now=NOW + 100, limits=limits)

    with pytest.raises(UsageLimitExceeded, match="今日の AI 利用回数"):
        acquire("a", now=NOW + 200, limits=limits)
    acquire("b", now=NOW + 200, limits=limits)
    acquire("a", now=NOW + 86400, limits=limits)


def test_global_daily_limit_caps_all_users_together() -> None:
    limits = UsageLimits(per_minute=0, daily_per_user=0, daily_global=3)
    for index, scope in enumerate(["a", "b", "c"]):
        acquire(scope, now=NOW + index, limits=limits)

    with pytest.raises(UsageLimitExceeded, match="全体の上限"):
        acquire("d", now=NOW + 10, limits=limits)
    assert usage_snapshot("a", now=NOW + 10).today_global == 3


def test_rejected_call_is_not_counted() -> None:
    limits = UsageLimits(per_minute=1, daily_per_user=0, daily_global=0)
    acquire("a", now=NOW, limits=limits)
    with pytest.raises(UsageLimitExceeded):
        acquire("a", now=NOW + 1, limits=limits)

    assert usage_snapshot("a", now=NOW + 1).today_scope == 1


def test_invalid_env_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYMNOTE_LLM_PER_MINUTE", "abc")
    monkeypatch.setenv("SYMNOTE_LLM_DAILY_GLOBAL", "50")

    limits = usage_limit.load_limits()

    assert limits.per_minute == usage_limit.DEFAULT_PER_MINUTE
    assert limits.daily_global == 50


class _Models:
    def __init__(self, calls: List[str]) -> None:
        self.calls = calls

    def generate_content(self, **_: Any) -> Any:
        self.calls.append("call")
        return type("Response", (), {"text": "ok", "candidates": []})()


class _Client:
    def __init__(self, calls: List[str]) -> None:
        self.models = _Models(calls)

    def close(self) -> None:
        pass


def test_gemini_is_not_called_once_the_limit_is_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("SYMNOTE_LLM_PER_MINUTE", "1")
    calls: List[str] = []

    def factory(*_: Any) -> _Client:
        return _Client(calls)

    llm.generate_text("こんにちは", client_factory=factory)
    with pytest.raises(llm.LLMUnavailableError, match="集中"):
        llm.generate_text("こんにちは", client_factory=factory)

    assert calls == ["call"]


def test_oversized_prompt_is_rejected_before_any_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("SYMNOTE_LLM_MAX_PROMPT_CHARS", "10")
    calls: List[str] = []

    with pytest.raises(llm.LLMUnavailableError, match="長すぎ"):
        llm.generate_text("あ" * 11, client_factory=lambda *_: _Client(calls))

    assert calls == []
