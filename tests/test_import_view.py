"""自動取り込み画面が、ボタンを押すまで AI や Gmail を呼ばず、未接続・キー無しでも落ちないことを保証するテスト。"""

from __future__ import annotations

from typing import Any, List

import pytest
from streamlit.testing.v1 import AppTest

from symnote.core import gmail_import, llm
from symnote.core.candidates import list_candidates
from symnote.core.db import fetch_tasks, init_db
from symnote.core.task_extraction import ExtractionResult


def _script() -> None:
    """画面を単体で描画する AppTest 用スクリプト。"""
    from symnote.views.import_view import render_auto_import

    render_auto_import()


def _button(app: AppTest, label: str) -> Any:
    """ラベルが一致するボタンを返す。"""
    return next(button for button in app.button if button.label == label)


@pytest.fixture()
def ai_calls(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """llm.generate_json を記録用の代役に差し替え、渡されたプロンプトを返す。"""
    init_db()
    monkeypatch.setenv("LLM_API_KEY", "dummy-key")
    calls: List[str] = []

    def fake_generate_json(prompt: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(prompt)
        return {"candidates": [{
            "source_ref": prompt.split("BEGIN_UNTRUSTED_DATA_JSON")[1].split('"source_ref": "')[1].split('"')[0],
            "title": "請求書を出す", "details": "", "excerpt": "請求書を出す",
            "due_date": None, "due_time": None, "confidence": 0.9,
        }]}

    monkeypatch.setattr(llm, "generate_json", fake_generate_json)
    return calls


def test_reruns_do_not_call_ai_until_paste_button_is_pressed(
    monkeypatch: pytest.MonkeyPatch, ai_calls: List[str]
) -> None:
    """入力や再実行では AI を呼ばず、ボタン押下時に 1 回だけ呼んで承認待ちに入れる。"""
    monkeypatch.setenv("SYMNOTE_TEST_MODE", "1")
    app = AppTest.from_function(_script, default_timeout=30)
    app.run()
    app.text_area(key="auto_import_chatgpt_text").input("user: 明日までに請求書を出す").run()
    app.run()

    assert not app.exception
    assert ai_calls == []

    _button(app, "貼り付けた会話から ToDo を探す").click().run()

    assert len(ai_calls) == 1
    assert any("候補を 1 件追加しました" in block.value for block in app.success)
    assert [row["title"] for row in list_candidates("pending")] == ["請求書を出す"]
    assert fetch_tasks() == []

    app.run()
    assert len(ai_calls) == 1
    assert any("候補を 1 件追加しました" in block.value for block in app.success)


def test_test_mode_shows_gmail_notice_without_touching_keyring(
    monkeypatch: pytest.MonkeyPatch, ai_calls: List[str]
) -> None:
    """テスト環境では Gmail の接続ボタンを出さず、資格情報ストアにも触れない。"""
    monkeypatch.setenv("SYMNOTE_TEST_MODE", "1")
    monkeypatch.setattr(gmail_import, "_keyring", lambda: pytest.fail("keyring に触れてはいけない"))
    app = AppTest.from_function(_script, default_timeout=30)
    app.run()

    assert not app.exception
    assert any("テスト環境では Gmail に接続しません" in block.value for block in app.info)
    assert all("Gmail に接続する" not in button.label for button in app.button)


def test_missing_api_key_and_unconnected_gmail_do_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """API キー無し・Gmail 未接続でも画面は描画され、抽出ボタンは日本語の案内を返す。"""
    init_db()
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.delenv("SYMNOTE_TEST_MODE", raising=False)
    monkeypatch.setattr(gmail_import, "is_connected", lambda: False)
    app = AppTest.from_function(_script, default_timeout=30)
    app.run()

    assert not app.exception
    assert any("LLM_API_KEY" in block.value for block in app.warning)
    assert any(button.label == "Gmail に接続する（読み取り専用）" for button in app.button)

    app.text_area(key="auto_import_chatgpt_text").input("請求書を出す").run()
    _button(app, "貼り付けた会話から ToDo を探す").click().run()

    assert not app.exception
    assert any("API キー" in block.value for block in app.warning)
    assert list_candidates("pending") == []


def test_gmail_button_runs_import_only_when_pressed(monkeypatch: pytest.MonkeyPatch, ai_calls: List[str]) -> None:
    """接続済みの Gmail は、ボタンを押したときだけ取り込みを実行して件数を表示する。"""
    monkeypatch.delenv("SYMNOTE_TEST_MODE", raising=False)
    monkeypatch.setattr(gmail_import, "is_connected", lambda: True)
    runs: List[tuple] = []

    def fake_import(days: int, max_messages: int) -> ExtractionResult:
        runs.append((days, max_messages))
        return ExtractionResult(created_ids=[1, 2], processed_refs=["gmail:a:1"])

    monkeypatch.setattr(gmail_import, "import_recent_messages", fake_import)
    app = AppTest.from_function(_script, default_timeout=30)
    app.run()
    app.run()

    assert not app.exception and runs == []

    _button(app, "直近7日のメールから ToDo を探す").click().run()

    assert runs == [(7, 20)]
    assert any("候補を 2 件追加しました。下の承認待ちで確認してください。" in b.value for b in app.success)
    assert ai_calls == []


def test_empty_file_button_shows_guidance(monkeypatch: pytest.MonkeyPatch, ai_calls: List[str]) -> None:
    """ファイル未選択で抽出ボタンを押しても AI を呼ばず、選択を促す。"""
    monkeypatch.setenv("SYMNOTE_TEST_MODE", "1")
    app = AppTest.from_function(_script, default_timeout=30)
    app.run()

    _button(app, "ファイルの会話から ToDo を探す").click().run()

    assert not app.exception and ai_calls == []
    assert any("ZIP または conversations.json を選んで" in block.value for block in app.warning)
