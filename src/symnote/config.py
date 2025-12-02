from __future__ import annotations

from dataclasses import dataclass
import os

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class AppConfig:
    """アプリ全体で利用する設定値。"""

    db_path: str
    llm_api_key: str
    llm_model: str
    max_tokens: int


def load_config() -> AppConfig:
    """環境変数から設定を読み込み、デフォルトは ./symnote.db。"""
    db_path: str = os.getenv("DB_PATH", "./symnote.db")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "gemini-2.0-flash-lite")
    max_tokens: int = int(os.getenv("MAX_TOKENS", "4096"))
    return AppConfig(
        db_path=db_path,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        max_tokens=max_tokens,
    )
