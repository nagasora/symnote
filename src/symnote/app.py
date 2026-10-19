from __future__ import annotations

from typing import Any, Dict, List, Tuple
import datetime as dt
import hashlib

import streamlit as st

from symnote.core.db import (
    fetch_memos,
    fetch_tasks,
    fetch_tasks_for_today_view,
    init_db,
    insert_memo,
    insert_task,
    update_item_fields,
    delete_item,
    complete_task,
    create_recurring_task,
    recurrence_label,
    stop_task_recurrence,
)
from symnote.core.nlp import (
    parse_quick_recurrence,
    priority_score,
    Effort,
    Energy,
    suggest_today_tasks,
    summarize_and_extract_tasks_from_text,
)
from symnote.core.recurrence import RecurrenceRule, WEEKDAY_LABELS, first_occurrence, rule_label
from symnote.core.task_ai import ParsedTask, parse_quick_task
from symnote.views.today_ai_view import QUICK_PREVIEW_KEY, _describe, save_parsed_task
from symnote.calendar_app import render_calendar_tab
from symnote.core.candidates import count_pending_candidates
from symnote.views.candidates_view import render_candidates_tab
from symnote.views.ideas_view import (
    _mindmap_nodes_with_positions as _mindmap_nodes_with_positions,
    _parse_mindmap_event as _parse_mindmap_event,
    render_idea_tab,
)
from symnote.views.text import escape_markdown
from symnote.config import load_config
from symnote.core.doc_loader import extract_text_from_file
from symnote.core.google_calendar import (
    is_connected as google_calendar_connected,
    sync_pending_tasks,
)
from symnote.views.agenda_view import render_today_agenda
from symnote.views.calendar_settings_view import render_google_calendar_settings
from symnote.core.calendar_settings import get_setting
from symnote.core.calendar_task_export import DEFAULT_EXPORT_TASKS
from symnote.core.quest import pop_unseen_rewards
from symnote.core.runtime import (
    PASSCODE_LIMITER,
    app_passcode,
    is_public_bind,
    is_test_mode,
    passcode_source,
)
from symnote.views.connections_view import render_connections_settings
from symnote.views.delegation_view import pending_report_count, render_agent_reports, render_delegate_button
from symnote.views.focus_view import render_focus_launcher, render_focus_mode
from symnote.views.goals_view import render_goals_page
from symnote.views.import_view import render_auto_import
from symnote.views.usage_view import render_ai_usage
from symnote.views.timeblock_view import render_timeblock
from symnote.views.quest_view import render_celebration, render_player_hud, render_rewards_page
from symnote.views.secretary_view import (
    render_brain_dump,
    render_secretary_panel,
    render_secretary_settings,
)
from symnote.views.today_ai_view import (
    render_ai_plan,
    render_breakdown_button,
    today_events_for_plan,
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
    """落ち着いた配色に、クエストらしい手触り（角丸カード・完了ボタン）を足す。"""
    st.markdown(
        """
        <style>
          .block-container { max-width: 1240px; padding-top: 3.6rem; padding-bottom: 3rem; }
          [data-testid="stSidebar"] { border-right: 1px solid rgba(109, 91, 208, .15); }
          [data-testid="stSidebar"] [role="radiogroup"] label { padding: .28rem .2rem; border-radius: 10px; }
          [data-testid="stSidebar"] [role="radiogroup"] label:hover { background: rgba(109, 91, 208, .08); }
          [data-testid="stVerticalBlockBorderWrapper"] { border-radius: 14px; }
          [data-testid="stMetric"] { padding: .55rem .15rem; }
          .symnote-page-title { margin: 0 0 .15rem; font-size: 1.75rem; letter-spacing: .01em; }
          div[data-testid="stButton"] button[kind="secondary"]:has(p:only-child) { border-radius: 10px; }
          button[title="完了にする"], div[data-testid="stButton"] button p { font-weight: 600; }
          .symnote-test-banner { background: #FFF6DB; border: 1px solid #F1D98A; color: #6B5310;
            border-radius: 10px; padding: .35rem .75rem; font-size: .85rem; margin-bottom: .6rem; }
          [data-testid="stElementContainer"]:has(.symnote-mobile-nav-marker) { display: none; }
          @media (min-width: 641px) {
            [data-testid="stElementContainer"]:has(.symnote-mobile-nav-marker) + [data-testid="stElementContainer"] {
              display: none;
            }
          }
          @media (max-width: 640px) {
            .block-container { padding: 3.4rem .9rem 4rem; }
            h1.symnote-page-title { font-size: 1.35rem !important; padding: .2rem 0 !important; }
            svg[aria-label="秘書のアバター"] { width: 56px; height: 56px; }
            div[data-testid="stButton"] button { min-height: 2.75rem; }
            input, textarea { font-size: 16px !important; }
          }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _require_passcode() -> bool:
    """パスコードが設定されていれば、このセッションで一度だけ入力を求める。通過済みなら True。"""
    expected = app_passcode()
    if not expected:
        if is_public_bind(st.get_option("server.address")):
            st.error(
                "この画面はネットワークに公開されていますが、パスコードが未設定です。"
                "データを守るため表示を止めました。SYMNOTE_APP_PASSCODE を設定して起動し直してください。"
            )
            return False
        return True
    if st.session_state.get("passcode_ok"):
        return True
    st.markdown("<h1 class='symnote-page-title'>🔒 SymNote</h1>", unsafe_allow_html=True)
    source = passcode_source(st.context.headers or {})
    wait = PASSCODE_LIMITER.locked_for(source=source)
    if wait:
        st.error(f"パスコードの入力を続けて間違えたため、{int(wait // 60) + 1} 分ほど入力を止めています。")
        return False
    with st.form("passcode_form"):
        entered = st.text_input("パスコード", type="password", key="passcode_input")
        submitted = st.form_submit_button("開く", type="primary")
    if submitted:
        if PASSCODE_LIMITER.attempt(entered, expected, source=source):
            st.session_state["passcode_ok"] = True
            st.rerun()
        st.error("パスコードが違います。")
    return False


def _render_test_banner() -> None:
    """テスト環境であることと、本番データに影響しない範囲を示す。"""
    st.markdown(
        "<div class='symnote-test-banner'>🧪 テスト環境：本番データのコピーです。"
        "変更は本番に反映されず、カレンダーへも書き込みません。</div>",
        unsafe_allow_html=True,
    )


# 内部キー（既存の画面遷移・テストが使う）→ サイドバーの表示名。
PAGES: Dict[str, str] = {
    "今日": "🏠 ホーム",
    "ゴール": "🎯 ゴール",
    "タスク": "✅ タスク",
    "候補の承認": "📥 受信箱",
    "アイデア整理": "🧠 マインドマップ",
    "カレンダー": "🗓️ カレンダー",
    "報酬": "🏆 報酬・実績",
    "設定": "⚙️ 設定",
}
PAGE_SUBTITLES: Dict[str, str] = {
    "今日": "秘書がいまやるべき一つを示します。片付けるたびにクエストクリア。",
    "ゴール": "すべての ToDo を「なぜやるか」につなげ、並行する仕事の進み具合をひと目で。",
    "タスク": "期限と時刻を決めて、優先度や繰り返しを整理します。",
    "候補の承認": "AI が見つけた ToDo と、頭の中から書き出したことを、確認してから登録します。",
    "アイデア整理": "テーマを1つ書くと AI がマインドマップを作ります。＋で枝を足し（AI か手入力）、−で畳み、ノードをタスクにできます。",
    "カレンダー": "予定と期限を、月単位で見渡します。",
    "報酬": "貯めたゴールドを、自分へのご褒美と交換しましょう。",
    "設定": "秘書キャラクター、Google Calendar、ChatGPT などの AI 連携を設定します。",
}


@st.dialog("🎉 クエストクリア！", width="large")
def _show_celebration(rewards: List[Any]) -> None:
    """どこまでスクロールしていても見えるよう、完了演出はモーダルで出す。"""
    render_celebration(rewards)


NAV_KEY = "nav_page"
MOBILE_NAV_KEY = "nav_page_mobile"


def _copy_nav(source: str, target: str) -> None:
    """サイドバーとスマホ用の画面切替を、どちらで選んでも同じ画面にそろえる。"""
    st.session_state[target] = st.session_state[source]


def _go_to_page(page: str) -> None:
    """ボタンから別の画面へ移る（サイドバーとスマホ用の切替の両方をそろえる）。"""
    st.session_state[NAV_KEY] = page
    st.session_state[MOBILE_NAV_KEY] = page


def render_mobile_nav() -> None:
    """スマホ幅でだけ見える画面切替。サイドバーを開かずにページを移れる。"""
    st.markdown("<span class='symnote-mobile-nav-marker'></span>", unsafe_allow_html=True)
    st.selectbox(
        "画面",
        list(PAGES),
        format_func=PAGES.get,
        label_visibility="collapsed",
        key=MOBILE_NAV_KEY,
        on_change=_copy_nav,
        args=(MOBILE_NAV_KEY, NAV_KEY),
    )


def render_sidebar() -> str:
    """Render the persistent navigation and a compact personal task snapshot."""
    all_tasks = fetch_tasks(limit=500)
    overview = _task_overview(all_tasks)
    pages = PAGES
    # 件数を表示名に入れると、件数が変わるたびに Streamlit が別ウィジェットとみなして
    # 選択中の画面が「今日」に戻ってしまうため、表示名は固定にして件数は別に出す。
    pending_candidates = count_pending_candidates()
    with st.sidebar:
        st.title("SymNote 🧠")
        st.caption("覚える・選ぶ・思い出すは任せて、やることだけに集中。")
        render_player_hud()
        st.caption(
            f"今日 {overview['today']} ・ 期限切れ {overview['overdue']} ・ 未完了 {overview['active']}"
        )
        selected = st.radio(
            "画面",
            list(pages),
            format_func=pages.get,
            label_visibility="collapsed",
            key=NAV_KEY,
            on_change=_copy_nav,
            args=(NAV_KEY, MOBILE_NAV_KEY),
        )
        st.divider()
        if pending_candidates:
            st.info(f"📥 承認待ちの候補が {pending_candidates} 件あります。")
        st.divider()
        st.caption("データはこの端末の SQLite に保存されています。")
        if not load_config().llm_api_key:
            st.info("AI機能はAPIキー未設定でも、メモ・タスク管理はそのまま使えます。")
    return selected


def _render_recurrence_inputs(
    key: str,
    task: Dict[str, Any] | None = None,
    start: dt.date | None = None,
) -> Tuple[RecurrenceRule | None, str | None]:
    """フォーム内の再描画に依存しない共通の繰り返し設定欄を表示する。"""
    task = task or {}
    start = start or dt.date.today()
    interval = task.get("recurrence_interval_days") or 1
    frequency = task.get("recurrence_frequency")
    selected = {
        "daily": "毎日",
        "weekdays": "平日",
        "weekly": "隔週" if interval == 14 else "毎週",
        "monthly": "毎月",
        "custom": "N日ごと",
    }.get(frequency, "なし")
    if frequency == "daily" and interval != 1:
        selected = "N日ごと"
    if task.get("recurrence_active") == 0:
        selected = "なし"
    options = ["なし", "毎日", "平日", "毎週", "隔週", "毎月", "N日ごと"]
    choice = st.selectbox(
        "繰り返し", options, index=options.index(selected), key=f"{key}_frequency"
    )
    days = st.multiselect(
        "曜日（毎週・隔週、空欄は期限日の曜日）",
        WEEKDAY_LABELS,
        default=[
            WEEKDAY_LABELS[int(day)]
            for day in (task.get("recurrence_weekdays") or "").split(",")
            if day
        ],
        key=f"{key}_weekdays",
    )
    month_day = int(
        st.number_input(
            "毎月の日付",
            min_value=1,
            max_value=31,
            value=task.get("recurrence_month_day") or start.day,
            key=f"{key}_month_day",
        )
    )
    custom_days = int(
        st.number_input("間隔（日、N日ごと）", min_value=1, value=interval, key=f"{key}_interval")
    )
    end = st.date_input(
        "繰り返し終了日（空欄で無期限）",
        value=dt.date.fromisoformat(task["recurrence_end_date"])
        if task.get("recurrence_end_date")
        else None,
        key=f"{key}_end",
    )
    st.caption("曜日・日付・間隔は、選んだ頻度の欄だけが適用されます。短い月は月末になります。")
    rule = None
    if choice != "なし":
        frequency = {
            "毎日": "daily",
            "平日": "weekdays",
            "毎週": "weekly",
            "隔週": "weekly",
            "毎月": "monthly",
            "N日ごと": "custom",
        }[choice]
        interval_days = (
            custom_days
            if frequency == "custom"
            else 14
            if choice == "隔週"
            else 7
            if frequency == "weekly"
            else 1
        )
        rule = RecurrenceRule(
            frequency,
            interval_days,
            tuple(WEEKDAY_LABELS.index(day) for day in days) if frequency == "weekly" else (),
            month_day if frequency == "monthly" else None,
        )
    return rule, end.isoformat() if end else None


def save_quick_recurring_task(
    text: str, *, use_ai: bool = False
) -> Tuple[int, ParsedTask, str | None]:
    """繰り返しを先に除去し、既存の日時解析を利用してクイック追加を保存する。"""
    from dataclasses import replace

    rule, remaining = parse_quick_recurrence(text)
    parsed = parse_quick_task(remaining, use_ai=use_ai)
    if rule is None:
        return save_parsed_task(parsed), parsed, None
    due = first_occurrence(
        dt.date.fromisoformat(parsed.due_date) if parsed.due_date else dt.date.today(), rule
    )
    parsed = replace(parsed, due_date=due.isoformat())
    task_id = create_recurring_task(
        raw_text=parsed.details or parsed.title,
        tags=parsed.title,
        due_date=parsed.due_date,
        due_time=parsed.due_time,
        frequency=rule.frequency,
        interval=rule.interval_days // 7 if rule.frequency == "weekly" else rule.interval_days,
        weekdays=rule.weekdays,
        month_day=rule.month_day,
    )
    update_item_fields(
        task_id, importance=parsed.importance, urgency=4 if parsed.importance >= 5 else 3
    )
    return task_id, parsed, rule_label(rule)


def _calendar_hint(has_date: bool) -> str:
    """追加した ToDo が Google カレンダーに載るかどうかを一言で伝える。"""
    try:
        connected = google_calendar_connected()
    except Exception:
        connected = False
    if not connected:
        return "Google カレンダーと連携すると、期限のある ToDo が自動で予定に入ります（設定 → Google Calendar）。"
    if get_setting("export_tasks", DEFAULT_EXPORT_TASKS) != "1":
        return "カレンダーへの自動登録はオフです（設定 → Google Calendar）。"
    if not has_date:
        return "日付がないため、カレンダーには登録しません（例:「明日」「金曜15時」と書くと登録されます）。"
    return "📅 Google カレンダーにも自動で登録します。"


def render_quick_add() -> None:
    """ホームの自然文追加にオフライン対応の繰り返し設定を提供する。"""
    use_ai = bool(load_config().llm_api_key)
    with st.form("quick_add_form", clear_on_submit=True):
        text = st.text_input(
            "クイック追加",
            placeholder="例: 毎日15時に日報 / 毎週月曜 会議 / 毎月15日 請求書",
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button("＋ 追加", type="primary")
    if submitted and text.strip():
        try:
            with st.spinner("読み取り中…" if use_ai else None):
                task_id, parsed, cadence = save_quick_recurring_task(text, use_ai=use_ai)
            st.session_state[QUICK_PREVIEW_KEY] = (task_id, parsed)
            st.session_state["quick_add_recurrence"] = cadence
        except ValueError as exc:
            st.warning(str(exc))
    preview = st.session_state.get(QUICK_PREVIEW_KEY)
    if preview:
        task_id, parsed = preview
        source = "AI" if parsed.source == "ai" else "ルール"
        cadence = st.session_state.get("quick_add_recurrence")
        st.success(
            f"追加しました（{source}で読み取り）: {_describe(parsed)}"
            + (f" ・ {cadence}" if cadence else "")
        )
        st.caption(_calendar_hint(bool(parsed.due_date or cadence)))
        if st.button("取り消す", key=f"undo_quick_{task_id}"):
            delete_item(task_id)
            st.session_state.pop(QUICK_PREVIEW_KEY, None)
            st.session_state.pop("quick_add_recurrence", None)
            st.rerun()
    st.caption(
        "日時・重要度・繰り返しは文章から読み取ります。繰り返しはオフラインでも利用できます。"
    )


def render_task_tab() -> None:
    """Create and organize tasks in one place."""
    st.subheader("📝 タスクを追加")
    with st.form("task_capture_form", clear_on_submit=True):
        title = st.text_input("タイトル", placeholder="タスクの概要")
        details = st.text_area("詳細", height=150, placeholder="例: 明日のゼミ資料を確認する")
        date_column, time_column = st.columns(2)
        with date_column:
            due_date = st.date_input("期限日", value=dt.date.today())
        with time_column:
            due_time = st.time_input(
                "期限時刻", value=dt.time(hour=load_config().google_calendar_deadline_hour)
            )
        rule, end_date = _render_recurrence_inputs("task_add", start=due_date)
        submitted = st.form_submit_button("タスクを保存", type="primary")
        if submitted:
            if not details.strip():
                st.warning("タスクの詳細を入力してください。")
            else:
                try:
                    if rule:
                        task_id = create_recurring_task(
                            raw_text=details,
                            tags=title,
                            due_date=due_date.isoformat(),
                            due_time=due_time.strftime("%H:%M"),
                            frequency=rule.frequency,
                            interval=rule.interval_days // 7
                            if rule.frequency == "weekly"
                            else rule.interval_days,
                            weekdays=rule.weekdays,
                            month_day=rule.month_day,
                            end_date=end_date,
                        )
                    else:
                        task_id = insert_task(
                            raw_text=details,
                            tags=title,
                            due_date=due_date.isoformat(),
                            due_time=due_time.strftime("%H:%M"),
                        )
                    st.success(f"タスクを保存しました (ID: {task_id})")
                except ValueError as exc:
                    st.warning(str(exc))

    st.divider()
    render_task_organizer_tab()


def render_memo_tab() -> None:
    """Create and browse notes separately from tasks."""
    st.subheader("📝 メモを追加")
    with st.form("memo_form", clear_on_submit=True):
        title = st.text_input("タイトル", placeholder="メモの題名")
        content = st.text_area("内容", height=180, placeholder="気づき、アイデア、記録など")
        submitted = st.form_submit_button("メモを保存", type="primary")
        if submitted:
            if not content.strip():
                st.warning("メモの内容を入力してください。")
            else:
                memo_id = insert_memo(raw_text=content, tags=title)
                st.success(f"メモを保存しました (ID: {memo_id})")

    st.divider()
    st.subheader("最近のメモ")
    memos = fetch_memos(limit=100)
    if not memos:
        st.info("まだメモはありません。上のフォームから追加してください。")
        return
    for memo in memos:
        with st.expander(f"{memo['date']} | {escape_markdown(memo.get('tags') or '無題')}"):
            st.markdown(escape_markdown(memo["raw_text"]))


def render_task_editor(task: Dict) -> None:
    """タスクの内容・期限・繰り返し規則をまとめて編集する。"""
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
    default_due_time = dt.time(hour=load_config().google_calendar_deadline_hour)
    if task.get("due_time"):
        try:
            default_due_time = dt.time.fromisoformat(task["due_time"])
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
                "所要時間",
                ["short", "medium", "long"],
                index=["short", "medium", "long"].index(default_effort),
                key=f"effort_{task['id']}",
            )
        with cols[3]:
            energy = st.selectbox(
                "エネルギー",
                ["low", "mid", "high"],
                index=["low", "mid", "high"].index(default_energy),
                key=f"energy_{task['id']}",
            )

        col_date, col_status = st.columns(2)
        with col_date:
            new_due_date = st.date_input(
                "期限", value=default_due_date, key=f"due_date_{task['id']}"
            )
        with col_status:
            status = st.selectbox(
                "ステータス",
                ["inbox", "today", "week", "done"],
                index=["inbox", "today", "week", "done"].index(default_status),
                key=f"status_{task['id']}",
            )

        new_due_time = st.time_input(
            "期限時刻", value=default_due_time, key=f"due_time_{task['id']}"
        )

        rule, end_date = _render_recurrence_inputs(f"task_edit_{task['id']}", task, new_due_date)
        submit = st.form_submit_button("更新")
        if submit:
            try:
                update_item_fields(
                    task["id"],
                    tags=new_tags,
                    raw_text=raw_text,
                    due_date=new_due_date.isoformat() if new_due_date else None,
                    due_time=new_due_time.strftime("%H:%M") if new_due_date else None,
                    importance=importance,
                    urgency=urgency,
                    effort=effort,
                    energy=energy,
                    status=status,
                    recurrence_settings=(rule, end_date),
                )
                st.success("更新しました。")
            except ValueError as exc:
                st.warning(str(exc))

    if task.get("recurrence_rule_id") and st.button(
        "繰り返しを停止", key=f"stop_recurrence_{task['id']}"
    ):
        stop_task_recurrence(task["id"])
        st.success("このタスク以降の繰り返しを停止しました。")
        st.rerun()

    render_breakdown_button(task)
    render_delegate_button(task)

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
    st.subheader("🗂️ タスクを整理")
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
        st.info("条件に合うタスクはありません。条件を変えるか、上のフォームからタスクを追加してください。")
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
            with st.expander(f"[{task.get('status', 'inbox')}] {escape_markdown(display_title)}"):
                render_task_editor(task)


def _display_tasks(tasks: List[Dict], title: str) -> None:
    """タスクをクエストの一覧として表示する。完了ボタン 1 つで片付けられる。"""
    if title:
        st.subheader(title)
    if not tasks:
        st.info("いま片付けるクエストはありません。")
        return
    today = dt.date.today()
    for task in sorted(tasks, key=_due_priority_tuple):
        due_str = task.get("due_date")
        is_overdue = False
        if due_str:
            try:
                is_overdue = dt.date.fromisoformat(due_str) < today
            except ValueError:
                pass
        heading = (task.get("tags") or "").strip() or next(
            iter((task.get("raw_text") or "").strip().splitlines()), f"タスク #{task['id']}"
        )
        body = (task.get("raw_text") or "").strip()
        with st.container(border=True):
            done_col, text_col = st.columns([1, 7], vertical_alignment="center")
            with done_col:
                if st.button("✔", key=f"done_btn_{task['id']}", help="完了にする", use_container_width=True):
                    next_task_id = complete_task(task["id"])
                    if next_task_id:
                        st.toast("次回分のタスクも作成しました。")
                    st.rerun()
            with text_col:
                prefix = "🚨 " if is_overdue else ""
                st.markdown(f"{prefix}**{escape_markdown(heading)}**")
                meta = [f"期限 {due_str}" + (f" {task['due_time']}" if task.get("due_time") else "") if due_str else "期限なし"]
                if (task.get("importance") or 3) >= 4:
                    meta.append("重要")
                if body and body != heading:
                    meta.append(escape_markdown(body.splitlines()[0][:60]))
                caption = " ・ ".join(meta)
                st.caption(f":red[{caption}]" if is_overdue else caption)


def render_today_tab() -> None:
    """ホーム：秘書・次の一手・クイック追加・今日の計画とタスクを 1 画面に集める。"""
    today_str = dt.date.today().isoformat()
    render_quick_add()
    st.button(
        "🧠 マインドマップで考えを広げる",
        key="home_open_mindmap",
        on_click=_go_to_page,
        args=("アイデア整理",),
        help="テーマを書くだけで AI がマインドマップを作り、枝をタスクにできます。",
    )
    render_secretary_panel()
    candidate_tasks = fetch_tasks(statuses=["today", "week", "inbox"], limit=300)
    left, right = st.columns([3, 2], gap="large")
    with left:
        _display_tasks(fetch_tasks_for_today_view(today_str), "⚔️ 今日のクエスト")
        later = [t for t in candidate_tasks if t.get("status") in ("week", "inbox") and not _is_due_by(t, today_str)]
        if later:
            with st.expander(f"あとでやる（{len(later)}件）"):
                _display_tasks(later, "")
    with right:
        render_focus_launcher(candidate_tasks)
        events = today_events_for_plan()
        with st.container(border=True):
            render_ai_plan(candidate_tasks, events)
        with st.container(border=True):
            render_timeblock(candidate_tasks, events)
        with st.container(border=True):
            render_today_agenda()


def _is_due_by(task: Dict, day: str) -> bool:
    """期限が指定日以前（期限切れを含む）なら True。"""
    due = task.get("due_date")
    return bool(due) and due <= day


def render_inbox_page() -> None:
    """受信箱：AI の候補、頭の中の書き出し、資料からの抽出を 1 か所にまとめる。"""
    reports = pending_report_count()
    candidates_tab, reports_tab, dump_tab, doc_tab = st.tabs(
        ["🤖 AI からの候補", f"📨 AI の進捗報告（{reports}）", "🧠 頭の中を書き出す", "📄 資料から抽出"]
    )
    with candidates_tab:
        render_auto_import()
        render_candidates_tab()
    with reports_tab:
        render_agent_reports()
    with dump_tab:
        render_brain_dump()
    with doc_tab:
        render_doc_analysis_tab()


def render_tasks_page() -> None:
    """タスクとメモ。"""
    task_tab, memo_tab = st.tabs(["✅ タスク", "📝 メモ"])
    with task_tab:
        render_task_tab()
    with memo_tab:
        render_memo_tab()


def render_settings_page() -> None:
    """設定：秘書・Google Calendar・AI 連携。"""
    secretary_tab, calendar_tab, ai_tab, usage_tab = st.tabs(
        ["🧑‍💼 秘書", "📆 Google Calendar", "🔌 AI 連携（ChatGPT / Claude）", "📊 AI の利用状況"]
    )
    with secretary_tab:
        render_secretary_settings()
    with calendar_tab:
        render_google_calendar_settings()
    with ai_tab:
        render_connections_settings()
    with usage_tab:
        render_ai_usage()


MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
DOC_ANALYSIS_KEY = "doc_analysis_result"


def render_doc_analysis_tab() -> None:
    """アップロードした資料を、ボタンを押したときだけ AI で要約・タスク抽出する。"""
    st.subheader("📄 ドキュメント分析")
    uploaded_file = st.file_uploader("ファイルをアップロードしてください (PDF, DOCX, TXT, MD)", type=["pdf", "docx", "txt", "md"])
    if uploaded_file is None:
        st.session_state.pop(DOC_ANALYSIS_KEY, None)
        return
    file_content = uploaded_file.getvalue()
    if len(file_content) > MAX_DOCUMENT_BYTES:
        st.warning("ファイルが大きすぎます（10MB まで）。")
        return
    digest = hashlib.sha256(file_content).hexdigest()
    saved = st.session_state.get(DOC_ANALYSIS_KEY)
    # Why not アップロード直後に自動分析: Streamlit はボタン操作のたびに再実行するため、
    # そのたびに AI を呼んで利用回数と料金を消費してしまう。
    if not saved or saved.get("digest") != digest:
        if not st.button("🤖 AI で要約・タスク抽出", key="doc_analyze", type="primary"):
            return
        with st.spinner("ファイルを処理中..."):
            file_type = uploaded_file.name.split(".")[-1].lower()
            try:
                text = extract_text_from_file(file_content, file_type)
            except Exception as exc:
                st.error(f"ファイルを読み込めませんでした: {escape_markdown(exc)}")
                return
            if not text.strip():
                st.warning("ファイルからテキストを抽出できませんでした。")
                return
            saved = {"digest": digest, "result": summarize_and_extract_tasks_from_text(text)}
            st.session_state[DOC_ANALYSIS_KEY] = saved
    analysis_result = saved["result"]

    st.subheader("要約")
    st.write(escape_markdown(analysis_result.get("summary", "要約を生成できませんでした。")))

    st.subheader("抽出されたタスク")
    tasks = analysis_result.get("tasks", [])
    if not tasks:
        st.info("タスクは抽出されませんでした。")
    else:
        for i, task_text in enumerate(tasks):
            col1, col2 = st.columns([3, 1])
            with col1:
                st.write(escape_markdown(task_text))
            with col2:
                if st.button("タスクとして保存", key=f"add_task_{i}"):
                    insert_task(raw_text=task_text, tags="from_document")
                    st.success(f"タスク「{escape_markdown(task_text[:30])}...」を保存しました。")


def render_task_hub_tab() -> None:
    """Backward-compatible entry point for the unified task workspace."""
    render_task_tab()


def main() -> None:
    """Streamlit でメモ・タスク・今日・カレンダービューを提供。"""
    st.set_page_config(page_title="SymNote", page_icon="🧠", layout="wide")
    _apply_app_style()
    if not _require_passcode():
        return
    init_db()
    # Never block local work on the network: failed delivery remains queued in
    # SQLite and is retried on a later app run.
    if google_calendar_connected():
        sync_pending_tasks()
    if render_focus_mode():
        return
    selected = render_sidebar()
    if is_test_mode():
        _render_test_banner()
    render_mobile_nav()
    rewards = pop_unseen_rewards()
    if rewards:
        _show_celebration(rewards)
    st.markdown(f"<h1 class='symnote-page-title'>{PAGES[selected]}</h1>", unsafe_allow_html=True)
    st.caption(PAGE_SUBTITLES[selected])

    renderers = {
        "今日": render_today_tab,
        "ゴール": render_goals_page,
        "タスク": render_tasks_page,
        "候補の承認": render_inbox_page,
        "アイデア整理": render_idea_tab,
        "カレンダー": render_calendar_tab,
        "報酬": render_rewards_page,
        "設定": render_settings_page,
    }
    renderers[selected]()

    # The start-of-run sync retries offline work.  A second, best-effort pass
    # publishes task changes made by the screen that was just rendered.
    if google_calendar_connected():
        sync_pending_tasks()


if __name__ == "__main__":
    main()
