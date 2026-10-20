"""設定画面の「AI 連携」：ChatGPT / Claude から SymNote を使うための接続状態と手順。"""

from __future__ import annotations

import os
import time
from typing import Dict, Optional

import streamlit as st

from symnote.core.db import connection


def remote_connection_status(now: Optional[float] = None) -> Dict[str, int]:
    """リモート MCP の登録クライアント数と、有効なトークン数を返す。"""
    now = now or time.time()
    with connection() as conn:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "oauth_clients" not in tables:
            return {"clients": 0, "active_tokens": 0}
        clients = conn.execute("SELECT count(*) FROM oauth_clients").fetchone()[0]
        active = conn.execute(
            "SELECT count(*) FROM oauth_tokens WHERE expires_at > ? AND consumed_at IS NULL",
            (int(now),),
        ).fetchone()[0]
    return {"clients": int(clients), "active_tokens": int(active)}


def render_connections_settings() -> None:
    """ChatGPT・Claude からの接続方法と、いまの接続状態を表示する。"""
    public_url = os.getenv("SYMNOTE_MCP_PUBLIC_URL", "").strip().rstrip("/")
    status = remote_connection_status()
    st.subheader("ChatGPT から使う")
    st.caption(
        "ChatGPT が Gmail・Slack・カレンダーなどから見つけた ToDo を、SymNote の「受信箱」に候補として届けます。"
        "候補はあなたが承認したものだけがタスクになります。"
    )
    if not public_url:
        st.warning("公開 URL が未設定です（.env の SYMNOTE_MCP_PUBLIC_URL）。")
        return
    mcp_url = f"{public_url}/mcp"
    if status["active_tokens"]:
        st.success("ChatGPT / Claude と接続中です。")
    else:
        st.info("まだ ChatGPT と接続されていません。下の手順で登録してください。")
    st.markdown("**接続 URL**（コピーして使います）")
    st.code(mcp_url, language=None)
    st.markdown(
        """
1. ブラウザで **chatgpt.com/plugins** を開く（または ChatGPT の「設定 → アプリ」）。
2. **＋** →「**Add custom MCP server**（カスタム MCP サーバーを追加）」を選ぶ。
3. 名前に `SymNote`、説明に「タスク候補の受け取り」と入力。
4. 接続方法は「**Public endpoint**」を選び、URL に上の接続 URL を貼り付ける。
5. 認証は **OAuth** を選ぶ（クライアント ID / シークレット欄は空のまま）。
6. 注意書きを読んで「I understand and want to continue」→「**Create as a plugin**」。
7. SymNote の同意画面が開いたら、`.env` の `SYMNOTE_MCP_PASSPHRASE` の値を入力して承認。
8. 新しいチャットで `@SymNote` を選び、「今週のメールから私のToDoを洗い出して SymNote に提案して」のように頼む。
"""
    )
    st.caption(
        "同意画面で「Invalid Origin」と出た場合は、ChatGPT 側でもう一度「接続」からやり直してください"
        "（2026-10-16 の修正で解消済み。古い同意画面のタブは閉じてください）。"
    )
    st.caption(
        "メニュー名は ChatGPT の更新で変わることがあります。Business / Enterprise のワークスペースでは、"
        "管理者がカスタム MCP（開発者モード）を許可している必要があります。"
    )
    if "trycloudflare.com" in public_url:
        st.warning(
            "いまの URL は一時的なものです。Mac の再起動やトンネルの停止で変わり、そのたびに ChatGPT 側で登録し直しが必要です。"
            "固定するには Cloudflare で独自ドメインを使います。"
        )
    st.caption(f"登録済みクライアント: {status['clients']} 件 / 有効な接続: {status['active_tokens']} 件")

    st.subheader("Claude（claude.ai）から使う")
    st.markdown(
        f"「設定 → コネクタ → カスタムコネクタを追加」で URL に `{mcp_url}` を入力し、同じパスフレーズで承認します。"
    )
