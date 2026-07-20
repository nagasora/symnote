from __future__ import annotations

from typing import Any, Dict, List, Tuple
import datetime as dt
import json

import streamlit as st

from symnote.core.db import (
    ClassificationSummary,
    classify_inbox_items,
    fetch_inbox,
    fetch_tasks,
    fetch_tasks_for_today_view,
    init_db,
    insert_memo,
    insert_task,
    update_item_fields,
    create_idea_session,
    update_idea_session_chat,
    fetch_idea_sessions,
    get_idea_session,
    delete_item,
    delete_idea_session,
    complete_task,
    create_recurring_task,
    recurrence_label,
    stop_task_recurrence,
)
from symnote.core.nlp import (
    classify_text_rule_based,
    priority_score,
    Effort,
    Energy,
    suggest_today_tasks,
    generate_todos_from_idea,
    summarize_and_extract_tasks_from_text,
    analyze_source_and_generate_title,
    brainstorm_ideas,
    generate_initial_mindmap,
    expand_node,
)
from symnote.core.mindmap import (
    MindmapNode,
    create_node,
    get_nodes_by_project,
    update_node,
    delete_node_and_descendants,
    ensure_task_item_for_node,
    save_mindmap_tree,
)
from symnote.calendar_app import render_calendar_tab
from symnote.config import load_config
from symnote.core.doc_loader import extract_text_from_file
from symnote.core.google_calendar import (
    CalendarSetupError,
    connect as connect_google_calendar,
    disconnect as disconnect_google_calendar,
    is_connected as google_calendar_connected,
    sync_pending_tasks,
)


def _due_priority_tuple(task: Dict) -> Tuple[dt.date, float, int]:
    """Sort tasks by due date, then by their calculated priority score."""
    date_str = task.get("due_date")
    try:
        due = dt.date.fromisoformat(date_str) if date_str else dt.date.max
    except ValueError:
        due = dt.date.max
    importance = task.get("importance") or 3
    urgency = task.get("urgency") or 3
    effort: Effort = task.get("effort") or "medium"
    energy: Energy = task.get("energy") or "mid"
    score = priority_score(importance, urgency, effort, energy)
    return (due, -score, task["id"])


def _task_overview(tasks: List[Dict], today: dt.date | None = None) -> Dict[str, int]:
    """Return small, display-oriented task counts without changing task data."""
    today = today or dt.date.today()
    overview = {"active": 0, "today": 0, "overdue": 0, "done": 0}
    for task in tasks:
        if task.get("status") == "done":
            overview["done"] += 1
            continue
        overview["active"] += 1
        due_str = task.get("due_date")
        try:
            due = dt.date.fromisoformat(due_str) if due_str else None
        except ValueError:
            due = None
        if due and due < today:
            overview["overdue"] += 1
        if task.get("status") == "today" or due == today:
            overview["today"] += 1
    return overview


def _apply_app_style() -> None:
    """Keep the personal workspace calm and make primary actions easy to spot."""
    st.markdown(
        """
        <style>
          .block-container { max-width: 1180px; padding-top: 1.6rem; padding-bottom: 3rem; }
          [data-testid="stSidebar"] { border-right: 1px solid rgba(128, 128, 128, .18); }
          [data-testid="stMetric"] { padding: .55rem .15rem; }
          .symnote-kicker { color: #6b7280; font-size: .9rem; margin-bottom: .25rem; }
          .symnote-page-title { margin: 0 0 .25rem; font-size: 1.7rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_sidebar() -> str:
    """Render the persistent navigation and a compact personal task snapshot."""
    all_tasks = fetch_tasks(limit=500)
    overview = _task_overview(all_tasks)
    pages = {
        "今日": "📌 今日",
        "インボックス": "📝 インボックス",
        "タスク整理": "🗂️ タスク整理",
        "カレンダー": "🗓️ カレンダー",
        "アイデア整理": "💡 アイデア整理",
        "ドキュメント分析": "📄 ドキュメント分析",
    }
    pages["Google Calendar"] = "📆 Google Calendar"
    with st.sidebar:
        st.title("SymNote 🧠")
        st.caption("考えを残し、今日やることを決める。")
        selected = st.radio(
            "画面",
            list(pages),
            format_func=pages.get,
            label_visibility="collapsed",
        )
        st.divider()
        st.caption("タスクの状況")
        first, second = st.columns(2)
        first.metric("今日", overview["today"])
        second.metric("期限切れ", overview["overdue"])
        first, second = st.columns(2)
        first.metric("未完了", overview["active"])
        second.metric("完了", overview["done"])
        st.divider()
        st.caption("データはこの端末の SQLite に保存されています。")
        if not load_config().llm_api_key:
            st.info("AI機能はAPIキー未設定でも、メモ・タスク管理はそのまま使えます。")
    return selected


def render_google_calendar_tab() -> None:
    """Configure the optional, local-first Google Calendar integration."""
    st.subheader("📆 Google Calendar 同期")
    st.caption(
        "SymNote の未完了タスク（期限あり）を Google Calendar に自動同期します。"
        "オフライン時の変更は端末内に保留され、次回に再試行されます。"
    )
    config = load_config()
    st.write(f"対象カレンダー: `{config.google_calendar_id}`")
    st.write(
        f"通知: 期限日 {config.google_calendar_deadline_hour:02d}:00 の Google Calendar "
        f"ポップアップ通知（{config.google_calendar_reminder_minutes} 分前）"
    )
    st.caption(
        f"期限は日付のみのため、期限日の {config.google_calendar_deadline_hour:02d}:00 から 30 分の予定として登録されます。端末で通知を受け取るには、"
        "Google Calendar アプリ／ブラウザ側で通知を許可してください。"
    )

    if not google_calendar_connected():
        st.info(
            "未接続です。Google Cloud で Calendar API を有効にし、Desktop OAuth クライアントを作成後、"
            "ダウンロードした JSON のパスを GOOGLE_CALENDAR_CLIENT_SECRET_PATH に設定してください。"
        )
        if st.button("Google Calendar に接続", type="primary"):
            try:
                connect_google_calendar(config)
            except CalendarSetupError as exc:
                st.error(str(exc))
            except Exception:
                st.error("接続に失敗しました。OAuth 設定を確認して再試行してください。")
            else:
                result = sync_pending_tasks(config)
                if result.failed:
                    st.warning(f"{result.pending} 件を同期保留にしました。次回起動時に再試行します。")
                else:
                    st.success(f"接続し、{result.synced} 件を同期しました。")
        return

    st.success("接続済みです。タスクの保存・編集・完了・削除は自動で同期されます。")
    left, right = st.columns(2)
    with left:
        if st.button("今すぐ同期", type="primary"):
            result = sync_pending_tasks(config)
            if result.failed:
                st.warning(f"{result.pending} 件を保留しました。ネットワークまたは認証を確認してください。")
            else:
                st.success(f"{result.synced} 件を同期しました。")
    with right:
        if st.button("接続を解除"):
            try:
                disconnect_google_calendar()
            except Exception:
                st.error("資格情報の削除に失敗しました。OS の資格情報ストアを確認してください。")
            else:
                st.success("この端末の Google Calendar 接続を解除しました。")


def render_inbox_tab() -> None:
    st.subheader("📝 インボックスに追加")
    with st.form("inbox_form", clear_on_submit=True):
        tags = st.text_input("タイトル (タグ)", placeholder="タスクの概要")
        raw_text = st.text_area(
            "詳細",
            height=150,
            placeholder="例: 来週のゼミ発表の準備… / 論文Xを読む / バイトのシフト調整...",
        )
        
        # 期限の選択
        st.markdown("**期限**")
        col_due_1, col_due_2 = st.columns([1, 1])
        with col_due_1:
            due_option = st.radio(
                "期限プリセット", 
                ["今日", "明日", "カレンダーから選択"], 
                horizontal=True,
                label_visibility="collapsed"
            )
        with col_due_2:
            custom_date = st.date_input(
                "日付を指定", 
                value=dt.date.today(), 
                label_visibility="collapsed"
            )

        # tags = st.text_input("タグ（カンマ区切り・任意）", "") # Removed separate tags input
        
        recurrence = st.selectbox(
            "繰り返し",
            ["なし", "毎日", "毎週", "隔週"],
            help="完了すると次回分のタスクが自動で作成されます。",
        )
        col_sub1, col_sub2 = st.columns(2)
        with col_sub1:
            submitted_memo = st.form_submit_button("メモとして保存", type="secondary")
        with col_sub2:
            submitted_task = st.form_submit_button("タスクとして保存", type="primary")
        
        # 期限の決定
        due_date_str = None
        today = dt.date.today()
        if due_option == "今日":
            due_date_str = today.isoformat()
        elif due_option == "明日":
            due_date_str = (today + dt.timedelta(days=1)).isoformat()
        elif due_option == "カレンダーから選択":
            due_date_str = custom_date.isoformat()

        if submitted_memo:
            if not raw_text.strip():
                st.warning("メモ本文を入力してください。")
            else:
                note_id = insert_memo(raw_text=raw_text, tags=tags, date_str=due_date_str)
                st.success(f"メモを保存しました (ID: {note_id})")

        if submitted_task:
            if not raw_text.strip():
                st.warning("タスク本文を入力してください。")
            else:
                # ``date`` is the creation date; the selected date is the task deadline.
                cadence = {"毎日": ("daily", 1), "毎週": ("weekly", 1), "隔週": ("weekly", 2)}
                if recurrence in cadence:
                    frequency, interval = cadence[recurrence]
                    note_id = create_recurring_task(
                        raw_text=raw_text,
                        tags=tags,
                        due_date=due_date_str or today.isoformat(),
                        frequency=frequency,
                        interval=interval,
                    )
                else:
                    note_id = insert_task(raw_text=raw_text, tags=tags, due_date=due_date_str)
                st.success(f"タスクを保存しました (ID: {note_id})")

    if st.button("AI でインボックスを整理", type="primary"):
        summary: ClassificationSummary = classify_inbox_items(classify_text_rule_based)
        if summary.processed == 0:
            st.info("新しく整理するメモはありません。")
        else:
            st.success(
                f"処理 {summary.processed} 件 / タスク化 {summary.converted_to_task} 件 / "
                f"today {summary.updated_status_today} 件 / week {summary.updated_status_week} 件"
            )

    st.divider()
    st.subheader("📥 今日のインボックス")
    inbox_items = fetch_inbox(limit=100)
    if not inbox_items:
        st.info("まだインボックスは空です。上のフォームからメモを追加してください。")
        return
    for item in inbox_items:
        with st.expander(f"ID {item['id']} | {item['date']} | {item.get('tags') or 'No Title'}"):
            st.write(item["raw_text"])
            if item.get("tags"):
                st.caption(f"タグ: {item['tags']}")


def render_task_editor(task: Dict) -> None:
    default_importance = task.get("importance") or 3
    default_urgency = task.get("urgency") or 3
    default_effort = task.get("effort") or "medium"
    default_energy = task.get("energy") or "mid"
    default_status = task.get("status") or "inbox"

    default_tags = task.get("tags") or ""
    default_raw_text = task.get("raw_text", "")
    default_due_date_str = task.get("due_date")
    default_due_date = None
    if default_due_date_str:
        try:
            default_due_date = dt.date.fromisoformat(default_due_date_str)
        except ValueError:
            pass

    st.markdown(f"**ID {task['id']}** | {task.get('ai_category', 'task')}")
    if cadence := recurrence_label(task):
        st.caption(f"繰り返し: {cadence}（完了時に次回分を作成）")
    
    with st.form(f"task_form_{task['id']}"):
        # Content Editor
        new_tags = st.text_input("タイトル (タグ)", default_tags, key=f"tags_input_{task['id']}")
        raw_text = st.text_area("詳細", default_raw_text, key=f"text_{task['id']}")
        
        cols = st.columns(4)
        with cols[0]:
            importance = st.slider("重要度", 1, 5, default_importance, key=f"imp_{task['id']}")
        with cols[1]:
            urgency = st.slider("緊急度", 1, 5, default_urgency, key=f"urg_{task['id']}")
        with cols[2]:
            effort = st.selectbox(
                "所要時間", ["short", "medium", "long"], index=["short", "medium", "long"].index(default_effort),
                key=f"effort_{task['id']}",
            )
        with cols[3]:
            energy = st.selectbox(
                "エネルギー", ["low", "mid", "high"], index=["low", "mid", "high"].index(default_energy),
                key=f"energy_{task['id']}",
            )
            
        col_date, col_status = st.columns(2)
        with col_date:
            new_due_date = st.date_input("期限", value=default_due_date, key=f"due_date_{task['id']}")
        with col_status:
            status = st.selectbox(
                "ステータス",
                ["inbox", "today", "week", "done"],
                index=["inbox", "today", "week", "done"].index(default_status),
                key=f"status_{task['id']}",
            )
            
        # tags = st.text_input("タグ（任意）", task.get("tags") or "", key=f"tags_{task['id']}") # Removed
        submit = st.form_submit_button("更新")
        if submit:
            update_item_fields(
                task["id"],
                tags=new_tags,
                raw_text=raw_text,
                due_date=new_due_date.isoformat() if new_due_date else None,
                importance=importance,
                urgency=urgency,
                effort=effort,
                energy=energy,
                status=status,
            )
            st.success("更新しました。")

    if task.get("recurrence_rule_id") and st.button("繰り返しを停止", key=f"stop_recurrence_{task['id']}"):
        stop_task_recurrence(task["id"])
        st.success("このタスク以降の繰り返しを停止しました。")
        st.rerun()

    # Delete Confirmation Logic
    confirm_key = f"confirm_delete_task_{task['id']}"
    
    if st.button("🗑️ 削除", key=f"delete_task_{task['id']}"):
        st.session_state[confirm_key] = True
        st.rerun()

    if st.session_state.get(confirm_key):
        st.warning("本当に削除しますか？")
        col_yes, col_no = st.columns(2)
        with col_yes:
            if st.button("はい", key=f"yes_del_task_{task['id']}"):
                delete_item(task["id"])
                del st.session_state[confirm_key]
                st.success("削除しました。")
                st.rerun()
        with col_no:
            if st.button("いいえ", key=f"no_del_task_{task['id']}"):
                del st.session_state[confirm_key]
                st.rerun()


def render_task_organizer_tab() -> None:
    st.subheader("🗂️ タスク整理")
    st.caption("キーワードや期限で絞り込み、必要なタスクだけを開いて編集できます。")
    filter_col, search_col, due_col = st.columns([1, 2, 1])
    with filter_col:
        filter_status = st.radio("状態", ["未完了", "完了"], horizontal=True)
    with search_col:
        query = st.text_input("検索", placeholder="タイトル・詳細・タグを検索")
    with due_col:
        due_filter = st.selectbox("期限", ["すべて", "期限切れ", "今日", "今週", "期限なし"])

    target_statuses = ["inbox", "today", "week"] if filter_status == "未完了" else ["done"]
    tasks = fetch_tasks(statuses=target_statuses, limit=200)
    today = dt.date.today()
    week_end = today + dt.timedelta(days=6)

    def matches_filter(task: Dict) -> bool:
        haystack = " ".join(
            str(task.get(field) or "") for field in ("tags", "raw_text", "ai_category")
        ).lower()
        if query.strip() and query.strip().lower() not in haystack:
            return False
        due_str = task.get("due_date")
        try:
            due = dt.date.fromisoformat(due_str) if due_str else None
        except ValueError:
            due = None
        if due_filter == "期限切れ":
            return bool(due and due < today)
        if due_filter == "今日":
            return due == today
        if due_filter == "今週":
            return bool(due and today <= due <= week_end)
        if due_filter == "期限なし":
            return due is None
        return True

    tasks = [task for task in tasks if matches_filter(task)]
    if not tasks:
        st.info("条件に合うタスクはありません。条件を変えるか、インボックスからタスクを追加してください。")
        return

    # Group tasks by deadline. ``date`` remains the immutable creation date.
    tasks_by_date: Dict[str, List[Dict]] = {}
    for task in tasks:
        d = task.get("due_date") or "期限なし"
        if d not in tasks_by_date:
            tasks_by_date[d] = []
        tasks_by_date[d].append(task)
    
    # Sort dates (Newest first, "期限なし" at end or beginning? User said "最新の日付が来て、下になるほど古くなる")
    # Assuming ISO format strings, reverse sort works. "期限なし" will be at the end if we handle it carefully.
    sorted_dates = sorted([d for d in tasks_by_date.keys() if d != "期限なし"], reverse=True)
    if "期限なし" in tasks_by_date:
        sorted_dates.append("期限なし")

    for d in sorted_dates:
        st.markdown(f"### {d}")
        date_tasks = tasks_by_date[d]
        sorted_tasks = sorted(date_tasks, key=_due_priority_tuple)
        for task in sorted_tasks:
            display_title = task.get("tags") or task.get("raw_text", "")[:20]
            with st.expander(f"[{task.get('status', 'inbox')}] {display_title}"):
                render_task_editor(task)


def _display_tasks(tasks: List[Dict], title: str) -> None:
    st.subheader(title)
    if not tasks:
        st.info("該当タスクはありません。")
        return
    sorted_tasks = sorted(tasks, key=_due_priority_tuple)
    today = dt.date.today()
    
    for idx, task in enumerate(sorted_tasks, start=1):
        # Overdue check
        due_str = task.get("due_date")
        is_overdue = False
        if due_str:
            try:
                due_date = dt.date.fromisoformat(due_str)
                if due_date < today:
                    is_overdue = True
            except ValueError:
                pass
        
        prefix = "🚨 " if is_overdue else ""
        display_title = task.get("tags") or task.get("raw_text", "")[:30]
        st.markdown(f"**{idx}.** {prefix}{display_title}")
        with st.expander("詳細"):
            st.write(task.get("raw_text", ""))
        
        due = due_str or "-"
        meta = (
            f"期限: {due} / "
            f"優先度: importance {task.get('importance') or '-'} × urgency {task.get('urgency') or '-'}"
        )
        if is_overdue:
            st.caption(f":red[{meta}]")
        else:
            st.caption(meta)
            
        btn_col1, btn_col2 = st.columns([1, 3])
        with btn_col1:
            if st.button("✔ 完了", key=f"done_btn_{task['id']}"):
                next_task_id = complete_task(task["id"])
                if next_task_id:
                    st.success("完了にし、次回分のタスクを作成しました。")
                else:
                    st.success("完了に更新しました。")
                st.rerun()  # Ensure this is called in a valid Streamlit context
        with btn_col2:
            st.caption(f"status: {task.get('status', '')} | tags: {task.get('tags') or '-'}")
        st.divider()


def render_today_tab() -> None:
    today_str = dt.date.today().isoformat()
    candidate_tasks = fetch_tasks(statuses=["today", "week", "inbox"], limit=300)
    overview = _task_overview(fetch_tasks(limit=500))
    metrics = st.columns(3)
    metrics[0].metric("今日取り組む", overview["today"])
    metrics[1].metric("期限切れ", overview["overdue"])
    metrics[2].metric("未完了", overview["active"])
    if overview["overdue"]:
        st.warning("期限切れのタスクがあります。まずは期限を見直すか、完了にしてください。")

    # AI Suggestion (using candidates from inbox/week/today)
    suggestions = suggest_today_tasks(candidate_tasks, dt.date.today())
    if suggestions:
        st.subheader("🤖 AI 推薦: 今日のトップ3（理由付き）")
        task_lookup = {task["id"]: task for task in candidate_tasks}
        for idx, suggestion in enumerate(suggestions, start=1):
            with st.container(border=True):
                st.markdown(f"**{idx}. {suggestion.raw_text}**")
                status = (task_lookup.get(suggestion.task_id, {}).get("status") or "inbox").lower()
                meta = f"理由: {suggestion.reason}"
                if suggestion.due:
                    meta += f" / 期限: {suggestion.due}"
                st.caption(meta)
                if status != "today":
                    if st.button("今日のタスクに追加", key=f"suggest_to_today_{suggestion.task_id}"):
                        update_item_fields(suggestion.task_id, status="today")
                        st.success("今日のタスクに移動しました。")
                        st.rerun()
        st.divider()

    # Today's Tasks (including overdue)
    today_tasks = fetch_tasks_for_today_view(today_str)
    
    # Split into "Explicitly Today" and "Due/Overdue but not Today status" if needed, 
    # or just show them all. The user wants them to appear in Today View.
    # Let's show them in one list, but maybe highlight if they are overdue.
    
    _display_tasks(today_tasks, "📅 今日のタスク (期限到来・期限切れ含む)")


def render_idea_tab() -> None:
    st.subheader("💡 アイデア整理 (NotebookLM風)")
    st.caption("資料やメモをアップロードして、AIとブレインストーミングしましょう。")

    # Layout: Left (Source) / Right (Brainstorming & Ideas)
    col_left, col_right = st.columns([1, 1])

    with col_left:
        # Session Management (Moved from Sidebar)
        with st.expander("📂 過去のセッションを開く", expanded=False):
            if st.button("➕ 新しいセッション", type="primary", key="new_session_btn"):
                keys_to_clear = ["idea_source_text", "idea_project_title", "idea_summary", "idea_generated_items", "idea_chat_history", "current_session_id"]
                for k in keys_to_clear:
                    if k in st.session_state:
                        del st.session_state[k]
                st.rerun()
            
            st.divider()
            sessions = fetch_idea_sessions()
            if not sessions:
                st.caption("保存されたセッションはありません。")
            else:
                for s in sessions:
                    col_s_name, col_s_del = st.columns([4, 1])
                    with col_s_name:
                        if st.button(f"{s['title']} ({s['updated_at'][:10]})", key=f"load_session_{s['id']}"):
                             # Load session
                            full_session = get_idea_session(s["id"])
                            if full_session:
                                st.session_state["current_session_id"] = full_session["id"]
                                st.session_state["idea_project_title"] = full_session["title"]
                                st.session_state["idea_summary"] = full_session["summary"]
                                st.session_state["idea_source_text"] = full_session["source_text"]
                                st.session_state["idea_chat_history"] = json.loads(full_session["chat_history"])
                                st.session_state["idea_generated_items"] = [] 
                                st.rerun()
                    with col_s_del:
                        if st.button("🗑️", key=f"del_session_{s['id']}"):
                            st.session_state[f"confirm_del_session_{s['id']}"] = True
                            st.rerun()
                    
                    if st.session_state.get(f"confirm_del_session_{s['id']}"):
                        st.warning("削除しますか？")
                        col_yes, col_no = st.columns(2)
                        with col_yes:
                            if st.button("はい", key=f"yes_del_session_{s['id']}"):
                                delete_idea_session(s["id"])
                                del st.session_state[f"confirm_del_session_{s['id']}"]
                                if st.session_state.get("current_session_id") == s["id"]:
                                    keys_to_clear = ["idea_source_text", "idea_project_title", "idea_summary", "idea_generated_items", "idea_chat_history", "current_session_id"]
                                    for k in keys_to_clear:
                                        if k in st.session_state:
                                            del st.session_state[k]
                                st.rerun()
                        with col_no:
                            if st.button("いいえ", key=f"no_del_session_{s['id']}"):
                                del st.session_state[f"confirm_del_session_{s['id']}"]
                                st.rerun()

        st.markdown("### 1. ソース資料")
        if "current_session_id" in st.session_state:
             st.info(f"**セッション:** {st.session_state.get('idea_project_title')}")
             st.write(f"**要約:** {st.session_state.get('idea_summary')}")
             with st.expander("ソーステキストを表示"):
                 st.text(st.session_state.get("idea_source_text")[:500] + "...")
        else:
            uploaded_file = st.file_uploader("資料をアップロード (PDF, DOCX, TXT, MD)", type=["pdf", "docx", "txt", "md"], key="idea_uploader")
            text_input = st.text_area("またはテキストを直接入力", height=200, key="idea_text_input")
            
            analyze_btn = st.button("分析開始", type="primary")
            
            if analyze_btn:
                source_text = ""
                if uploaded_file:
                    with st.spinner("ファイルを読み込み中..."):
                        file_content = uploaded_file.getvalue()
                        file_type = uploaded_file.name.split(".")[-1].lower()
                        source_text = extract_text_from_file(file_content, file_type)
                elif text_input:
                    source_text = text_input
                
                if source_text:
                    with st.spinner("AIが分析中..."):
                        result = analyze_source_and_generate_title(source_text)
                        
                        title = result.get("title", "無題のプロジェクト")
                        summary = result.get("summary", "")
                        initial_ideas = result.get("initial_ideas", [])
                        
                        # Create Session in DB
                        session_id = create_idea_session(title, summary, source_text)
                        
                        st.session_state["current_session_id"] = session_id
                        st.session_state["idea_source_text"] = source_text
                        st.session_state["idea_project_title"] = title
                        st.session_state["idea_summary"] = summary
                        st.session_state["idea_generated_items"] = [{"text": idea, "added": False} for idea in initial_ideas]
                        st.session_state["idea_chat_history"] = [] 
                        
                        # Generate Initial Mind Map
                        # Generate Initial Mind Map
                        st.write("マインドマップ生成中...")
                        try:
                            mindmap_data = generate_initial_mindmap(source_text)
                            st.write(f"データ受信: {len(mindmap_data.get('children', []))} items")
                            save_mindmap_tree(session_id, mindmap_data)
                            st.write("DB保存完了")
                        except Exception as e:
                            st.error(f"マインドマップ生成エラー: {e}")
                        
                    st.success("分析完了！セッションとマインドマップを保存しました。")
                    st.rerun()
                else:
                    st.warning("資料をアップロードするかテキストを入力してください。")

    with col_right:
        st.markdown("### 2. マインドマップ (アイデア整理)")
        
        if "current_session_id" not in st.session_state:
            st.info("左側のパネルで資料を分析するか、過去のセッションを選択してください。")
            return

        session_id = st.session_state["current_session_id"]
        nodes = get_nodes_by_project(session_id)
        
        if not nodes:
            st.info("マインドマップがまだありません。")
            if st.button("マインドマップを初期生成 (Manual Root)", key="init_mindmap_btn"):
                # Phase 0: Manual Root Creation
                root_title = st.session_state.get("idea_project_title", "New Idea Map")
                create_node(
                    project_id=session_id,
                    kind="root",
                    title=root_title,
                    body="Root node created manually."
                )
                st.rerun()
        else:
            # Render Tree
            _render_mindmap_tree(nodes)

def _render_mindmap_tree(nodes: List[MindmapNode], level: int = 0) -> None:
    """Recursively render mindmap nodes. 
    Uses Expanders for Level 1 (Ideas).
    For deeper levels, uses flat rows with visual indentation to avoid nested columns/expanders.
    """
    for node in nodes:
        # Icon based on kind
        icon = "📄"
        if node.kind == "root": icon = "🌳"
        elif node.kind == "idea": icon = "💡"
        elif node.kind == "task": icon = "✅"
        elif node.kind == "detail": icon = "📝"
        elif node.kind == "note": icon = "📌"
        elif node.kind == "link": icon = "🔗"
        
        label = f"{icon} {node.title}"
        
        if level == 0:
            # Root: Header
            st.markdown(f"### {label}")
            if node.body:
                st.caption(node.body)
            _render_node_actions(node)
            if node.children:
                _render_mindmap_tree(node.children, level + 1)
                
        elif level == 1:
            # Idea: Expander
            with st.expander(label, expanded=True):
                st.caption(f"ID: {node.id} | {node.kind}")
                if node.body:
                    st.write(node.body)
                _render_node_actions(node)
                
                if node.children:
                    _render_mindmap_tree(node.children, level + 1)
        else:
            # Deeper levels: Flat rows with indentation
            # We are already inside the Level 1 Expander here.
            # Do NOT create a new column wrapper for the whole node that contains children.
            # Just render the current node row, then recurse.
            
            indent_spaces = "&nbsp;" * ((level - 1) * 4)
            
            # Layout: [Title (indented)] [Actions]
            # We use columns for the row, but ensure recursion is OUTSIDE these columns.
            
            # Note: We need to render the title and actions.
            # To keep alignment, maybe we just use one markdown for title?
            # But we need buttons.
            
            # col_row = st.columns([3, 1]) # Unused, removing to avoid confusion
            # Actually, standard actions take up space. 
            # Let's use the standard action renderer but maybe compact it?
            # For now, let's just render the title with indentation in a full width, 
            # and actions below or to the right?
            # Let's try to keep the same action row style.
            
            st.markdown(f"{indent_spaces} **{label}**", unsafe_allow_html=True)
            if node.body:
                st.caption(f"{indent_spaces} {node.body}", unsafe_allow_html=True)
            
            # Render actions with indentation? 
            # It's hard to indent Streamlit widgets. 
            # We'll just render them normally. They will appear left-aligned in the expander.
            # To make it look associated, maybe we use a container? No, that nests.
            # Let's just render actions.
            _render_node_actions(node)
            
            if node.children:
                _render_mindmap_tree(node.children, level + 1)

def _render_node_actions(node: MindmapNode) -> None:
    """Helper to render action buttons for a node."""
    col_acts = st.columns(4)
    with col_acts[0]:
        if st.button("✨アイデア拡散", key=f"expand_idea_{node.id}"):
            with st.spinner("AIがアイデアを広げています..."):
                context = st.session_state.get("idea_summary", "")
                new_items = expand_node(node.title, node.body, context, "related_ideas")
                _save_expansion_items(node.project_id, node.id, new_items)
                st.rerun()
    with col_acts[1]:
        if st.button("✨タスク抽出", key=f"expand_task_{node.id}"):
            with st.spinner("AIがタスクを抽出しています..."):
                context = st.session_state.get("idea_summary", "")
                new_items = expand_node(node.title, node.body, context, "related_tasks")
                _save_expansion_items(node.project_id, node.id, new_items)
                st.rerun()
    with col_acts[2]:
        if node.kind == "task":
            if st.button("✨詳細化", key=f"expand_step_{node.id}"):
                with st.spinner("AIが手順を分解しています..."):
                    context = st.session_state.get("idea_summary", "")
                    new_items = expand_node(node.title, node.body, context, "detail_steps")
                    _save_expansion_items(node.project_id, node.id, new_items)
                    st.rerun()
            
            # Task Sync Button
            if node.linked_item_id:
                st.caption(f"✅ 連携済 (ID: {node.linked_item_id})")
            else:
                if st.button("📥 インボックスへ", key=f"sync_task_{node.id}"):
                    item_id = ensure_task_item_for_node(node)
                    st.success(f"タスクを作成しました (ID: {item_id})")
                    st.rerun()

    with col_acts[3]:
            if st.button("🗑️", key=f"del_node_{node.id}"):
                delete_node_and_descendants(node.id)
                st.rerun()

def _save_expansion_items(project_id: int, parent_id: int, items: List[Dict[str, Any]]) -> None:
    for item in items:
        create_node(
            project_id=project_id,
            kind=item.get("kind", "idea"),
            title=item.get("title", "No Title"),
            body=item.get("body", ""),
            parent_id=parent_id
        )

def render_doc_analysis_tab() -> None:
    st.subheader("📄 ドキュメント分析")
    uploaded_file = st.file_uploader("ファイルをアップロードしてください (PDF, DOCX, TXT, MD)", type=["pdf", "docx", "txt", "md"])

    if uploaded_file is not None:
        with st.spinner("ファイルを処理中..."):
            file_content = uploaded_file.getvalue()
            file_type = uploaded_file.name.split(".")[-1].lower()
            text = extract_text_from_file(file_content, file_type)
            
            if not text.strip():
                st.warning("PDFからテキストを抽出できませんでした。")
                return

            analysis_result = summarize_and_extract_tasks_from_text(text)
        
        st.subheader("要約")
        st.write(analysis_result.get("summary", "要約を生成できませんでした。"))

        st.subheader("抽出されたタスク")
        tasks = analysis_result.get("tasks", [])
        if not tasks:
            st.info("タスクは抽出されませんでした。")
        else:
            for i, task_text in enumerate(tasks):
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.write(task_text)
                with col2:
                    if st.button("タスクとして保存", key=f"add_task_{i}"):
                        insert_task(raw_text=task_text, tags="from_document")
                        st.success(f"タスク「{task_text[:30]}...」を保存しました。")


def main() -> None:
    """Streamlit で MVP のインボックス/タスク/今日/今週ビューを提供。"""
    st.set_page_config(page_title="SymNote", page_icon="🧠", layout="wide")
    init_db()
    # Never block local work on the network: failed delivery remains queued in
    # SQLite and is retried on a later app run.
    if google_calendar_connected():
        sync_pending_tasks()
    _apply_app_style()
    selected = render_sidebar()
    subtitles = {
        "Google Calendar": "期限付きタスクを同期し、Google Calendar の通知を利用します。",
        "今日": "いま取り組むことを、迷わず片付けるための一覧です。",
        "インボックス": "思いついたことを、まずはそのまま残します。",
        "タスク整理": "優先度と期限を整えて、次の行動を決めます。",
        "カレンダー": "期限と記録を、月単位で振り返ります。",
        "アイデア整理": "資料やメモから、考えを行動に変えます。",
        "ドキュメント分析": "ファイルから要点とタスク候補を取り出します。",
    }
    st.markdown(f"<div class='symnote-kicker'>SymNote / {selected}</div>", unsafe_allow_html=True)
    st.markdown(f"<h1 class='symnote-page-title'>{selected}</h1>", unsafe_allow_html=True)
    st.caption(subtitles[selected])

    if selected == "Google Calendar":
        render_google_calendar_tab()
    elif selected == "インボックス":
        render_inbox_tab()
    elif selected == "タスク整理":
        render_task_organizer_tab()
    elif selected == "今日":
        render_today_tab()
    elif selected == "カレンダー":
        render_calendar_tab()
    elif selected == "アイデア整理":
        render_idea_tab()
    else:
        render_doc_analysis_tab()


if __name__ == "__main__":
    main()
