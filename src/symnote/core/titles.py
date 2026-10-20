"""タスクとメモの表示タイトルを一貫して取り出す共通関数。"""

from __future__ import annotations

from typing import Any, Mapping, Tuple


def task_title(row: Mapping[str, Any]) -> str:
    """タイトル（tags）が無い既存タスクは本文の 1 行目をタイトルとして扱う。"""
    title = str(row.get("tags") or "").strip()
    if title:
        return title
    lines = str(row.get("raw_text") or "").strip().splitlines()
    return lines[0].strip() if lines else f"（無題 #{row.get('id')}）"


def task_title_and_details(row: Mapping[str, Any]) -> Tuple[str, str]:
    """タスク行から (title, details) を取り出す。

    tags にタイトルが入っている場合は raw_text 全体を details とし、
    tags が空で raw_text の1行目をタイトルにした場合は2行目以降を details とする。
    ただし raw_text がタイトルと同じ1行だけなら details は空文字にする。
    """
    tags_title = str(row.get("tags") or "").strip()
    raw = str(row.get("raw_text") or "").strip()
    if tags_title:
        details = "" if raw == tags_title else raw
        return tags_title, details
    lines = raw.splitlines()
    if not lines:
        return f"（無題 #{row.get('id')}）", ""
    title = lines[0].strip()
    details = "\n".join(lines[1:]).strip()
    return title, details


def memo_title_and_body(row: Mapping[str, Any]) -> Tuple[str, str]:
    """メモ行から一覧用の見出し（1行目）と本文全体を取り出す。"""
    raw = str(row.get("raw_text") or "").strip()
    lines = raw.splitlines()
    if lines:
        return lines[0].strip()[:120], raw
    tags_title = str(row.get("tags") or "").strip()
    return (tags_title or f"（無題メモ #{row.get('id')}）"), raw
