from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class AppConfig:
    """アプリ全体で利用する設定値。"""

    db_path: str
    llm_api_key: str
    llm_model: str
    max_tokens: int
    google_calendar_client_secret_path: str
    google_calendar_id: str
    google_calendar_reminder_minutes: int
    google_calendar_timezone: str
    google_calendar_deadline_hour: int
    google_calendar_morning_digest_hour: int


def load_config() -> AppConfig:
    """環境変数から設定を読み込み、デフォルトは ./symnote.db。"""
    configured_db_path = os.getenv("DB_PATH")
    if configured_db_path:
        # An explicit path is user-owned configuration and must be left intact.
        # SQLite は "~" を展開しないため、ホーム相対の指定だけはここで解決する。
        db_path = (
            str(Path(configured_db_path).expanduser())
            if configured_db_path.startswith("~")
            else configured_db_path
        )
    else:
        data_home = os.getenv("LOCALAPPDATA") or os.getenv("XDG_DATA_HOME")
        root = Path(data_home) if data_home else Path.home() / ".local" / "share"
        db_path = str(root / "SymNote" / "symnote.db")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "gemini-3.1-flash-lite")
    max_tokens: int = int(os.getenv("MAX_TOKENS", "4096"))
    data_dir = Path(db_path).expanduser().parent
    google_calendar_client_secret_path = os.getenv(
        "GOOGLE_CALENDAR_CLIENT_SECRET_PATH",
        str(data_dir / "google_oauth_client.json"),
    )
    return AppConfig(
        db_path=db_path,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        max_tokens=max_tokens,
        google_calendar_client_secret_path=google_calendar_client_secret_path,
        google_calendar_id=os.getenv("GOOGLE_CALENDAR_ID", "primary"),
        google_calendar_reminder_minutes=int(
            os.getenv("GOOGLE_CALENDAR_REMINDER_MINUTES", "30")
        ),
        google_calendar_timezone=os.getenv("GOOGLE_CALENDAR_TIMEZONE", "Asia/Tokyo"),
        google_calendar_deadline_hour=int(os.getenv("GOOGLE_CALENDAR_DEADLINE_HOUR", "9")),
        google_calendar_morning_digest_hour=int(
            os.getenv("GOOGLE_CALENDAR_MORNING_DIGEST_HOUR", "8")
        ),
    )


def is_absolute_db_path(db_path: str) -> bool:
    """DB_PATH が作業ディレクトリに依存しない場所を指すかを返す。

    MCP サーバーや定期同期は任意の作業ディレクトリで起動されるため、
    相対パス（``file:`` URI の相対パスを含む）では別の空 DB を作ってしまう。
    """
    if db_path == ":memory:":
        return True
    if db_path.startswith("file:"):
        return Path(urlparse(db_path).path).is_absolute()
    return Path(db_path).is_absolute()
