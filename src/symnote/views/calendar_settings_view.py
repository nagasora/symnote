"""Google Calendar の接続・保存設定・同期操作を表示する。"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from symnote.core.calendar_events import (
    CalendarListPermissionError,
    list_calendars,
    propose_prep_tasks_from_events,
)
from symnote.core.calendar_settings import (
    calendar_error_message,
    effective_config,
    get_setting,
    save_client_json,
    set_setting,
)
from symnote.core.calendar_task_export import DEFAULT_EXPORT_TASKS
from symnote.core.google_calendar import connect, disconnect, is_connected, sync_pending_tasks


def _connect() -> None:
    """Desktop OAuth を開始し、失敗理由を安全な案内に変換する。"""
    try:
        connect(effective_config())
        st.success("Google Calendar に接続しました。")
        st.rerun()
    except Exception as exc:
        st.error(calendar_error_message(exc))


def render_google_calendar_settings() -> None:
    """任意の Google Calendar 連携を設定し、未接続でもローカル機能を維持する。"""
    st.subheader("Google Calendar 連携")
    config = effective_config()
    connected = is_connected()
    st.write("接続状態: 接続済み（保存済み認証情報あり）" if connected else "接続状態: 未接続")
    st.caption(
        "タスクとメモの原本はローカルに保存されます。解除しても Google 上の予定は削除しません。"
    )
    if not Path(config.google_calendar_client_secret_path).expanduser().is_file():
        st.info("Desktop OAuth クライアント JSON をアップロードすると接続できます。")
        st.markdown(
            "1. Google Cloud で **Google Calendar API を有効化**\n"
            "2. **OAuth 同意画面**を設定\n"
            "3. OAuth クライアントの種類 **デスクトップ アプリ**を作成して JSON を取得\n"
            "4. 公開ステータスがテストの場合、**テストユーザーに自分のアカウントを追加**"
        )
        upload = st.file_uploader(
            "Desktop OAuth クライアント JSON", type=["json"], key="calendar_client_upload"
        )
        if upload is not None and st.button("JSON を検証して保存", key="calendar_save_client"):
            try:
                save_client_json(upload.getvalue())
                st.success("OAuth クライアントを所有者のみ読み書きできる権限で保存しました。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
            except OSError:
                st.error(
                    "ファイルを保存できません。ユーザーデータディレクトリの書き込み権限を確認してください。"
                )
    if not connected:
        if st.button(
            "Google Calendar に接続",
            disabled=not Path(config.google_calendar_client_secret_path).is_file(),
            key="calendar_connect",
        ):
            _connect()
        return
    st.caption(
        f"朝のまとめ: 毎日 {config.google_calendar_morning_digest_hour:02d}:00 / {config.google_calendar_timezone}"
    )
    st.write(f"現在の対象カレンダー: {config.google_calendar_id}")
    if st.button("対象カレンダーの一覧を取得", key="calendar_load_choices"):
        try:
            st.session_state["calendar_choices"] = list_calendars()
        except CalendarListPermissionError as exc:
            st.warning(str(exc))
        except Exception as exc:
            st.error(calendar_error_message(exc))
    choices = st.session_state.get("calendar_choices", [])
    if choices:
        ids = [row["id"] for row in choices]
        names = {row["id"]: row.get("summary", row["id"]) for row in choices}
        selected = st.selectbox(
            "書き出し・取得先",
            ids,
            index=ids.index(config.google_calendar_id) if config.google_calendar_id in ids else 0,
            format_func=lambda value: names[value],
            key="calendar_target",
        )
        if st.button("対象カレンダーを保存", key="calendar_save_target"):
            set_setting("calendar_id", selected)
            st.rerun()
    enabled = get_setting("export_tasks", DEFAULT_EXPORT_TASKS) == "1"
    export = st.checkbox(
        "ToDo を自動で Google カレンダーに登録する", value=enabled, key="calendar_export_tasks"
    )
    st.caption(
        "期限日のある ToDo を追加・変更すると自動で反映します（時刻ありは 30 分の予定、日付だけなら終日）。"
        "完了・削除すると予定も消えます。日付のない ToDo は登録しません。"
    )
    if export != enabled:
        set_setting("export_tasks", "1" if export else "0")
        st.caption(
            "設定を保存しました。次回同期で反映します。OFF にすると既存の個別予定を削除します。"
        )
    if st.button("今すぐ同期", key="calendar_sync_now"):
        result = sync_pending_tasks()
        if result.failed:
            st.warning(calendar_error_message(RuntimeError(result.message)))
        else:
            st.success(f"同期しました（{result.synced} 件）。")
    if st.button("今後7日間の予定から準備タスクを提案", key="calendar_propose_prep"):
        try:
            result = propose_prep_tasks_from_events()
            st.success(
                f"承認待ちに {len(result.created_ids)} 件登録しました（重複 {result.duplicate_count} 件）。候補の承認画面で確認してください。"
            )
        except Exception as exc:
            st.error(calendar_error_message(exc))
    if st.button("再接続（権限の更新）", key="calendar_reconnect"):
        st.session_state.pop("calendar_choices", None)
        _connect()
    if st.button("接続を解除", key="calendar_disconnect"):
        try:
            disconnect()
            st.session_state.pop("calendar_choices", None)
            st.rerun()
        except Exception as exc:
            st.error(calendar_error_message(exc))
