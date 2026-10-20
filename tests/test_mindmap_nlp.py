"""共通LLM層を差し替えてマインドマップ生成の境界を検証する。"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from symnote.core import nlp
from symnote.core.nlp import expand_node, generate_initial_mindmap


class _FakeLLM:
    """ネットワークを使わず生成結果と呼び出し内容を保持する。"""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        """固定応答または送出する例外を設定する。"""
        self.result = result
        self.error = error
        self.calls: List[Tuple[str, Any, str, float]] = []

    def generate_json(
        self, prompt: str, schema: Any = None, *, hint: str = "", timeout: float = 45
    ) -> Any:
        """共通層の公開インターフェイスで固定応答を返す。"""
        self.calls.append((prompt, schema, hint, timeout))
        if self.error is not None:
            raise self.error
        return self.result


def test_expand_node_fails_fast_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """共通層の設定エラーは日本語の展開エラーとして伝播する。"""
    fake = _FakeLLM(error=RuntimeError("LLM_API_KEYが設定されていません。"))
    monkeypatch.setattr(nlp.llm, "generate_json", fake.generate_json)

    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        expand_node("Idea", "Details", "Context", "related_ideas")


def test_initial_mindmap_fails_fast_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """共通層の設定エラーはマップ生成エラーとして伝播する。"""
    fake = _FakeLLM(error=RuntimeError("LLM_API_KEYが設定されていません。"))
    monkeypatch.setattr(nlp.llm, "generate_json", fake.generate_json)

    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        generate_initial_mindmap("Source")


def test_initial_mindmap_accepts_parsed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON解析済みの応答を検証しテーマと資料の両方をプロンプトへ含める。"""
    fake = _FakeLLM({"kind": "root", "title": "地域交流", "body": "交流を広げる。"})
    monkeypatch.setattr(nlp.llm, "generate_json", fake.generate_json)

    result = generate_initial_mindmap("地域の課題を整理する", theme="地域交流")

    assert result["children"] == []
    prompt, _, hint, timeout = fake.calls[0]
    assert "地域の課題を整理する" in prompt
    assert "地域交流" in prompt
    assert "4〜6" in prompt and "2〜4" in prompt and "深さ3" in prompt
    assert "MECE" in prompt and "1〜2文" in prompt
    assert hint and timeout == 45


@pytest.mark.parametrize("content", [None, [], {"kind": "idea", "title": "不正なルート"}])
def test_initial_mindmap_rejects_invalid_tree(
    content: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ルートの構造が不正な応答は保存へ渡さず拒否する。"""
    monkeypatch.setattr(nlp.llm, "generate_json", _FakeLLM(content).generate_json)

    with pytest.raises(RuntimeError, match="マインドマップ生成"):
        generate_initial_mindmap(theme="テーマのみ")


@pytest.mark.parametrize(
    ("mode", "kind"),
    [("related_ideas", "idea"), ("task", "task"), ("related_tasks", "task"),
     ("detail_steps", "detail"), ("questions", "note"),
     ("perspectives", "note"), ("combine", "idea")],
)
def test_expand_node_requests_mode_and_context(
    monkeypatch: pytest.MonkeyPatch, mode: str, kind: str
) -> None:
    """各モードの種類とパス・兄弟・既存子を共通生成層へ伝える。"""
    response: List[Dict[str, Any]] = [{"kind": kind, "title": "新しい案", "body": "説明。"}]
    fake = _FakeLLM(response)
    monkeypatch.setattr(nlp.llm, "generate_json", fake.generate_json)

    result = expand_node(
        "親", "親の説明", "全体の要約", mode,
        path=[{"title": "全体テーマ"}], siblings=[{"title": "兄弟の切り口"}],
        existing_children=[{"title": "既存の案"}],
    )

    assert result[0]["kind"] == kind
    prompt, _, hint, timeout = fake.calls[0]
    assert all(value in prompt for value in ("全体テーマ", "兄弟の切り口", "既存の案", "全体の要約"))
    assert f"kindは必ず{kind}" in prompt
    assert hint and timeout == 45


def test_initial_mindmap_rejects_invalid_model_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """共通層でJSON解析に失敗した場合も日本語エラーを維持する。"""
    fake = _FakeLLM(error=ValueError("有効なJSON形式ではありません。"))
    monkeypatch.setattr(nlp.llm, "generate_json", fake.generate_json)

    with pytest.raises(RuntimeError, match="有効なJSON形式"):
        generate_initial_mindmap("Source")


def test_expand_node_rejects_invalid_model_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """不正な子ノードは仮ルートの検証で拒否する。"""
    fake = _FakeLLM([{"kind": "idea", "title": "", "body": "説明"}])
    monkeypatch.setattr(nlp.llm, "generate_json", fake.generate_json)

    with pytest.raises(RuntimeError, match="ノード展開"):
        expand_node("Idea", "Details", "Context", "related_ideas")
