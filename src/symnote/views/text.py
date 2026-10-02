"""ユーザーや外部 AI 由来の文字列を、Streamlit で安全に表示するための補助関数。"""

from __future__ import annotations

import re

_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$])")
_BARE_URL = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]`\\]+", re.IGNORECASE)


def _escape_plain(text: str) -> str:
    """Markdown の記号をバックスラッシュでエスケープする。"""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def escape_markdown(text: object) -> str:
    """Markdown の記号をエスケープし、文字列をそのままの見た目で表示させる。

    st.markdown / st.write / ウィジェットのラベルは Markdown を解釈するため、
    外部由来の文字列に画像やリンク記法が含まれると埋め込まれてしまう。
    素の URL は ``<...>`` の明示的な自動リンクにする。GFM の自動リンク拡張は
    エスケープ記号や直後の記号までリンクに取り込んで URL を壊すため、範囲を確定させる。
    素の URL は表示どおりの先にしか飛ばないので、リンク化しても偽装にはならない。
    """
    value = "" if text is None else str(text)
    parts: list[str] = []
    position = 0
    for match in _BARE_URL.finditer(value):
        parts.append(_escape_plain(value[position : match.start()]))
        url = match.group(0)
        parts.append(f"<{url if '://' in url else 'http://' + url}>")
        position = match.end()
    parts.append(_escape_plain(value[position:]))
    return "".join(parts)
