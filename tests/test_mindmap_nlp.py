from __future__ import annotations

import pytest

from symnote.core.nlp import _parse_mindmap_json, expand_node, generate_initial_mindmap


def test_expand_node_fails_fast_without_api_key(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "")

    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        expand_node("Idea", "Details", "Context", "related_ideas")


def test_initial_mindmap_fails_fast_without_api_key(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "")

    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        generate_initial_mindmap("Source")


def test_mindmap_json_parser_accepts_plain_and_fenced_json() -> None:
    assert _parse_mindmap_json('{"kind":"root"}') == {"kind": "root"}
    assert _parse_mindmap_json('```json\n{"kind":"root"}\n```') == {"kind": "root"}

    with pytest.raises(ValueError, match="閉じられていません"):
        _parse_mindmap_json('```json\n{"kind":"root"}')
