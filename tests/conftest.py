"""全テスト共通の設定。利用者の実 DB に触れないことを保証する。"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """DB_PATH を既定で一時ファイルに向ける。個別のテストが上書きしてもよい。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "isolated.db"))
