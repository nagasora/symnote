"""設定画面の「AI の利用状況」：今日の呼び出し回数と上限を表示する。"""

from __future__ import annotations

import streamlit as st

from symnote.core.usage_limit import usage_snapshot


def _limit_text(value: int) -> str:
    """上限値を表示用の文字列にする（0 以下は無制限）。"""
    return f"{value:,} 回" if value > 0 else "無制限"


def render_ai_usage() -> None:
    """今日の AI 呼び出し回数・上限と、上限の変え方を表示する。"""
    st.subheader("AI の利用状況")
    snapshot = usage_snapshot()
    limits = snapshot.limits
    first, second = st.columns(2)
    first.metric("今日の利用（この利用者）", f"{snapshot.today_scope:,} 回", help=f"上限 {_limit_text(limits.daily_per_user)}")
    second.metric("今日の利用（全体）", f"{snapshot.today_global:,} 回", help=f"上限 {_limit_text(limits.daily_global)}")
    if limits.daily_per_user > 0:
        st.progress(min(1.0, snapshot.today_scope / limits.daily_per_user))
    st.caption(
        f"上限: 1 分あたり {_limit_text(limits.per_minute)}・1 日 {_limit_text(limits.daily_per_user)}（利用者ごと）・"
        f"1 日 {_limit_text(limits.daily_global)}（全体）・入力 {limits.max_prompt_chars:,} 文字まで。"
        "上限に達すると AI 機能だけが止まり、手動での編集はそのまま使えます。"
    )
    st.caption(
        ".env の SYMNOTE_LLM_PER_MINUTE / SYMNOTE_LLM_DAILY_PER_USER / SYMNOTE_LLM_DAILY_GLOBAL / "
        "SYMNOTE_LLM_MAX_PROMPT_CHARS で変更できます。Google Cloud 側でも API キーの割り当て上限と"
        "予算アラートを設定しておくと二重の安全策になります。"
    )
