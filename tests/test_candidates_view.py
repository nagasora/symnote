"""候補の承認画面から承認・却下できることを保証する UI テスト。"""

from __future__ import annotations

from streamlit.testing.v1 import AppTest

from symnote.core.candidates import list_candidates, propose_candidates, validate_candidate
from symnote.core.db import fetch_tasks, init_db


def _open_candidates_page(monkeypatch, tmp_path) -> AppTest:
    """候補 2 件を用意した一時 DB で承認画面を開く。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "view.db"))
    init_db()
    propose_candidates(
        [
            validate_candidate(source="gmail", source_ref="m1", title="請求書を確認する"),
            validate_candidate(source="slack", source_ref="s1", title="![x](https://evil/p.png)"),
        ]
    )
    app = AppTest.from_file("src/symnote/app.py", default_timeout=15)
    app.run()
    app.sidebar.radio[0].set_value("候補の承認").run()
    return app


def test_sidebar_shows_pending_count_and_page_renders(monkeypatch, tmp_path) -> None:
    """承認待ちの件数がサイドバーに出て、候補のタイトルが入力欄に表示される。"""
    app = _open_candidates_page(monkeypatch, tmp_path)

    assert not app.exception
    assert {field.value for field in app.text_input if field.label == "タイトル"} == {
        "請求書を確認する",
        "![x](https://evil/p.png)",
    }
    assert not any("evil" in block.value for block in app.markdown)


def test_approve_button_turns_candidate_into_task(monkeypatch, tmp_path) -> None:
    """承認ボタンで候補がタスクになり、承認待ちから消える。"""
    app = _open_candidates_page(monkeypatch, tmp_path)
    target = next(
        index
        for index, field in enumerate(f for f in app.text_input if f.label == "タイトル")
        if field.value == "請求書を確認する"
    )
    approve_buttons = [b for b in app.button if b.label == "承認してタスクに追加"]

    approve_buttons[target].click().run()

    assert not app.exception
    assert [task["tags"] for task in fetch_tasks()] == ["請求書を確認する"]
    assert [row["title"] for row in list_candidates("pending")] == ["![x](https://evil/p.png)"]
