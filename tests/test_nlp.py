from datetime import date

from symnote.core.nlp import suggest_today_tasks


def test_today_suggestions_use_due_date_not_creation_date() -> None:
    """A task created today is not automatically treated as due today."""
    suggestions = suggest_today_tasks(
        [
            {
                "id": 1,
                "kind": "task",
                "status": "inbox",
                "raw_text": "Plan next month's review",
                "date": "2026-07-19",
                "due_date": "2026-07-29",
                "importance": 3,
                "urgency": 3,
                "effort": "medium",
                "energy": "mid",
            }
        ],
        today=date(2026, 7, 19),
    )

    assert suggestions[0].due == "2026-07-29"
    assert "期限が今日" not in suggestions[0].reason


def test_today_suggestions_recognize_overdue_due_date() -> None:
    suggestions = suggest_today_tasks(
        [
            {
                "id": 2,
                "kind": "task",
                "status": "week",
                "raw_text": "Submit report",
                "date": "2026-07-10",
                "due_date": "2026-07-18",
                "importance": 3,
                "urgency": 3,
                "effort": "medium",
                "energy": "mid",
            }
        ],
        today=date(2026, 7, 19),
    )

    assert suggestions[0].due == "2026-07-18"
    assert "期限を過ぎている" in suggestions[0].reason
