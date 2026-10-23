"""全テスト共通の設定。利用者の実 DB に触れないことを保証する。"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """DB_PATH を既定で一時ファイルに向ける。個別のテストが上書きしてもよい。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "isolated.db"))
    monkeypatch.setenv("SYMNOTE_STORAGE_BACKEND", "sqlite")


@pytest.fixture(autouse=True)
def _isolated_ai_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """ローカルのプロバイダー・実キーを拾わず、各テストが必要な設定だけを渡す。"""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("SYMNOTE_INTEGRATION_KEY", "")
    monkeypatch.setenv("SYMNOTE_SLACK_CLIENT_ID", "")
    monkeypatch.setenv("SYMNOTE_SLACK_CLIENT_SECRET", "")
