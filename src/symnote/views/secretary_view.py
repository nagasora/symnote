"""今日の画面に組み込める、コンパクトな秘書パネルと入力・設定画面。"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import math
import struct
import wave
from dataclasses import asdict
from typing import Any, Dict, List, Optional

import streamlit as st

from symnote.core import secretary
from symnote.views.text import escape_markdown

PENDING = "secretary_pending_command"
DRAFT = "secretary_dump_draft"


def _invalidate() -> None:
    """変更後のブリーフィングだけを無効にする。"""
    st.session_state.pop("secretary_briefing", None)


def _sound() -> None:
    """効果音が許可された場合だけ、小さな完了音を生成する。"""
    if not secretary.load_settings().sound_enabled:
        return
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        samples = [
            int(2500 * math.sin(2 * math.pi * 660 * index / 8000) * (1 - index / 1200))
            for index in range(1200)
        ]
        audio.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    st.audio(buffer.getvalue(), format="audio/wav", autoplay=True)


def _notice() -> None:
    """再描画をまたいで完了メッセージと音を一度だけ表示する。"""
    notice = st.session_state.pop("secretary_notice", None)
    if notice:
        st.success(escape_markdown(notice))
        _sound()


def _applied(command: secretary.Command) -> None:
    """確認済みの操作を実行し、成功した場合だけ確認状態を破棄する。"""
    try:
        secretary.execute_command(command, confirmed=True)
    except Exception:
        st.error("変更できませんでした。対象の状態や保存先を確認し、再試行してください。")
        return
    message = (
        secretary.reaction_for("complete_task")
        if command.intent == "complete_task"
        else "予定を更新しました。"
    )
    st.session_state.pop(PENDING, None)
    st.session_state["secretary_notice"] = message
    _invalidate()
    try:
        secretary.save_message("secretary", message)
    except Exception:
        # 実操作成功後の会話保存失敗で、同じ変更を再実行できる状態へ戻さない。
        st.warning("変更は保存しましたが、会話履歴の記録はできませんでした。")
    st.rerun()


def _render_pending(tasks: List[Dict[str, Any]]) -> None:
    """対象名と変更内容を提示し、ユーザーの確認を待つ。"""
    command: Optional[secretary.Command] = st.session_state.get(PENDING)
    if command is None:
        return
    params = command.parameters
    by_id = {task["id"]: task for task in tasks}
    task = by_id.get(params.get("task_id"), {})
    title = params.get("title") or task.get("tags") or task.get("raw_text") or "対象なし"
    action = {"complete_task": "完了にする", "reschedule": "延期する", "add_task": "追加する"}[
        command.intent
    ]
    st.info(
        escape_markdown(
            f"「{title}」を{action}。日時: {params.get('due_date') or '変更なし'} {params.get('due_time') or ''}"
        )
    )
    accept, cancel = st.columns(2)
    if accept.button("確認して実行", key="secretary_confirm"):
        _applied(command)
    if cancel.button("キャンセル", key="secretary_cancel"):
        st.session_state.pop(PENDING, None)
        st.rerun()


def _calendar_snapshot(now: dt.datetime) -> List[Dict[str, Any]]:
    """予定の再取得を15分に一度へ抑え、通信できない場合は空にする。"""
    key = (now.date().isoformat(), now.hour, now.minute // 15)
    cached = st.session_state.get("secretary_calendar")
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        from symnote.views.today_ai_view import today_events_for_plan

        events = today_events_for_plan()
    except Exception:
        events = []
    st.session_state["secretary_calendar"] = (key, events)
    return events


def _render_focus(
    tasks: List[Dict[str, Any]], events: List[Dict[str, Any]], now: dt.datetime
) -> None:
    """一つだけのフォーカスカードを表示する。"""
    action = secretary.next_action(
        tasks, now, free_minutes=secretary.available_minutes(events, now)
    )
    if action is None:
        st.caption("今取り組むタスクはありません。頭の中にあることを下から書き出せます。")
        return
    with st.container(border=True):
        st.caption("今は、この一つだけ")
        st.markdown(f"### {escape_markdown(action.title)}")
        st.caption(escape_markdown(f"{action.reason} ・ 目安 {action.estimated_minutes}分"))
        complete, later, split = st.columns(3)
        if complete.button(
            "完了", key=f"secretary_complete_{action.task_id}", use_container_width=True
        ):
            st.session_state[PENDING] = secretary.Command(
                "complete_task", {"task_id": action.task_id}, True
            )
            st.rerun()
        if later.button(
            "後で（明日へ）", key=f"secretary_later_{action.task_id}", use_container_width=True
        ):
            tomorrow = (now.date() + dt.timedelta(days=1)).isoformat()
            st.session_state[PENDING] = secretary.Command(
                "reschedule", {"task_id": action.task_id, "due_date": tomorrow}, True
            )
            st.rerun()
        if split.button(
            "小さく分ける", key=f"secretary_split_{action.task_id}", use_container_width=True
        ):
            st.session_state[DRAFT] = secretary.breakdown_focus(action.title)
            st.session_state["secretary_dump_saved"] = []
            st.session_state["secretary_dump_revision"] = (
                st.session_state.get("secretary_dump_revision", 0) + 1
            )
            st.session_state["secretary_dump_open"] = True
            st.rerun()


def _respond(
    text: str, tasks: List[Dict[str, Any]], events: List[Dict[str, Any]], now: dt.datetime
) -> None:
    """会話を保存し、操作は保留、質問にはその場で回答する。"""
    context = {"tasks": tasks, "now": now, "messages": secretary.recent_messages()}
    secretary.save_message("user", text, now)
    settings = secretary.load_settings()
    context["settings"] = asdict(settings)
    command = secretary.interpret_command(text, context)
    reply = command.reply
    if command.intent in ("complete_task", "reschedule", "add_task"):
        st.session_state[PENDING] = command
    elif command.intent == "ask_plan":
        action = secretary.next_action(
            tasks, now, free_minutes=secretary.available_minutes(events, now)
        )
        reply = (
            f"次は「{action.title}」。{action.reason}"
            if action
            else "未完了のタスクはありません。おつかれさまです。"
        )
    elif command.intent == "brain_dump":
        st.session_state[DRAFT] = secretary.split_brain_dump(text, now.date())
        st.session_state["secretary_dump_saved"] = []
        st.session_state["secretary_dump_revision"] = (
            st.session_state.get("secretary_dump_revision", 0) + 1
        )
        st.session_state["secretary_dump_open"] = True
        reply = "下の確認欄で、追加したいタスクを選んでください。"
    secretary.save_message("secretary", reply or "内容を確認してください。", now)


def render_secretary_panel() -> None:
    """アバター・状況説明・一つの行動・確認付き会話をまとめて表示する。"""
    from symnote.core.db import fetch_tasks

    now = dt.datetime.now()
    try:
        settings = secretary.load_settings()
        tasks = fetch_tasks(limit=10000)
        events = _calendar_snapshot(now)
        fingerprint = hashlib.sha256(
            json.dumps(
                [tasks, asdict(settings)], ensure_ascii=False, sort_keys=True, default=str
            ).encode()
        ).hexdigest()
        key = (
            now.date().isoformat(),
            secretary.time_period(now),
            now.hour * 4 + now.minute // 15,
            fingerprint,
        )
        cached = st.session_state.get("secretary_briefing")
        if cached is None or cached[0] != key:
            briefing = secretary.build_briefing(now, tasks=tasks, events=events, settings=settings)
            st.session_state["secretary_briefing"] = (key, briefing)
        else:
            briefing = cached[1]
        avatar, bubble = st.columns([1, 7])
        with avatar:
            st.markdown(secretary.avatar_svg(briefing.expression), unsafe_allow_html=True)
        with bubble:
            st.markdown(f"**{escape_markdown(settings.name)}**")
            st.markdown(escape_markdown(briefing.text))
        _notice()
        _render_focus(tasks, events, now)
        _render_pending(tasks)
        for alert in secretary.stale_alerts()[:2]:
            st.caption(escape_markdown(alert))
        with st.expander("秘書と話す", expanded=True):
            for message in secretary.recent_messages()[-4:]:
                name = settings.user_name if message["role"] == "user" else settings.name
                st.markdown(f"**{escape_markdown(name)}** · {escape_markdown(message['content'])}")
            with st.form("secretary_chat_form", clear_on_submit=True):
                text = st.text_input(
                    "秘書へのメッセージ",
                    placeholder="今日何すればいい？ / レポート終わった / 明日10時に歯医者を追加",
                )
                submit = st.form_submit_button("送信")
            if submit and text.strip():
                _respond(text, tasks, events, now)
                st.rerun()
        with st.expander(
            "頭の中を全部出す", expanded=st.session_state.get("secretary_dump_open", False)
        ):
            render_brain_dump()
    except Exception:
        # 秘書機能の失敗を、既存のタスク管理画面全体の停止へ波及させない。
        st.warning("秘書の情報を読み込めませんでした。通常のタスク操作は引き続き利用できます。")


def render_brain_dump() -> None:
    """文章を候補に整理し、選択されたものだけを一括保存する。"""
    with st.form("secretary_dump_form"):
        text = st.text_area(
            "気になっていることを、そのまま全部",
            height=150,
            placeholder="明日までに見積もりを送る。机を片付ける。来週の出張の準備…",
        )
        organize = st.form_submit_button("整理して確認", type="primary")
    if organize and text.strip():
        with st.spinner("一つずつ整理しています…"):
            st.session_state[DRAFT] = secretary.split_brain_dump(text)
            st.session_state["secretary_dump_saved"] = []
            st.session_state["secretary_dump_revision"] = (
                st.session_state.get("secretary_dump_revision", 0) + 1
            )
    candidates: List[secretary.DumpTask] = st.session_state.get(DRAFT, [])
    saved: List[int] = st.session_state.get("secretary_dump_saved", [])
    revision = st.session_state.get("secretary_dump_revision", 0)
    selected: List[int] = []
    for index, task in enumerate(candidates):
        if index in saved:
            continue
        label = f"{task.title} ・ {task.due_date or '期限なし'} {task.due_time or ''} ・ 重要度{task.importance} ・ 約{task.estimated_minutes}分"
        if task.goal_id is not None:
            label += f" ・ ゴール#{task.goal_id}"
        if st.checkbox(
            escape_markdown(label), value=True, key=f"secretary_dump_{revision}_{index}"
        ):
            selected.append(index)
    if candidates and st.button(
        "選んだタスクを一括追加", disabled=not selected, key="secretary_dump_save"
    ):
        count = 0
        for index in selected:
            try:
                secretary.save_dump_task(candidates[index])
            except secretary.GoalAssignmentError as error:
                st.warning(
                    f"タスク#{error.task_id}は保存済みですが、ゴールには関連付けできませんでした。後から設定できます。"
                )
            except Exception:
                st.error("一部を保存できませんでした。保存済みのタスクは再追加しません。")
                break
            saved.append(index)
            st.session_state["secretary_dump_saved"] = saved
            count += 1
        _invalidate()
        if count:
            st.success(f"{count}件追加しました。")
        if len(saved) == len(candidates):
            st.session_state.pop(DRAFT, None)
            st.session_state.pop("secretary_dump_saved", None)
            st.rerun()


def render_secretary_settings() -> None:
    """名前・口調・ユーザーの呼び方・効果音を編集する。"""
    settings = secretary.load_settings()
    with st.form("secretary_settings_form"):
        name = st.text_input("秘書の名前", value=settings.name, max_chars=40)
        tone = st.selectbox("口調", secretary.TONES, index=secretary.TONES.index(settings.tone))
        user_name = st.text_input("あなたの呼び方", value=settings.user_name, max_chars=40)
        sound = st.checkbox("完了時の効果音", value=settings.sound_enabled)
        save = st.form_submit_button("秘書設定を保存")
    if save:
        try:
            secretary.save_settings(secretary.SecretarySettings(name, tone, user_name, sound))
        except Exception:
            st.error("設定を保存できませんでした。再試行してください。")
        else:
            _invalidate()
            st.success("秘書の設定を保存しました。")
