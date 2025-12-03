from __future__ import annotations

from typing import Dict, List, Tuple
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
)
from symnote.calendar_app import render_calendar_tab
from symnote.core.doc_loader import extract_text_from_file


def _due_priority_tuple(task: Dict) -> Tuple[dt.date, float, int]:
    """Sort key: due date (date列) が近い順 → 優先度スコア → ID。"""
    date_str = task.get("date")
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
                # タスクとして保存 (insert_taskを使用)
                # insert_taskは date_str を受け取るが、これは実行日/期限として扱われることが多い
                note_id = insert_task(raw_text=raw_text, tags=tags, date_str=due_date_str)
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
    default_date_str = task.get("date")
    default_date = None
    if default_date_str:
        try:
            default_date = dt.date.fromisoformat(default_date_str)
        except ValueError:
            pass

    st.markdown(f"**ID {task['id']}** | {task.get('ai_category', 'task')}")
    
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
            new_date = st.date_input("期限", value=default_date, key=f"date_{task['id']}")
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
                date=new_date.isoformat() if new_date else None,
                importance=importance,
                urgency=urgency,
                effort=effort,
                energy=energy,
                status=status,
            )
            st.success("更新しました。")

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
    
    filter_status = st.radio(
        "表示フィルタ",
        ["未完了", "完了"],
        horizontal=True,
    )
    
    target_statuses = ["inbox", "today", "week"] if filter_status == "未完了" else ["done"]
    tasks = fetch_tasks(statuses=target_statuses, limit=200)
    if not tasks:
        st.info("AI でメモをタスクに分類するとここに表示されます。")
        return

    # Group by date
    tasks_by_date: Dict[str, List[Dict]] = {}
    for task in tasks:
        d = task.get("date") or "期限なし"
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
        due_str = task.get("date")
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
                update_item_fields(task["id"], status="done")
                st.success("完了に更新しました。")
                st.rerun()  # Ensure this is called in a valid Streamlit context
        with btn_col2:
            st.caption(f"status: {task.get('status', '')} | tags: {task.get('tags') or '-'}")
        st.divider()


def render_today_tab() -> None:
    today_str = dt.date.today().isoformat()
    
    # AI Suggestion (using candidates from inbox/week/today)
    candidate_tasks = fetch_tasks(statuses=["today", "week", "inbox"], limit=300)
    suggestions = suggest_today_tasks(candidate_tasks, dt.date.today())
    if suggestions:
        st.subheader("🤖 AI 推薦: 今日のトップ3（理由付き）")
        task_lookup = {task["id"]: task for task in candidate_tasks}
        for idx, suggestion in enumerate(suggestions, start=1):
            st.markdown(f"**{idx}.** {suggestion.raw_text}")
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
                        
                    st.success("分析完了！セッションを保存しました。")
                    st.rerun()
                else:
                    st.warning("資料をアップロードするかテキストを入力してください。")

    with col_right:
        st.markdown("### 2. ブレインストーミング & タスク化")
        
        if "idea_source_text" not in st.session_state:
            st.info("左側のパネルで資料を分析するか、過去のセッションを選択してください。")
        else:
            # Chat History Display
            chat_history = st.session_state.get("idea_chat_history", [])
            for msg in chat_history:
                with st.chat_message(msg["role"]):
                    st.write(msg["content"])

            # Chat Interface
            user_query = st.chat_input("AIに質問・アイデア出しを依頼")
            if user_query:
                with st.chat_message("user"):
                    st.write(user_query)
                
                with st.spinner("AIが考え中..."):
                    new_ideas = brainstorm_ideas(st.session_state["idea_source_text"], user_query)
                    
                    ai_response = f"{len(new_ideas)} 個のアイデアを生成しました。\n\n" + "\n".join([f"- {idea}" for idea in new_ideas])
                    
                    with st.chat_message("ai"):
                        st.write(ai_response)
                    
                    # Update State
                    st.session_state["idea_chat_history"].append({"role": "user", "content": user_query})
                    st.session_state["idea_chat_history"].append({"role": "ai", "content": ai_response})
                    
                    # Add new ideas to generated items list
                    if "idea_generated_items" not in st.session_state:
                        st.session_state["idea_generated_items"] = []
                    for idea in new_ideas:
                        st.session_state["idea_generated_items"].append({"text": idea, "added": False})
                    
                    # Update DB
                    if "current_session_id" in st.session_state:
                         update_idea_session_chat(st.session_state["current_session_id"], json.dumps(st.session_state["idea_chat_history"], ensure_ascii=False))

            # Display Ideas for Adding
            st.divider()
            st.markdown("#### 生成されたアイデア (タスク化)")
            if "idea_generated_items" in st.session_state:
                items = st.session_state["idea_generated_items"]
                # Use a copy to allow modification during iteration if needed, though we use indices
                for i, item in enumerate(items):
                    col_text, col_btn, col_del = st.columns([6, 2, 1])
                    with col_text:
                        st.write(f"- {item['text']}")
                    with col_btn:
                        if not item["added"]:
                            if st.button("追加", key=f"add_idea_{i}"):
                                title = st.session_state["idea_project_title"]
                                # Add to inbox with title as tag
                                insert_task(
                                    raw_text=f"[{title}] {item['text']}",
                                    tags=title,
                                    status="inbox",
                                )
                                item["added"] = True
                                st.toast(f"インボックスに追加しました")
                                st.rerun()
                        else:
                            st.caption("追加済")
                    with col_del:
                         if st.button("×", key=f"dismiss_idea_{i}"):
                             st.session_state[f"confirm_dismiss_idea_{i}"] = True
                             st.rerun()
                         
                         if st.session_state.get(f"confirm_dismiss_idea_{i}"):
                             st.warning("削除？")
                             if st.button("はい", key=f"yes_dismiss_{i}"):
                                 items.pop(i)
                                 del st.session_state[f"confirm_dismiss_idea_{i}"]
                                 st.rerun()
                             if st.button("いいえ", key=f"no_dismiss_{i}"):
                                 del st.session_state[f"confirm_dismiss_idea_{i}"]
                                 st.rerun()


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
                    if st.button("インボックスに追加", key=f"add_task_{i}"):
                        insert_memo(raw_text=task_text, tags="from_pdf")
                        st.success(f"タスク「{task_text[:30]}...」をインボックスに追加しました。")


def main() -> None:
    """Streamlit で MVP のインボックス/タスク/今日/今週ビューを提供。"""
    init_db()
    st.set_page_config(page_title="SymNote", page_icon="🧠", layout="wide")
    st.title("SymNote 🧠")
    st.caption("思考インボックスとタスク優先づけのための入り口")

    tabs = ["インボックス", "タスク整理", "今日ビュー", "カレンダー", "アイデア整理", "ドキュメント分析"]
    tab_inbox, tab_tasks, tab_today, tab_calendar, tab_idea, tab_doc_analysis = st.tabs(tabs)

    with tab_inbox:
        render_inbox_tab()
    with tab_tasks:
        render_task_organizer_tab()
    with tab_today:
        render_today_tab()
    with tab_calendar:
        render_calendar_tab()
    with tab_idea:
        render_idea_tab()
    with tab_doc_analysis:
        render_doc_analysis_tab()


if __name__ == "__main__":
    main()
