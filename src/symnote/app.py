from __future__ import annotations

from typing import Dict, List, Tuple
import datetime as dt

import streamlit as st

from symnote.core.db import (
    ClassificationSummary,
    classify_inbox_items,
    fetch_inbox,
    fetch_tasks,
    init_db,
    insert_memo,
    insert_task,
    update_item_fields,
)
from symnote.core.nlp import (
    classify_text_rule_based,
    priority_score,
    Effort,
    Energy,
    suggest_today_tasks,
    generate_todos_from_idea,
    summarize_and_extract_tasks_from_text,
)
from symnote.calendar_app import render_calendar_tab
from symnote.core.doc_loader import extract_text_from_pdf


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
        raw_text = st.text_area(
            "頭の中にあることをなんでも書き込んでください",
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

        tags = st.text_input("タグ（カンマ区切り・任意）", "")
        
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
        with st.expander(f"ID {item['id']} | {item['date']} | {item.get('ai_category') or '未分類'}"):
            st.write(item["raw_text"])
            if item.get("tags"):
                st.caption(f"タグ: {item['tags']}")


def render_task_editor(task: Dict) -> None:
    default_importance = task.get("importance") or 3
    default_urgency = task.get("urgency") or 3
    default_effort = task.get("effort") or "medium"
    default_energy = task.get("energy") or "mid"
    default_status = task.get("status") or "inbox"

    st.markdown(f"**ID {task['id']}** | {task.get('ai_category', 'task')} | {task.get('date', '')}")
    st.write(task.get("raw_text", ""))
    with st.form(f"task_form_{task['id']}"):
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
        status = st.selectbox(
            "ステータス",
            ["inbox", "today", "week", "done"],
            index=["inbox", "today", "week", "done"].index(default_status),
            key=f"status_{task['id']}",
        )
        tags = st.text_input("タグ（任意）", task.get("tags") or "", key=f"tags_{task['id']}")
        submit = st.form_submit_button("更新")
        if submit:
            update_item_fields(
                task["id"],
                importance=importance,
                urgency=urgency,
                effort=effort,
                energy=energy,
                status=status,
                tags=tags,
            )
            st.success("更新しました。")


def render_task_organizer_tab() -> None:
    st.subheader("🗂️ タスク整理")
    tasks = fetch_tasks(statuses=None, limit=200)
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
            with st.expander(f"[{task.get('status', 'inbox')}] {task.get('raw_text', '')[:40]}..."):
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
        st.markdown(f"**{idx}.** {prefix}{task.get('raw_text', '')}")
        
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

    today_tasks = [task for task in candidate_tasks if (task.get("status") or "").lower() == "today"]
    ordered = sorted(today_tasks, key=_due_priority_tuple)
    top3 = ordered[:3]
    others = ordered[3:]
    _display_tasks(top3, "📅 今日のトップ3")
    if others:
        _display_tasks(others, "その他の今日タスク")


def render_idea_tab() -> None:
    st.subheader("💡 アイデア整理")
    st.caption("実現したいことやアイデアを入力すると、AIがToDoリストを生成します。")
    
    idea_text = st.text_area("アイデア・実現したいこと", height=150)
    
    if st.button("ToDoを生成する", type="primary"):
        if not idea_text.strip():
            st.warning("アイデアを入力してください。")
            return
            
        with st.spinner("AIが考え中..."):
            todos = generate_todos_from_idea(idea_text)
            
        if not todos:
            st.error("ToDoの生成に失敗しました。")
            return
            
        st.session_state["generated_todos"] = todos
        st.success("ToDoが生成されました！")

    if "generated_todos" in st.session_state:
        st.subheader("生成されたToDo")
        todos = st.session_state["generated_todos"]
        
        for i, todo in enumerate(todos):
            col1, col2 = st.columns([4, 1])
            with col1:
                st.write(f"- {todo}")
            with col2:
                if st.button("追加", key=f"add_todo_{i}"):
                    insert_memo(raw_text=todo, tags="from_idea")
                    st.toast(f"「{todo[:20]}...」を追加しました")


def render_doc_analysis_tab() -> None:
    st.subheader("📄 ドキュメント分析")
    uploaded_file = st.file_uploader("PDFファイルをアップロードしてください", type="pdf")

    if uploaded_file is not None:
        with st.spinner("ファイルを処理中..."):
            file_content = uploaded_file.getvalue()
            text = extract_text_from_pdf(file_content)
            
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
