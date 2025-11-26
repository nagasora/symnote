from __future__ import annotations

from typing import Dict, List, Tuple
import datetime as dt

import streamlit as st

from symnote.core.db import (
    ClassificationSummary,
    fetch_inbox,
    fetch_tasks,
    init_db,
    insert_memo,
    classify_inbox_items,
    update_item_fields,
    insert_weekly_review,
)
from symnote.core.nlp import (
    classify_text_rule_based,
    priority_score,
    Effort,
    Energy,
    suggest_today_tasks,
    summarize_and_extract_tasks_from_text,
)
from symnote.core.weekly_review import generate_weekly_review
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
    with st.form("inbox_form"):
        raw_text = st.text_area(
            "頭の中にあることをなんでも書き込んでください",
            height=150,
            placeholder="例: 来週のゼミ発表の準備… / 論文Xを読む / バイトのシフト調整...",
        )
        tags = st.text_input("タグ（カンマ区切り・任意）", "")
        submitted = st.form_submit_button("インボックスに保存")
        if submitted:
            if not raw_text.strip():
                st.warning("メモ本文を入力してください。")
            else:
                note_id = insert_memo(raw_text=raw_text, tags=tags)
                st.success(f"保存しました (ID: {note_id})")

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

    sorted_tasks = sorted(tasks, key=_due_priority_tuple)
    for task in sorted_tasks:
        with st.expander(f"[{task.get('status', 'inbox')}] {task.get('raw_text', '')[:40]}..."):
            render_task_editor(task)


def _display_tasks(tasks: List[Dict], title: str) -> None:
    st.subheader(title)
    if not tasks:
        st.info("該当タスクはありません。")
        return
    sorted_tasks = sorted(tasks, key=_due_priority_tuple)
    for idx, task in enumerate(sorted_tasks, start=1):
        st.markdown(f"**{idx}.** {task.get('raw_text', '')}")
        due = task.get("date") or "-"
        meta = (
            f"期限: {due} / "
            f"優先度: importance {task.get('importance') or '-'} × urgency {task.get('urgency') or '-'}"
        )
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


def render_week_tab() -> None:
    tasks = fetch_tasks(statuses=["week"], limit=200)
    ordered = sorted(tasks, key=_due_priority_tuple)
    _display_tasks(ordered, "🌤 今週のタスク")

    st.subheader("📘 AI 週次振り返り")
    payload = generate_weekly_review()
    st.write(f"期間: {payload.period_start} - {payload.period_end}")
    st.write(payload.summary)
    cols = st.columns(3)
    with cols[0]:
        st.markdown("**👍 良かった点**")
        for point in payload.good_points:
            st.write(f"- {point}")
    with cols[1]:
        st.markdown("**🧠 学び・改善点**")
        for point in payload.learnings:
            st.write(f"- {point}")
    with cols[2]:
        st.markdown("**🎯 来週のフォーカス**")
        for point in payload.focus_next:
            st.write(f"- {point}")
    if st.button("AI レポートを保存する"):
        review_id = insert_weekly_review(payload)
        st.success(f"週次レポート (ID: {review_id}) を保存しました。")


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

    tabs = ["インボックス", "タスク整理", "今日ビュー", "今週ビュー", "カレンダー", "ドキュメント分析"]
    tab_inbox, tab_tasks, tab_today, tab_week, tab_calendar, tab_doc_analysis = st.tabs(tabs)

    with tab_inbox:
        render_inbox_tab()
    with tab_tasks:
        render_task_organizer_tab()
    with tab_today:
        render_today_tab()
    with tab_week:
        render_week_tab()
    with tab_calendar:
        render_calendar_tab()
    with tab_doc_analysis:
        render_doc_analysis_tab()


if __name__ == "__main__":
    main()

