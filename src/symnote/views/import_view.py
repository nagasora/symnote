"""Gmail と ChatGPT の会話から、SymNote 自身の AI で ToDo 候補を探す画面。

AI 呼び出しはボタン押下時だけ行い、Streamlit の再実行では結果表示だけを繰り返す。
見つかった候補は承認待ちに入るだけで、タスクにはならない。
"""

from __future__ import annotations

from typing import Callable, Optional

import streamlit as st

from symnote.config import load_config
from symnote.core import chatgpt_import, gmail_import
from symnote.core.runtime import is_test_mode
from symnote.core.task_extraction import ExtractionResult, extract_task_candidates

GMAIL_RESULT_KEY = "auto_import_gmail_result"
CHATGPT_RESULT_KEY = "auto_import_chatgpt_result"
GMAIL_DAY_OPTIONS = (1, 3, 7, 14, 30)
CHATGPT_DAY_OPTIONS = (7, 30, 90, 365)


def _store_result(key: str, result: ExtractionResult) -> None:
    """再実行後も表示できるよう、結果を表示用の値だけにして保存する。"""
    st.session_state[key] = {
        "message": result.message,
        "created": result.created_count,
        "failed": bool(result.errors),
    }


def _store_error(key: str, message: str) -> None:
    """利用者向けの日本語エラーを結果欄に保存する。"""
    st.session_state[key] = {"message": message, "created": 0, "failed": True}


def _show_result(key: str) -> None:
    """直近の結果を、成功・候補なし・失敗で色分けして表示する。"""
    result = st.session_state.get(key)
    if not result:
        return
    if result["created"]:
        st.success(result["message"])
    elif result["failed"]:
        st.warning(result["message"])
    else:
        st.info(result["message"])


def _run_guarded(key: str, action: Callable[[], ExtractionResult]) -> None:
    """取り込み処理を実行し、既知の失敗は画面を落とさず結果欄に表示する。"""
    try:
        with st.spinner("AI が ToDo を探しています…"):
            _store_result(key, action())
    except (gmail_import.GmailImportError, chatgpt_import.ChatGPTImportError) as exc:
        _store_error(key, str(exc))
    except ValueError as exc:
        _store_error(key, f"入力を確認してください: {exc}")
    except Exception:
        _store_error(key, "取り込み中に予期しないエラーが発生しました。時間をおいて再度お試しください。")


def _render_ai_key_notice() -> None:
    """API キーが無くても画面は使えることを明示する。"""
    if not load_config().llm_api_key.strip():
        st.warning(
            "Gemini の API キー（.env の LLM_API_KEY）が未設定のため、AI による抽出は実行できません。"
            "接続やファイルの選択はできます。"
        )


def _render_gmail_section() -> None:
    """Gmail の接続状態と、直近メールからの抽出ボタンを描画する。"""
    st.markdown("#### Gmail")
    st.caption("受信トレイ（プロモーション・ソーシャルを除く）を読み取り専用で読みます。メールの変更・送信はしません。")
    if is_test_mode():
        st.info("テスト環境では Gmail に接続しません。")
        return
    if not gmail_import.is_connected():
        st.info("Gmail はまだ接続されていません。")
        if st.button("Gmail に接続する（読み取り専用）", key="auto_import_gmail_connect"):
            try:
                with st.spinner("ブラウザで Google の承認画面を開いています…"):
                    gmail_import.connect()
            except gmail_import.GmailImportError as exc:
                st.error(str(exc))
            else:
                st.rerun()
        return
    st.success("Gmail と接続中です（読み取り専用）。")
    days = int(
        st.selectbox(
            "対象期間",
            GMAIL_DAY_OPTIONS,
            index=GMAIL_DAY_OPTIONS.index(7),
            format_func=lambda value: f"直近 {value} 日",
            key="auto_import_gmail_days",
        )
    )
    max_messages = int(
        st.number_input("最大メール件数", min_value=1, max_value=100, value=20, step=5, key="auto_import_gmail_max")
    )
    if st.button(f"直近{days}日のメールから ToDo を探す", type="primary", key="auto_import_gmail_run"):
        _run_guarded(GMAIL_RESULT_KEY, lambda: gmail_import.import_recent_messages(days, max_messages))
    st.caption("一度 AI で読んだメールは次回から読み直しません（AI が失敗したメールは次回に再試行します）。")
    _show_result(GMAIL_RESULT_KEY)
    if st.button("Gmail の接続を解除", key="auto_import_gmail_disconnect"):
        try:
            gmail_import.disconnect()
        except gmail_import.GmailImportError as exc:
            st.error(str(exc))
        else:
            st.session_state.pop(GMAIL_RESULT_KEY, None)
            st.rerun()


def _import_chatgpt_file(payload: bytes, days: int, max_conversations: int) -> ExtractionResult:
    """エクスポートを解析し、対象会話があれば抽出する。"""
    items = chatgpt_import.parse_export(payload, days=days, max_conversations=max_conversations)
    if not items:
        raise chatgpt_import.ChatGPTImportError(f"直近 {days} 日に該当する会話がありませんでした。")
    return extract_task_candidates("chat", items)


def _import_chatgpt_text(text: str) -> ExtractionResult:
    """貼り付けた会話を 1 件の出典として抽出する。"""
    return extract_task_candidates("chat", [chatgpt_import.parse_pasted_text(text)])


def _render_chatgpt_section() -> None:
    """ChatGPT のエクスポートと貼り付けテキストの取り込み欄を描画する。"""
    st.markdown("#### ChatGPT の会話")
    st.caption(
        "ChatGPT の「設定 → データコントロール → データをエクスポート」を押すと、"
        "登録メールアドレスに ZIP のダウンロードリンクが届きます。その ZIP（または中の conversations.json）を選んでください。"
        "ファイルはこの PC 内で読み取り、保存しません。"
    )
    days = int(
        st.selectbox(
            "対象期間",
            CHATGPT_DAY_OPTIONS,
            index=CHATGPT_DAY_OPTIONS.index(30),
            format_func=lambda value: f"直近 {value} 日",
            key="auto_import_chatgpt_days",
        )
    )
    max_conversations = int(
        st.number_input(
            "最大会話数", min_value=1, max_value=100, value=20, step=5, key="auto_import_chatgpt_max"
        )
    )
    uploaded = st.file_uploader(
        "エクスポートの ZIP / conversations.json", type=["zip", "json"], key="auto_import_chatgpt_file"
    )
    if st.button("ファイルの会話から ToDo を探す", type="primary", key="auto_import_chatgpt_run_file"):
        if uploaded is None:
            _store_error(CHATGPT_RESULT_KEY, "先に ZIP または conversations.json を選んでください。")
        elif uploaded.size > chatgpt_import.MAX_UPLOAD_BYTES:
            _store_error(CHATGPT_RESULT_KEY, "アップロードは25MB以下にしてください。")
        else:
            payload = uploaded.getvalue()
            _run_guarded(CHATGPT_RESULT_KEY, lambda: _import_chatgpt_file(payload, days, max_conversations))
    pasted: Optional[str] = st.text_area(
        "または会話を貼り付け",
        height=160,
        placeholder="ChatGPT の画面から会話をコピーして貼り付けてください。",
        key="auto_import_chatgpt_text",
    )
    if st.button("貼り付けた会話から ToDo を探す", key="auto_import_chatgpt_run_text"):
        text = pasted or ""
        _run_guarded(CHATGPT_RESULT_KEY, lambda: _import_chatgpt_text(text))
    _show_result(CHATGPT_RESULT_KEY)


def render_auto_import() -> None:
    """Gmail と ChatGPT からの ToDo 自動抽出の欄を描画する。"""
    st.subheader("メールや会話から ToDo を探す")
    st.caption("SymNote の AI が読み、見つけた ToDo は「承認待ち」に入ります。承認したものだけがタスクになります。")
    _render_ai_key_notice()
    _render_gmail_section()
    st.divider()
    _render_chatgpt_section()
