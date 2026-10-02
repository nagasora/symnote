"""外部由来の文字列を Markdown として安全に表示するためのエスケープ処理のテスト。"""

from __future__ import annotations

from symnote.views.text import escape_markdown


def test_image_and_link_syntax_is_neutralized() -> None:
    """画像・リンク記法の括弧はエスケープされ、埋め込みにならない。"""
    escaped = escape_markdown("![x](https://evil.example/p.png) と [安全](https://evil.example)")

    assert escaped.startswith("\\!\\[x\\]\\(<https://evil.example/p.png>\\)")
    assert "\\[安全\\]\\(<https://evil.example>\\)" in escaped
    assert "](" not in escaped


def test_bare_urls_become_explicit_autolinks() -> None:
    """素の URL はエスケープせず <...> で囲み、リンク先が壊れない。"""
    text = "資料: https://example.com/a_b.c?x=1&y=2 と www.example.org/path"

    escaped = escape_markdown(text)

    assert "<https://example.com/a_b.c?x=1&y=2>" in escaped
    assert "<http://www.example.org/path>" in escaped


def test_markdown_emphasis_and_headings_are_escaped() -> None:
    """強調・見出し・HTML 風の記号は文字として表示される。"""
    assert escape_markdown("# 見出し **太字** <b>") == "\\# 見出し \\*\\*太字\\*\\* \\<b\\>"
    assert escape_markdown(None) == ""
