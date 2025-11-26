from __future__ import annotations

from dataclasses import dataclass
import os

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class AppConfig:
    """アプリ全体で利用する設定値。"""

    db_path: str
    openai_api_key: str


def load_config() -> AppConfig:
    """環境変数から設定を読み込み、デフォルトは ./symnote.db。"""
    db_path: str = os.getenv("DB_PATH", "./symnote.db")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    return AppConfig(db_path=db_path, openai_api_key=openai_api_key)
