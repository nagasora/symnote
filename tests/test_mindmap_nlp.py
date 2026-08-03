from __future__ import annotations

import pytest

from symnote.core import nlp
from symnote.core.nlp import _parse_mindmap_json, expand_node, generate_initial_mindmap


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


class _ModelWithResponse:
    def __init__(self, text: str) -> None:
        self._text = text

    def generate_content(self, *_args, **_kwargs) -> _Response:
        return _Response(self._text)


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


@pytest.mark.parametrize(
    "content",
    ["", "```json\n\n```", "not valid JSON"],
)
def test_mindmap_json_parser_rejects_empty_or_non_json_responses(content: str) -> None:
    with pytest.raises(ValueError, match="有効なマインドマップ形式"):
        _parse_mindmap_json(content)


def test_mindmap_model_requests_json_response(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeGenerationConfig:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(nlp.genai, "configure", lambda **_kwargs: None)
    monkeypatch.setattr(nlp.genai, "GenerationConfig", FakeGenerationConfig)
    monkeypatch.setattr(nlp.genai, "GenerativeModel", lambda *_args, **_kwargs: object())

    nlp._mindmap_model()

    assert captured["response_mime_type"] == "application/json"


def test_initial_mindmap_rejects_invalid_model_response(monkeypatch) -> None:
    monkeypatch.setattr(nlp, "_mindmap_model", lambda: _ModelWithResponse("not valid JSON"))

    with pytest.raises(RuntimeError, match="有効なマインドマップ形式"):
        generate_initial_mindmap("Source")


def test_expand_node_rejects_invalid_model_response(monkeypatch) -> None:
    monkeypatch.setattr(nlp, "_mindmap_model", lambda: _ModelWithResponse("```json\n\n```"))

    with pytest.raises(RuntimeError, match="有効なマインドマップ形式"):
        expand_node("Idea", "Details", "Context", "related_ideas")
