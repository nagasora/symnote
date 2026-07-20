from __future__ import annotations

import datetime as dt

from streamlit.testing.v1 import AppTest

from symnote.app import _task_overview


def test_task_overview_separates_active_today_overdue_and_done() -> None:
    today = dt.date(2026, 7, 20)
    tasks = [
        {"id": 1, "status": "today", "due_date": None},
        {"id": 2, "status": "week", "due_date": "2026-07-20"},
        {"id": 3, "status": "inbox", "due_date": "2026-07-19"},
        {"id": 4, "status": "done", "due_date": "2026-07-19"},
    ]

    assert _task_overview(tasks, today) == {
        "active": 3,
        "today": 2,
        "overdue": 1,
        "done": 1,
    }


def test_app_starts_on_today_view_with_a_temporary_database(monkeypatch, tmp_path) -> None:
    """The primary local workflow should render without touching a user database."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "ui-test.db"))
    app = AppTest.from_file("src/symnote/app.py", default_timeout=10)
    app.run()

    assert not app.exception
    assert app.sidebar.radio[0].value == "今日"
    assert any("今日" in heading.value for heading in app.markdown)
