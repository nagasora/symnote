"""ChatGPT のエクスポート・貼り付けテキストを安全に抽出入力へ変換できることを保証するテスト。"""

from __future__ import annotations

import datetime as dt
import io
import json
import zipfile
from typing import Any, Dict, Optional

import pytest

from symnote.core import chatgpt_import
from symnote.core.chatgpt_import import ChatGPTImportError, parse_export, parse_pasted_text
from symnote.core.task_extraction import MAX_ITEM_CHARACTERS

NOW = dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.timezone.utc)
RECENT = NOW.timestamp() - 3600


def _node(parent: Optional[str], role: str, text: Any, ts: Optional[float]) -> Dict[str, Any]:
    """mapping の 1 ノードを作る。"""
    return {"parent": parent, "message": {
        "author": {"role": role}, "create_time": ts, "content": {"content_type": "text", "parts": [text]},
    }}


def _conversation(cid: str = "c1", ts: float = RECENT, title: str = "相談") -> Dict[str, Any]:
    """分岐を 1 つ持つ会話を作る（current_node は b の枝）。"""
    return {
        "id": cid, "title": title, "create_time": ts - 100, "update_time": ts,
        "current_node": "b2",
        "mapping": {
            "root": {"parent": None, "message": None},
            "sys": _node("root", "system", "システム指示", ts - 90),
            "u1": _node("sys", "user", "明日までに請求書を出さないと", ts - 80),
            "a1": _node("u1", "assistant", "了解です。", ts - 70),
            "b1": _node("a1", "user", "あと歯医者の予約も", ts - 60),
            "b2": _node("b1", "assistant", "予約を忘れずに。", ts - 50),
            "x1": _node("a1", "user", "捨てられた分岐", ts - 40),
        },
    }


def _zip(files: Dict[str, bytes], compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    """メモリ上で ZIP を作る。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _json(data: Any) -> bytes:
    """JSON をバイト列にする。"""
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def test_zip_export_follows_current_branch_in_time_order() -> None:
    """ZIP 内の conversations.json から現在の枝の user/assistant 発言だけを時系列で取り出す。"""
    payload = _zip({"export/conversations.json": _json([_conversation()]), "chat.html": b"<html>"})

    [item] = parse_export(payload, now=NOW)

    assert item.source_ref == "chatgpt:c1"
    assert item.title == "相談"
    assert item.text.splitlines() == [
        "user: 明日までに請求書を出さないと", "", "assistant: 了解です。", "",
        "user: あと歯医者の予約も", "", "assistant: 予約を忘れずに。",
    ]
    assert "捨てられた分岐" not in item.text and "システム指示" not in item.text


def test_plain_json_list_dict_wrapper_and_single_conversation_are_accepted() -> None:
    """JSON 単体（配列・conversations キー・会話 1 件）を受け付ける。"""
    conversation = _conversation()

    for data in ([conversation], {"conversations": [conversation]}, conversation):
        assert [item.source_ref for item in parse_export(_json(data), now=NOW)] == ["chatgpt:c1"]


def test_missing_current_node_uses_all_nodes_and_skips_broken_parts() -> None:
    """current_node が無ければ全ノードを使い、欠損・非文字列の発言は飛ばす。"""
    conversation = _conversation()
    del conversation["current_node"]
    conversation["mapping"]["bad1"] = {"parent": "u1"}
    conversation["mapping"]["bad2"] = _node("u1", "user", {"image": 1}, RECENT)
    conversation["mapping"]["bad3"] = {"message": {"author": "user", "content": None}}
    conversation["mapping"]["bad4"] = "壊れたノード"

    [item] = parse_export(_json([conversation, "not a dict", {"id": "no-mapping"}]), now=NOW)

    assert "捨てられた分岐" in item.text


def test_recent_window_and_max_conversations_keep_newest() -> None:
    """直近 N 日の会話だけを、新しい順に最大 M 件返す。"""
    old = _conversation("old", ts=NOW.timestamp() - 40 * 86400)
    conversations = [old] + [_conversation(f"c{i}", ts=RECENT - i * 60) for i in range(5)]

    items = parse_export(_json(conversations), days=30, max_conversations=3, now=NOW)

    assert [item.source_ref for item in items] == ["chatgpt:c0", "chatgpt:c1", "chatgpt:c2"]


def test_long_conversation_keeps_latest_part() -> None:
    """長い会話は直近の発言を残して上限文字数に収める。"""
    conversation = _conversation()
    conversation["mapping"]["b2"] = _node("b1", "assistant", "古" * 30000 + "最後の発言", RECENT)

    [item] = parse_export(_json([conversation]), now=NOW)

    assert len(item.text) <= MAX_ITEM_CHARACTERS
    assert item.text.endswith("最後の発言")


@pytest.mark.parametrize("payload", [
    b"not json",
    _json({"other": 1}),
    _zip({"readme.txt": b"x"}),
    _zip({"a/conversations.json": b"[]", "b/conversations.json": b"[]"}),
    b"PK\x03\x04broken",
])
def test_unreadable_exports_raise_japanese_error(payload: bytes) -> None:
    """壊れた・形式違いのファイルは日本語の ChatGPTImportError になる。"""
    with pytest.raises(ChatGPTImportError):
        parse_export(payload, now=NOW)


def test_zip_bomb_is_rejected_by_expanded_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """高圧縮で展開後サイズが上限を超える ZIP は、展開せずに拒否する。"""
    monkeypatch.setattr(chatgpt_import, "MAX_JSON_BYTES", 1000)
    bomb = _zip({"conversations.json": b"[" + b" " * 200_000 + b"]"})
    assert len(bomb) < 2000

    with pytest.raises(ChatGPTImportError):
        parse_export(bomb, now=NOW)


def test_oversized_upload_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """アップロード自体が上限を超えれば解析前に拒否する。"""
    monkeypatch.setattr(chatgpt_import, "MAX_UPLOAD_BYTES", 10)

    with pytest.raises(ChatGPTImportError, match="25MB"):
        parse_export(b"[" + b" " * 20 + b"]", now=NOW)


def test_pasted_text_uses_content_hash_as_source_ref() -> None:
    """貼り付けは本文ハッシュを出典にし、同じ本文なら同じ出典になる。"""
    first = parse_pasted_text("  請求書を出す  ")
    second = parse_pasted_text("請求書を出す")
    other = parse_pasted_text("別の会話")

    assert first.source_ref == second.source_ref != other.source_ref
    assert first.source_ref.startswith("chatgpt:paste:")
    assert first.text == "請求書を出す"


def test_pasted_text_rejects_empty_and_trims_long_input() -> None:
    """空の貼り付けは拒否し、長文は末尾を残して切り詰める。"""
    with pytest.raises(ChatGPTImportError):
        parse_pasted_text("   ")

    item = parse_pasted_text("前" * 20000 + "末尾")
    assert len(item.text) <= MAX_ITEM_CHARACTERS and item.text.endswith("末尾")
