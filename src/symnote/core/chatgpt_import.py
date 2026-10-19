"""ChatGPT のエクスポートと貼り付け会話を、出典付きの抽出入力へ変換する。"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import math
import zipfile
from typing import Any, List, Tuple

from symnote.core.task_extraction import ImportItem, MAX_ITEM_CHARACTERS

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_JSON_BYTES = 50 * 1024 * 1024
MAX_PASTE_CHARACTERS = 200_000
OMITTED_MARKER = "[前半省略]\n"


class ChatGPTImportError(ValueError):
    """会話ファイルの形式やサイズが取り込み条件に合わないことを表す。"""


def _load_export(payload: bytes) -> Any:
    """ZIP をディスクへ展開せず、サイズ制限下で conversations.json を読む。"""
    if len(payload) > MAX_UPLOAD_BYTES:
        raise ChatGPTImportError("アップロードは25MB以下にしてください。必要なら conversations.json を分割してください。")
    try:
        if zipfile.is_zipfile(io.BytesIO(payload)):
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                members = [m for m in archive.infolist() if not m.is_dir() and m.filename.replace("\\", "/").split("/")[-1] == "conversations.json"]
                if len(members) != 1:
                    raise ChatGPTImportError("ZIP 内に conversations.json が1つ必要です。")
                member = members[0]
                if member.file_size > MAX_JSON_BYTES:
                    raise ChatGPTImportError("ZIP 内の conversations.json は50MB以下にしてください。")
                with archive.open(member) as stream:
                    payload = stream.read(MAX_JSON_BYTES + 1)
                if len(payload) > MAX_JSON_BYTES:
                    raise ChatGPTImportError("会話 JSON の展開サイズが上限を超えています。")
        return json.loads(payload.decode("utf-8-sig"))
    except ChatGPTImportError:
        raise
    except (ValueError, UnicodeError, zipfile.BadZipFile, RuntimeError, NotImplementedError, RecursionError, OSError):
        raise ChatGPTImportError("ChatGPT の ZIP または conversations.json を読み取れませんでした。形式を確認してください。") from None


def _timestamp(value: Any) -> float | None:
    """有限の Unix 時刻だけを受け付け、欠損は None にする。"""
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 253402300799 else None


def _conversation_messages(conversation: dict[str, Any]) -> List[Tuple[float, int, str]]:
    """mapping の有効な会話経路から user/assistant の本文を時系列に取り出す。"""
    mapping = conversation.get("mapping")
    if not isinstance(mapping, dict):
        return []
    current = conversation.get("current_node")
    if isinstance(current, str) and current in mapping:
        selected: set[str] = set()
        while isinstance(current, str) and current in mapping and current not in selected:
            selected.add(current)
            node = mapping[current]
            current = node.get("parent") if isinstance(node, dict) else None
        nodes = [(key, value) for key, value in mapping.items() if key in selected]
    else:
        nodes = list(mapping.items())
    messages: List[Tuple[float, int, str]] = []
    default_time = _timestamp(conversation.get("create_time")) or 0.0
    for index, (_, node) in enumerate(nodes):
        message = node.get("message") if isinstance(node, dict) else None
        if not isinstance(message, dict):
            continue
        author = message.get("author")
        role = author.get("role") if isinstance(author, dict) else None
        if role not in {"user", "assistant"}:
            continue
        content = message.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        text = "\n".join(part for part in parts if isinstance(part, str)).strip()
        if not text:
            continue
        timestamp = _timestamp(message.get("create_time"))
        messages.append((timestamp if timestamp is not None else default_time, index, f"{role}: {text}"))
    return sorted(messages, key=lambda row: (row[0], row[1]))


def _keep_latest(text: str) -> str:
    """会話の末尾ほど未完了の用事が残りやすいため、上限超過時は前半を落とす。"""
    if len(text) <= MAX_ITEM_CHARACTERS:
        return text
    return OMITTED_MARKER + text[-(MAX_ITEM_CHARACTERS - len(OMITTED_MARKER)):]


def parse_export(
    payload: bytes,
    days: int = 30,
    max_conversations: int = 20,
    now: dt.datetime | None = None,
) -> List[ImportItem]:
    """直近 N 日の会話を新しい順に最大 M 件返す。日時不明の会話は除外する。

    current_node があればその祖先だけを採用し、再生成された別分岐は混ぜない。
    長い会話は直近の本文を優先し、抽出入力の文字数上限に切り詰める。
    """
    if not 1 <= days <= 3650 or not 1 <= max_conversations <= 100:
        raise ValueError("日数は1〜3650、会話件数は1〜100で指定してください。")
    data = _load_export(payload)
    if isinstance(data, dict):
        data = [data] if "mapping" in data else data.get("conversations")
    if not isinstance(data, list):
        raise ChatGPTImportError("会話一覧の配列がありません。ChatGPT の conversations.json を選択してください。")
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    cutoff = (current - dt.timedelta(days=days)).timestamp()
    records: List[Tuple[float, ImportItem]] = []
    seen: set[str] = set()
    for conversation in data:
        if not isinstance(conversation, dict):
            continue
        identifier = conversation.get("id") or conversation.get("conversation_id")
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 450:
            continue
        messages = _conversation_messages(conversation)
        if not messages:
            continue
        latest = max(_timestamp(conversation.get("update_time")) or 0.0, max(row[0] for row in messages))
        if latest < cutoff or latest > current.timestamp():
            continue
        ref = f"chatgpt:{identifier}"
        if ref in seen:
            continue
        seen.add(ref)
        text = "\n\n".join(row[2] for row in messages)
        text = _keep_latest(text)
        title = conversation.get("title")
        records.append((latest, ImportItem(
            source_ref=ref, text=text, title=title if isinstance(title, str) else "ChatGPT の会話",
            date=dt.datetime.fromtimestamp(latest, dt.timezone.utc).isoformat(),
        )))
    records.sort(key=lambda row: row[0], reverse=True)
    return [item for _, item in records[:max_conversations]]


def parse_pasted_text(text: str) -> ImportItem:
    """貼り付けた会話を本文ハッシュ付きの出典にする。

    同じ本文は同じ source_ref になり、候補の重複排除が効く。長文は末尾を優先して
    抽出上限に切り詰め、空欄と極端に大きい入力は拒否する。
    """
    normalized = text.strip()
    if not normalized:
        raise ChatGPTImportError("会話テキストを貼り付けてください。")
    if len(normalized) > MAX_PASTE_CHARACTERS:
        raise ChatGPTImportError(f"貼り付けは{MAX_PASTE_CHARACTERS:,}文字以内にしてください。")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return ImportItem(source_ref=f"chatgpt:paste:{digest}", text=_keep_latest(normalized), title="貼り付けた会話")
