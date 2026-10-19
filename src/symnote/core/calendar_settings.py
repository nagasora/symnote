"""Google Calendar の利用者設定と Desktop OAuth ファイルを管理する。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict

from symnote.config import AppConfig, load_config
from symnote.core.db import connection


def get_setting(key: str, default: str = "") -> str:
    """環境変数より優先するローカル設定を取得する。"""
    with connection() as conn:
        row = conn.execute("SELECT value FROM calendar_settings WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else default


def set_setting(key: str, value: str) -> None:
    """ローカル設定をトランザクションで保存する。"""
    with connection() as conn, conn:
        conn.execute(
            "INSERT INTO calendar_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        if key == "export_tasks":
            conn.execute(
                "INSERT INTO calendar_sync_outbox(task_id, operation, changed_at) "
                "SELECT id, 'upsert', strftime('%Y-%m-%dT%H:%M:%f', 'now') "
                "FROM items WHERE kind = 'task' "
                "ON CONFLICT(task_id) DO UPDATE SET changed_at = excluded.changed_at"
            )


def effective_config(config: AppConfig | None = None) -> AppConfig:
    """保存済みカレンダー選択を反映し、環境変数は既定値として残す。"""
    config = config or load_config()
    client_path = Path(config.google_calendar_client_secret_path).expanduser()
    uploaded_path = Path(config.db_path).expanduser().parent / "google_oauth_client.json"
    return replace(
        config,
        google_calendar_id=get_setting("calendar_id", config.google_calendar_id),
        google_calendar_client_secret_path=str(
            client_path if client_path.is_file() else uploaded_path
        ),
    )


def validate_client_json(payload: bytes) -> Dict[str, Any]:
    """Desktop 型 OAuth JSON を検証し、秘密情報を例外へ含めず返す。"""
    if len(payload) > 1024 * 1024:
        raise ValueError("JSON ファイルは 1MB 以下にしてください。")
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeError):
        raise ValueError("有効な JSON ファイルを選択してください。") from None
    if not isinstance(data, dict) or not isinstance(data.get("installed"), dict):
        raise ValueError(
            "Desktop 型の OAuth クライアントを作成してください。web 型は利用できません。"
        )
    for key in ("client_id", "client_secret"):
        if not isinstance(data["installed"].get(key), str) or not data["installed"][key].strip():
            raise ValueError(f"Desktop OAuth JSON に {key} がありません。")
    return data


def save_client_json(payload: bytes, config: AppConfig | None = None) -> Path:
    """検証済み JSON を DB と同じディレクトリへ 0600 で原子的に保存する。"""
    data = validate_client_json(payload)
    config = config or load_config()
    if config.db_path == ":memory:" or config.db_path.startswith("file:"):
        raise ValueError("OAuth ファイルの保存には通常の DB ファイルパスが必要です。")
    directory = Path(config.db_path).expanduser().resolve().parent
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "google_oauth_client.json"
    fd, temporary = tempfile.mkstemp(prefix=".google-oauth-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def calendar_error_message(error: Exception) -> str:
    """認証・API・通信エラーを秘密情報のない日本語の案内へ変換する。"""
    from symnote.core.runtime import CalendarWriteBlocked

    if isinstance(error, CalendarWriteBlocked):
        return str(error)
    text = str(error).lower()
    if "access_denied" in text or "test user" in text or "testing" in text:
        return "アクセスが拒否されました。OAuth 同意画面のテストユーザーに自分の Google アカウントを追加し、権限を許可してください。"
    if any(
        word in text
        for word in ("accessnotconfigured", "service_disabled", "has not been used", "not enabled")
    ):
        return "Google Calendar API が未有効化です。OAuth クライアントと同じ Cloud プロジェクトで有効にしてください。"
    if any(word in text for word in ("scope", "insufficient", "invalid_grant", "期限切れ")):
        return "認証の期限切れ、または権限不足です。再接続してカレンダーの権限を許可してください。"
    if any(word in text for word in ("network", "connection", "timeout", "offline", "dns")):
        return "ネットワークに接続できません。接続を確認して再試行してください。保存済みの予定とローカルタスクは引き続き利用できます。"
    return "Google Calendar の操作に失敗しました。ネットワーク、OAuth 設定、カレンダーへのアクセス権を確認してください。"
