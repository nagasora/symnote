"""ユーザーや外部 AI 由来の文字列を、Streamlit で安全に表示するための補助関数。"""

from __future__ import annotations

import re

_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$])")


def escape_markdown(text: object) -> str:
    """Markdown の記号をエスケープし、文字列をそのままの見た目で表示させる。

    st.markdown / st.write / ウィジェットのラベルは Markdown を解釈するため、
    外部由来の文字列に画像やリンク記法が含まれると埋め込まれてしまう。
    """
    return _MARKDOWN_SPECIAL.sub(r"\\\1", "" if text is None else str(text))
