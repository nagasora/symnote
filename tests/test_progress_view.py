"""Streamlit interaction checks for the Japanese progress pages."""

from __future__ import annotations

from streamlit.testing.v1 import AppTest


def _safe_local_app(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "ui-progress.db"))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_SECRET_PATH", raising=False)
    return AppTest.from_file("src/symnote/app.py", default_timeout=15)


def test_first_project_goal_and_task_flow_is_interactive(monkeypatch, tmp_path) -> None:
    app = _safe_local_app(monkeypatch, tmp_path).run()
    app.sidebar.radio[0].set_value("プロジェクト").run()

    assert any("最初のプロジェクト" in element.value for element in app.info)
    app.text_input[0].set_value("合成デモ企画")
    app.button[0].click().run()

    assert any(element.label == "目標" for element in app.text_input)
    app.text_input[0].set_value("資料を提出する")
    app.text_input[1].set_value("図表を確認する")
    app.button[0].click().run()

    assert any("資料を提出する" in element.value for element in app.markdown)
    task_title = next(element for element in app.text_input if element.label == "ToDo")
    task_title.set_value("図のキャプションを書く")
    task_button = next(element for element in app.button if element.label == "ToDo を追加")
    task_button.click().run()

    assert not app.exception
    assert any("図のキャプションを書く" in element.value for element in app.markdown)
    assert any("図表を確認する" in element.value for element in app.text)


def test_review_page_explains_inert_import_and_claim_status(monkeypatch, tmp_path) -> None:
    from symnote.core import progress
    from symnote.core.db import init_db

    monkeypatch.setenv("DB_PATH", str(tmp_path / "ui-review.db"))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    init_db()
    project = progress.create_project("合成レビュー例")
    goal_id = progress.create_goal(
        project["id"], "報告を確認する", status="active", next_action="証拠を確認する"
    )
    task_id = progress.create_project_task(project["id"], goal_id, "合成タスク")
    report = {
        "schema": progress.EVENT_SCHEMA,
        "event_id": "12345678-1234-4234-8234-123456789abc",
        "reported_at": "2026-10-02T12:00:00Z",
        "project_key": project["project_key"],
        "target": {"kind": "task", "id": task_id},
        "expected_revision": 0,
        "claimed_status": "completed",
        "source": {
            "repo": "https://example.test/synthetic",
            "branch": "demo",
            "commit": "a" * 40,
            "worktree": "",
        },
        "artifact_ref": "artifacts/demo.txt",
        "test_ref": "tests/demo.log",
        "claimed_test_result": "passed",
    }
    progress.import_progress_events([report])

    app = AppTest.from_file("src/symnote/app.py", default_timeout=15).run()
    app.sidebar.radio[0].set_value("進捗レビュー").run()

    assert not app.exception
    visible_text = "\n".join(
        str(element.value) for element in [*app.markdown, *app.caption, *app.text]
    )
    assert "コマンドを実行せず" in visible_text
    assert "合成タスク" in visible_text
    assert "完了申告 / 未検証" in visible_text
    assert app.get("file_uploader")
    assert app.get("download_button")
