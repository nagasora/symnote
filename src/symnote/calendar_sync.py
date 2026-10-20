"""Google Calendar の朝のまとめを、アプリを開かずに同期するコマンド。

launchd や cron から定期実行し、毎朝の通知予定を最新に保つ。

実行例::

    DB_PATH=/absolute/path/to/symnote.db python -m symnote.calendar_sync
"""

from __future__ import annotations

import sys

from symnote.config import is_absolute_db_path, load_config
from symnote.core.db import init_db
from symnote.core.google_calendar import is_connected, sync_pending_tasks


def run() -> int:
    """同期を 1 回実行し、終了コード（成功・未接続は 0、失敗は 1、設定不備は 2）を返す。"""
    config = load_config()
    if not is_absolute_db_path(config.db_path):
        # 定期実行は作業ディレクトリが不定なので、相対パスだと別の空 DB を作ってしまう。
        print(
            f"DB_PATH は絶対パスで指定してください（現在: {config.db_path!r}）。",
            file=sys.stderr,
        )
        return 2
    if not is_connected():
        print("Google Calendar が未接続のため、同期をスキップしました。")
        return 0
    init_db()
    result = sync_pending_tasks(config)
    print(
        f"同期 {result.synced} 件 / 失敗 {result.failed} 件"
        + (f": {result.message}" if result.message else "")
    )
    return 1 if result.failed else 0


def main() -> None:
    """コマンドラインの入口。"""
    raise SystemExit(run())


if __name__ == "__main__":
    main()
