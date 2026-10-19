"""冒険者HUD、完了演出、ご褒美ショップを表示するStreamlitビュー。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import List, Optional

import streamlit as st
import streamlit.components.v1 as components

from symnote.core.db import connection
from symnote.core.quest import (
    InsufficientGoldError, QuestReward, add_reward, get_player_stats,
    list_achievements, list_recent_quests, list_rewards, redeem_reward,
)

_COMPONENT_PATH = Path(__file__).resolve().parents[1] / "components" / "quest_celebration" / "index.html"


def _sound_enabled() -> bool:
    """効果音の永続設定を取得し、未設定なら無音にする。"""
    with connection() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'quest_sound'").fetchone()
    return row is not None and row[0] == "on"


def _save_sound(enabled: bool) -> None:
    """クエスト効果音設定のみを保存する。"""
    with connection() as conn, conn:
        conn.execute("INSERT INTO app_settings(key, value) VALUES ('quest_sound', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", ("on" if enabled else "off",))


def render_celebration(rewards: List[QuestReward], *, sound: Optional[bool] = None) -> None:
    """取得済み未表示報酬を合算表示する。sound未指定時はショップの設定を使う。"""
    if not rewards:
        return
    achievements = {item.code: item for item in list_achievements()}
    codes = list(dict.fromkeys(code for reward in rewards for code in reward.new_achievements))
    payload = {
        "titles": [reward.title for reward in rewards],
        "xp": sum(reward.xp for reward in rewards),
        "gold": sum(reward.gold for reward in rewards),
        "bonuses": list(dict.fromkeys(bonus for reward in rewards for bonus in reward.bonuses)),
        "levelBefore": min(reward.level_before for reward in rewards),
        "levelAfter": max(reward.level_after for reward in rewards),
        "achievements": [f"{achievements[code].icon} {achievements[code].title}" for code in codes if code in achievements],
        "sound": _sound_enabled() if sound is None else sound,
    }
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    document = _COMPONENT_PATH.read_text(encoding="utf-8").replace("__QUEST_PAYLOAD_BASE64__", encoded)
    components.html(document, height=300, scrolling=True)


def render_player_hud() -> None:
    """レベル・経験値の比率・ゴールド・連続日数をコンパクトに表示する。"""
    stats = get_player_stats()
    st.markdown(f"**Lv.{stats.level}** {stats.title}")
    st.caption(f"💰 {stats.gold:,} G ・ 🔥 連続 {stats.streak_days} 日")
    st.progress(stats.xp_into_level / stats.xp_for_next,
                text=f"XP {stats.xp_into_level} / {stats.xp_for_next} ・ 今日 {stats.completed_today} 件クリア")


def _render_shop() -> None:
    """明示操作でのおすすめ追加・ご褒美登録・交換を表示する。"""
    rewards = list_rewards()
    if not rewards:
        st.info("自分への小さなご褒美を登録しましょう。おすすめはボタンで追加できます。")
        for title, cost, icon in [("コンビニスイーツ", 100, "🍰"), ("ゆっくりカフェ時間", 200, "☕"), ("好きな映画を観る", 300, "🎬")]:
            if st.button(f"{icon} {title} {cost}G を追加", key=f"quest_suggest_{cost}"):
                add_reward(title, cost, icon)
                st.rerun()
    with st.form("quest_reward_form", clear_on_submit=True):
        title = st.text_input("ご褒美の名前", max_chars=200)
        cost = st.number_input("必要ゴールド", min_value=1, value=100, step=10)
        icon = st.text_input("アイコン", value="🎁", max_chars=8)
        submitted = st.form_submit_button("ご褒美を登録")
    if submitted:
        try:
            add_reward(title, int(cost), icon)
        except ValueError as exc:
            st.warning(str(exc))
        else:
            st.rerun()
    for reward in rewards:
        with st.container(border=True):
            st.text(f"{reward.icon} {reward.title} ・ {reward.cost:,}G")
            if st.button("交換する", key=f"quest_redeem_{reward.id}"):
                try:
                    remaining = redeem_reward(reward.id)
                except (InsufficientGoldError, ValueError) as exc:
                    st.warning(str(exc))
                else:
                    st.session_state["quest_exchange_notice"] = f"{reward.title} と交換しました。残高 {remaining:,}G"
                    st.rerun()


def render_rewards_page() -> None:
    """報酬ショップ・全実績・履歴・効果音の永続設定を表示する。"""
    render_player_hud()
    if notice := st.session_state.pop("quest_exchange_notice", None):
        st.success(notice)
    enabled = _sound_enabled()
    selected = st.toggle("クエスト完了の効果音", value=enabled, key="quest_sound_toggle")
    if selected != enabled:
        _save_sound(selected)
    st.caption("効果音はブラウザの自動再生設定によって鳴らない場合があります。")
    shop_tab, achievement_tab, history_tab = st.tabs(["ご褒美ショップ", "実績", "最近のクエスト"])
    with shop_tab:
        _render_shop()
    with achievement_tab:
        for item in list_achievements():
            status = f"解除済み {item.unlocked_at[:10]}" if item.unlocked_at else "これからの冒険"
            st.text(f"{item.icon} {item.title} ・ {status}")
            st.caption(item.description)
    with history_tab:
        history = list_recent_quests()
        if not history:
            st.info("最初のクエストをクリアすると、ここに足跡が残ります。")
        for item in history:
            st.text(f"{item['title']} ・ +{item['xp']}XP / +{item['gold']}G")
            st.caption(item["completed_at"])
