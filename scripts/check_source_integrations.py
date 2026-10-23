"""情報源連携の起動前設定を秘密値やDBを読み出さずに確認する。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from symnote.config import load_config
from symnote.core import source_connections as store, source_providers as remote


def main() -> int:
    config = load_config()
    result = {
        "ai_configured": bool(config.llm_api_key.strip()),
        "encryption_ready": store.encryption_ready(),
        "providers": {},
        "note": "設定の存在確認のみ。実OAuth・取得・AI抽出の成功を示しません。",
    }
    for provider in ("gmail", "slack"):
        try:
            callback = remote.callback_url(provider)
        except ValueError:
            callback = None
        client_id, secret = remote.provider_config(provider)
        result["providers"][provider] = {
            "client_configured": bool(client_id and secret),
            "callback_url": callback,
            "ready_to_authorize": remote.ready(provider),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(p["ready_to_authorize"] for p in result["providers"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
