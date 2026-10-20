"""SymNote Web サーバーの起動エントリポイント（symnote-web）。"""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from symnote.api.app import create_app
from symnote.api.auth import auth_mode
from symnote.core.accounts import configured_owner_email, invite_user
from symnote.core.runtime import is_public_bind


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SymNote Web サーバーを起動する")
    parser.add_argument(
        "--host",
        default=os.getenv("SYMNOTE_WEB_HOST", "127.0.0.1"),
        help="待受ホスト（既定: 127.0.0.1）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("SYMNOTE_WEB_PORT", "8000")),
        help="待受ポート（既定: 8000）",
    )
    args = parser.parse_args(argv)

    # 外部バインド時に認証設定が無ければ起動を拒否する（フェイルクローズ）
    if is_public_bind(args.host) and auth_mode() == "local":
        print(
            "エラー: 外部ネットワーク（0.0.0.0 等）へ公開する場合は、"
            "SYMNOTE_GOOGLE_CLIENT_ID / SECRET または SYMNOTE_APP_PASSCODE の設定が必須です。",
            file=sys.stderr,
        )
        raise SystemExit(2)

    owner_email = configured_owner_email()
    if owner_email:
        invite_user(owner_email, is_owner=True, note="owner")

    app = create_app()
    # OAuth callback query strings contain one-time credentials.
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
