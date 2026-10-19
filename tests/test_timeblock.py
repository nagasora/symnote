"""一時DBとGoogle APIモックで時間割の配置・承認・再送の契約を検証する。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import pytest

from symnote.core import llm, timeblock
from symnote.core.calendar_settings import set_setting
from symnote.core.db import connection, fetch_tasks, init_db, insert_task
from symnote.core.timeblock import Block, free_slots, pack_tasks
from symnote.views.timeblock_view import _timeline_html

DAY = "2026-10-16"


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """実データを使わず独立したSQLiteファイルを作る。"""
    path = tmp_path / "timeblock.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("GOOGLE_CALENDAR_ID", "primary")
    monkeypatch.setenv("GOOGLE_CALENDAR_TIMEZONE", "Asia/Tokyo")
    init_db()
    return path


@pytest.fixture
def export_database(database: Path) -> Path:
    """書き出し対象となる実際の未完了タスクを登録する。"""
    insert_task("資料作成", tags="資料作成", status="today")
    return database


def task(identity: int, **fields: Any) -> Dict[str, Any]:
    """見積もり付きの今日のタスクを生成する。"""
    return {
        "id": identity,
        "kind": "task",
        "status": "today",
        "title": f"タスク{identity}",
        "estimate_minutes": 30,
        **fields,
    }


def fake_service() -> MagicMock:
    """同じIDの再送を409にし、応答喪失後もリモートの予定を保持する。"""
    service = MagicMock()
    service.remote = {}
    service.lose_response = False
    service.lose_delete_response = False

    def list_events(**kwargs: Any) -> MagicMock:
        """指定されたカレンダーの最新予定を返す。"""
        request = MagicMock()
        request.execute.return_value = {
            "items": [
                body
                for (calendar, _), body in service.remote.items()
                if calendar == kwargs["calendarId"]
            ]
        }
        return request

    def delete(*, calendarId: str, eventId: str) -> MagicMock:
        """削除成功後の応答喪失も模倣する。"""
        request = MagicMock()

        def execute() -> Dict[str, Any]:
            """実際に削除し、不在なら404を返す。"""
            if service.remote.pop((calendarId, eventId), None) is None:
                error = RuntimeError("not found")
                error.resp = MagicMock(status=404)
                raise error
            if service.lose_delete_response:
                service.lose_delete_response = False
                raise TimeoutError("delete response lost")
            return {}

        request.execute.side_effect = execute
        return request

    def insert(*, calendarId: str, body: Dict[str, Any]) -> MagicMock:
        """execute時に作成し、必要なら作成後の応答だけ失わせる。"""
        request = MagicMock()

        def execute() -> Dict[str, Any]:
            """リモートの変更とHTTP競合を模倣する。"""
            key = (calendarId, body["id"])
            if key in service.remote:
                error = RuntimeError("conflict")
                error.resp = MagicMock(status=409)
                raise error
            service.remote[key] = body
            if service.lose_response:
                service.lose_response = False
                raise TimeoutError("response lost")
            return body

        request.execute.side_effect = execute
        return request

    def get(*, calendarId: str, eventId: str) -> MagicMock:
        """IDからリモートに残っている予定を読み戻す。"""
        request = MagicMock()
        request.execute.return_value = service.remote[(calendarId, eventId)]
        return request

    service.events.return_value.list.side_effect = list_events
    service.events.return_value.delete.side_effect = delete
    service.events.return_value.insert.side_effect = insert
    service.events.return_value.get.side_effect = get
    return service


def test_free_slots_merges_overlaps_and_rounds_inward() -> None:
    """重なる予定を結合し、空き時間を予定へ食い込まない5分刻みにする。"""
    events = [{"start": "10:02", "end": "11:01"}, {"start": "10:30", "end": "12:03"}]
    assert free_slots(events) == [("09:00", "10:00"), ("12:05", "19:00")]


def test_free_slots_clips_before_now_and_day_bounds() -> None:
    """現在時刻の秒を切り上げ、作業時間外の予定を切り詰める。"""
    events = [{"start": "08:00", "end": "09:10"}, {"start": "18:02", "end": "21:00"}]
    assert free_slots(events, now=dt.datetime(2026, 10, 16, 10, 5, 1)) == [("10:10", "18:00")]
    assert free_slots([], now=dt.datetime(2026, 10, 16, 20)) == []
    assert free_slots([]) == [("09:00", "19:00")]


def test_free_slots_all_day_and_midnight() -> None:
    """終日予定は全時間を塞ぎ、日跨ぎ予定も開始以降を塞ぐ。"""
    assert free_slots([{"start": "終日", "end": ""}]) == []
    assert free_slots([{"start": "18:00", "end": "00:00"}]) == [("09:00", "18:00")]
    assert free_slots([{"start": "終日", "status": "cancelled"}]) == [("09:00", "19:00")]
    assert free_slots([{"start": "終日", "transparency": "transparent"}]) == [("09:00", "19:00")]


@pytest.mark.parametrize("events", [[{"start": "invalid", "end": "12:00"}], [{"start": "12:00"}]])
def test_free_slots_rejects_unknown_busy_time(events: List[Dict[str, Any]]) -> None:
    """壊れた予定を空き時間として扱わない。"""
    with pytest.raises((ValueError, TypeError)):
        free_slots(events)


def test_pack_priority_buffer_default_and_overflow() -> None:
    """優先順位とバッファを守り、未見積もりは30分、長いタスクは分割しない。"""
    tasks = [
        task(1, due_date="2026-10-17"),
        task(2, due_date=DAY, importance=5, urgency=2, estimate_minutes=None),
        task(3, due_date=DAY, importance=5, urgency=4, estimate_minutes=67),
        task(4, due_date=DAY, importance=2),
    ]
    blocks = pack_tasks(tasks, [("09:00", "10:05"), ("11:00", "11:30")])
    assert [b.task_id for b in blocks] == [3, 2, 4, 1]
    assert blocks[0].overflow
    assert (blocks[1].start_time, blocks[1].end_time) == ("09:00", "09:30")
    assert blocks[1].estimated_default
    assert blocks[2].start_time == "09:35"
    assert blocks[3].start_time == "11:00"


def test_pack_does_not_split_or_duplicate_tasks() -> None:
    """合計時間が足りても単一枠に入らないタスクはoverflowにし、重複IDは一度だけ使う。"""
    blocks = pack_tasks(
        [task(1, estimate_minutes=60), task(2), task(2)], [("09:00", "09:30"), ("10:00", "10:30")]
    )
    assert len(blocks) == 2
    assert blocks[0].overflow and not blocks[1].overflow
    assert pack_tasks([task(1, estimate_minutes=31)], [("09:00", "10:00")])[0].end_time == "09:35"


def test_pack_rejects_overlapping_slots_and_negative_buffer() -> None:
    """不正な入力枠でタスクの重複予約を作らない。"""
    with pytest.raises(ValueError):
        pack_tasks([task(1)], [("09:00", "10:00"), ("09:30", "10:30")])
    with pytest.raises(ValueError):
        pack_tasks([task(1)], [("09:00", "10:00")], -1)


def test_today_selection_prefers_today_then_due_tasks() -> None:
    """todayがあれば優先し、なければ未完了の期限切れと今日期限だけを採用する。"""
    tasks = [
        task(1),
        task(2, status="inbox", due_date="2026-10-15"),
        task(3, status="done", due_date=DAY),
        task(4, status="week", due_date="2026-10-17"),
        task(5, kind="note"),
        task(6, status="week", due_date=DAY),
    ]
    day = dt.date.fromisoformat(DAY)
    assert [t["id"] for t in timeblock.select_today_tasks(tasks, day)] == [1]
    assert [t["id"] for t in timeblock.select_today_tasks(tasks[1:], day)] == [2, 6]


def test_ai_only_reorders_known_ids_and_keeps_omitted_tasks() -> None:
    """未知・重複・真偽値IDを除外し、AIに時刻やタイトルを変更させない。"""
    generator = MagicMock(return_value={"task_ids": [999, True, "1", 2, 2]})
    plan = timeblock.propose_timeblocks(
        [task(1), task(2)], [], dt.date.fromisoformat(DAY), generate_json=generator
    )
    assert [b.task_id for b in plan.blocks] == [2, 1]
    assert plan.blocks[0].title == "タスク2"
    assert plan.blocks[0].start_time == "09:00"
    assert plan.notice


@pytest.mark.parametrize("result", [{"task_ids": [999]}, None, {"task_ids": "1"}])
def test_ai_invalid_response_falls_back(result: Any) -> None:
    """有効IDがない応答ではルール順へ戻り、通知する。"""
    plan = timeblock.propose_timeblocks(
        [task(2), task(1)],
        [],
        dt.date.fromisoformat(DAY),
        generate_json=MagicMock(return_value=result),
    )
    assert [b.task_id for b in plan.blocks] == [1, 2]
    assert plan.notice


@pytest.mark.parametrize("error", [llm.LLMUnavailableError("offline"), TimeoutError("timeout")])
def test_ai_failure_does_not_break_planning(error: Exception) -> None:
    """AI障害時もローカルの配置を返す。"""
    plan = timeblock.propose_timeblocks(
        [task(1)], [], dt.date.fromisoformat(DAY), generate_json=MagicMock(side_effect=error)
    )
    assert len(plan.blocks) == 1
    assert plan.notice


def test_plan_reports_overflow_without_ai_call_for_empty_tasks() -> None:
    """終日予定の場合は全タスクをoverflowにし、候補なしではAIを呼ばない。"""
    generator = MagicMock()
    empty = timeblock.propose_timeblocks(
        [], [], dt.date.fromisoformat(DAY), generate_json=generator
    )
    generator.assert_not_called()
    assert not empty.blocks
    plan = timeblock.propose_timeblocks(
        [task(1)], [{"start": "終日"}], dt.date.fromisoformat(DAY), use_ai=False
    )
    assert not plan.blocks and len(plan.overflow) == 1


def test_save_replaces_planned_preserves_exported_and_other_days(database: Path) -> None:
    """同日の未送信枠だけを置換し、書き出し済みと他の日の枠は残す。"""
    timeblock.save_blocks(
        DAY, [Block(1, "送信済み", "09:00", "09:30"), Block(2, "古い提案", "10:00", "10:30")]
    )
    timeblock.save_blocks("2026-10-17", [Block(3, "明日", "09:00", "09:30")])
    with connection() as conn, conn:
        conn.execute("UPDATE time_blocks SET status='exported' WHERE task_id=1")
    timeblock.save_blocks(DAY, [Block(4, "新しい提案", "11:00", "11:30")])
    assert [(b["task_id"], b["status"]) for b in timeblock.list_blocks(DAY)] == [
        (1, "exported"),
        (4, "planned"),
    ]
    assert len(timeblock.list_blocks("2026-10-17")) == 1


def test_save_rejects_overlap_atomically(database: Path) -> None:
    """重複時刻を拒否し、元の承認済み時間割を変更しない。"""
    timeblock.save_blocks(DAY, [Block(1, "元", "09:00", "09:30")])
    with pytest.raises(ValueError):
        timeblock.save_blocks(
            DAY, [Block(2, "重複1", "10:00", "11:00"), Block(3, "重複2", "10:30", "11:30")]
        )
    assert timeblock.list_blocks(DAY)[0]["title"] == "元"


def test_export_is_idempotent(export_database: Path) -> None:
    """2回書き出しても予定は1件だけで、送信済みではAPIを呼ばない。"""
    timeblock.save_blocks(DAY, [Block(1, "資料作成", "09:00", "09:30")])
    service = fake_service()
    factory = MagicMock(return_value=service)
    assert timeblock.export_blocks_to_calendar(DAY, factory).synced == 1
    assert timeblock.export_blocks_to_calendar(DAY, factory).synced == 0
    factory.assert_called_once()
    assert len(service.remote) == 1
    body = next(iter(service.remote.values()))
    assert body["summary"] == "SymNote: 資料作成"
    assert body["start"]["timeZone"] == "Asia/Tokyo"
    assert timeblock.list_blocks(DAY)[0]["status"] == "exported"


def test_response_loss_keeps_id_and_retry_recovers(export_database: Path) -> None:
    """作成後の応答喪失でも予約IDを残し、再試行で同じ予定の承認を回復する。"""
    timeblock.save_blocks(DAY, [Block(1, "資料", "09:00", "09:30")])
    service = fake_service()
    service.lose_response = True
    assert timeblock.export_blocks_to_calendar(DAY, lambda: service).failed == 1
    pending_id = timeblock.list_blocks(DAY)[0]["calendar_event_id"]
    assert pending_id
    with pytest.raises(ValueError, match="未確定"):
        timeblock.save_blocks(DAY, [Block(2, "別の提案", "10:00", "10:30")])
    assert timeblock.export_blocks_to_calendar(DAY, lambda: service).synced == 1
    assert len(service.remote) == 1
    assert timeblock.list_blocks(DAY)[0]["calendar_event_id"] == pending_id


def test_id_is_persisted_before_network(export_database: Path) -> None:
    """APIが受け取るIDは送信前にDBへコミット済みである。"""
    timeblock.save_blocks(DAY, [Block(1, "資料", "09:00", "09:30")])
    service = fake_service()
    original = service.events.return_value.insert.side_effect

    def checked_insert(*, calendarId: str, body: Dict[str, Any]) -> MagicMock:
        """別の接続から永続化済みIDを照合する。"""
        assert timeblock.list_blocks(DAY)[0]["calendar_event_id"] == body["id"]
        return original(calendarId=calendarId, body=body)

    service.events.return_value.insert.side_effect = checked_insert
    assert timeblock.export_blocks_to_calendar(DAY, lambda: service).synced == 1


def test_pending_export_does_not_move_to_new_calendar(export_database: Path) -> None:
    """応答未確定後に送信先を変更しても別カレンダーへ予定を複製しない。"""
    timeblock.save_blocks(DAY, [Block(1, "資料", "09:00", "09:30")])
    service = fake_service()
    service.lose_response = True
    timeblock.export_blocks_to_calendar(DAY, lambda: service)
    set_setting("calendar_id", "other@example.com")
    assert timeblock.export_blocks_to_calendar(DAY, lambda: service).failed == 1
    assert len(service.remote) == 1
    set_setting("calendar_id", "primary")
    assert timeblock.export_blocks_to_calendar(DAY, lambda: service).synced == 1


def test_export_connection_failure_is_reported(export_database: Path) -> None:
    """未接続やネットワーク障害でも承認済み時間割を保持する。"""
    timeblock.save_blocks(DAY, [Block(1, "資料", "09:00", "09:30")])
    result = timeblock.export_blocks_to_calendar(
        DAY, MagicMock(side_effect=RuntimeError("offline"))
    )
    assert result.failed == result.pending == 1
    assert timeblock.list_blocks(DAY)[0]["status"] == "planned"


def test_timeline_escapes_titles_and_orders_rows() -> None:
    """予定とタスクのHTMLを無害化し、時刻順に表示する。"""
    html = _timeline_html(
        [{"start": "10:00", "end": "11:00", "summary": "<script>alert(1)</script>"}],
        [
            {
                "start_time": "09:00",
                "end_time": "09:30",
                "title": '<img src=x onerror="alert(1)">',
                "estimated_default": True,
            }
        ],
    )
    assert "<script>" not in html and "<img" not in html
    assert "&lt;script&gt;" in html and "&lt;img" in html
    assert html.index("09:00") < html.index("10:00")
    assert "見積もり 30 分で仮置き" in html


def test_view_preview_approval_and_disconnected_export(
    export_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """プレビューだけでは保存せず、承認後に保存し、未接続時には出力を無効化する。"""
    from streamlit.testing.v1 import AppTest

    from symnote.views import timeblock_view

    monkeypatch.setattr(timeblock_view, "is_connected", lambda: False)
    monkeypatch.setattr(
        timeblock_view,
        "propose_timeblocks",
        lambda *args, **kwargs: timeblock.TimeblockPlan(
            [Block(1, "<img src=x>", "09:00", "09:30", estimated_default=True)],
            [Block(2, "入りきらない作業", overflow=True)],
        ),
    )
    app = AppTest.from_string(
        "from symnote.views.timeblock_view import render_timeblock\n"
        'render_timeblock([{"id":1,"kind":"task","status":"today","title":"資料"}], [])'
    ).run()
    assert not app.exception
    app.button(key="timeblock_make").click().run()
    assert not app.exception
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM time_blocks").fetchone()[0] == 0
    assert any("&lt;img src=x&gt;" in markdown.value for markdown in app.markdown)
    assert any("今日は入りきらない" in markdown.value for markdown in app.markdown)
    app.button(key="timeblock_accept").click().run()
    assert not app.exception
    assert app.button(key="timeblock_export").disabled
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM time_blocks").fetchone()[0] == 1


def calendar_event(identity: str, start: str, end: str) -> Dict[str, Any]:
    """オフセット付きのGoogle予定を作る。"""
    return {
        "id": identity,
        "summary": "会議",
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }


def test_real_task_row_title_survives_ai_save_and_export(export_database: Path) -> None:
    """fetch_tasksのtagsをAI入力、配置、保存、予定名で一貫して使う。"""
    rows = fetch_tasks(statuses=["today"])
    assert "title" not in rows[0] and "content" not in rows[0]
    generator = MagicMock(return_value={"task_ids": [rows[0]["id"]]})
    plan = timeblock.propose_timeblocks(
        rows, [], dt.date.fromisoformat(DAY), generate_json=generator
    )
    assert "資料作成" in generator.call_args.args[0]
    assert plan.blocks[0].title == "資料作成"
    timeblock.save_blocks(DAY, plan.blocks)
    assert timeblock.list_blocks(DAY)[0]["title"] == "資料作成"
    service = fake_service()
    assert timeblock.export_blocks_to_calendar(DAY, lambda: service).synced == 1
    assert next(iter(service.remote.values()))["summary"] == "SymNote: 資料作成"


def test_task_title_falls_back_to_first_raw_text_line() -> None:
    """空白tagsではraw_textの先頭行をタイトルにする。"""
    row = task(1, tags="  ", raw_text="本文のタイトル\n詳細")
    assert timeblock.task_display_title(row) == "本文のタイトル"
    assert pack_tasks([row], [("09:00", "10:00")])[0].title == "本文のタイトル"


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("2026-10-15T23:00:00+09:00", "2026-10-16T10:00:00+09:00", [("10:00", "19:00")]),
        ("2026-10-16T18:00:00+09:00", "2026-10-17T10:00:00+09:00", [("09:00", "18:00")]),
        ("2026-10-15T23:00:00+09:00", "2026-10-17T10:00:00+09:00", []),
        ("2026-10-15T14:00:00+00:00", "2026-10-16T01:00:00+00:00", [("10:00", "19:00")]),
    ],
)
def test_cross_day_events_are_clipped_before_minutes(
    start: str,
    end: str,
    expected: List[Tuple[str, str]],
) -> None:
    """前日開始、翌日終了、複数日、異なるUTCオフセットを対象日でクリップする。"""
    events = timeblock.normalize_calendar_events(
        [calendar_event("meeting", start, end)], dt.date.fromisoformat(DAY), "Asia/Tokyo"
    )
    assert free_slots(events) == expected
    assert all(0 <= e["start_minute"] < e["end_minute"] <= 1440 for e in events)


def test_multiday_all_day_event_is_busy() -> None:
    """排他的終了日の終日予定を対象日の境界に正規化する。"""
    events = timeblock.normalize_calendar_events(
        [{"start": {"date": "2026-10-15"}, "end": {"date": "2026-10-17"}}],
        dt.date.fromisoformat(DAY),
        "Asia/Tokyo",
    )
    assert events[0]["start_minute"] == 0 and events[0]["end_minute"] == 1440
    assert free_slots(events) == []


@pytest.mark.parametrize("offline", [True, False])
def test_today_events_preserves_fetch_state_and_cross_day_boundaries(
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
    offline: bool,
) -> None:
    """今日の計画向け予定でも、日跨ぎ境界と取得済み・オフライン状態を保つ。"""
    from symnote.core import calendar_events, google_calendar
    from symnote.views.today_ai_view import today_events_for_plan

    monkeypatch.setattr(google_calendar, "is_connected", lambda: True)
    monkeypatch.setattr(
        google_calendar, "today_in_timezone", lambda timezone: dt.date.fromisoformat(DAY)
    )
    result = calendar_events.EventFetchResult(
        [calendar_event("cross", "2026-10-15T23:00:00+09:00", "2026-10-16T10:00:00+09:00")],
        fetched_at="2026-10-16T00:00:00+00:00",
        offline=offline,
    )
    monkeypatch.setattr(calendar_events, "fetch_events", MagicMock(return_value=result))
    events = today_events_for_plan()
    assert isinstance(events, list)
    assert events.confirmed is not offline
    assert events[0]["start"] == "00:00" and events[0]["end"] == "10:00"
    if offline:
        assert "空き時間未確認" in events.notice


def test_unconnected_calendar_is_not_confirmed_empty(
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未接続を取得成功の予定なしと区別する。"""
    from symnote.core import google_calendar
    from symnote.views.today_ai_view import today_events_for_plan

    monkeypatch.setattr(google_calendar, "is_connected", lambda: False)
    events = today_events_for_plan()
    assert events == [] and not events.confirmed
    assert "空き時間未確認" in events.notice
    plan = timeblock.propose_timeblocks([task(1)], events, dt.date.fromisoformat(DAY), use_ai=False)
    assert not plan.calendar_confirmed and "空き時間未確認" in plan.notice


def test_export_refreshes_and_refuses_new_conflict(export_database: Path) -> None:
    """承認後に追加された予定と衝突した枠は送信せず、再取得はキャッシュを使わない。"""
    timeblock.save_blocks(DAY, [Block(1, "資料作成", "09:00", "09:30")])
    service = fake_service()
    service.remote[("primary", "meeting")] = calendar_event(
        "meeting", "2026-10-15T23:00:00+09:00", "2026-10-16T09:10:00+09:00"
    )
    result = timeblock.export_blocks_to_calendar(DAY, lambda: service)
    assert result.failed == 1 and "衝突" in result.message
    service.events.return_value.list.assert_called_once()
    service.events.return_value.insert.assert_not_called()
    assert len(service.remote) == 1
    assert timeblock.list_blocks(DAY)[0]["calendar_event_id"] is None


def test_export_refuses_offline_even_with_cached_empty_calendar(export_database: Path) -> None:
    """予定なしのキャッシュがあっても最新取得に失敗すれば送信しない。"""
    timeblock.save_blocks(DAY, [Block(1, "資料", "09:00", "09:30")])
    service = fake_service()
    timeblock.fetch_events(
        dt.date.fromisoformat(DAY), dt.date(2026, 10, 17), service_factory=lambda: service
    )
    service.events.return_value.list.side_effect = TimeoutError("offline")
    result = timeblock.export_blocks_to_calendar(DAY, lambda: service)
    assert result.failed == 1 and "空き時間未確認" in result.message
    service.events.return_value.insert.assert_not_called()
    assert timeblock.list_blocks(DAY)[0]["status"] == "planned"


def test_cancel_exported_block_offline_then_retry(export_database: Path) -> None:
    """書き出し済み枠の取消をオフラインでも保存し、接続後に同じ予定を削除する。"""
    timeblock.save_blocks(DAY, [Block(1, "資料", "09:00", "09:30")])
    service = fake_service()
    timeblock.export_blocks_to_calendar(DAY, lambda: service)
    block = timeblock.list_blocks(DAY)[0]
    assert timeblock.cancel_block(block["id"])
    result = timeblock.flush_cancelled_blocks(MagicMock(side_effect=TimeoutError("offline")))
    assert result.failed == 1
    cancelled = timeblock.list_blocks(DAY)[0]
    assert (
        cancelled["status"] == "cancelled"
        and cancelled["calendar_event_id"] == block["calendar_event_id"]
    )
    assert timeblock.flush_cancelled_blocks(lambda: service).synced == 1
    assert not service.remote
    assert timeblock.list_blocks(DAY)[0]["calendar_event_id"] is None
    assert timeblock.flush_cancelled_blocks(lambda: service).synced == 0
    timeblock.save_blocks(DAY, [Block(1, "再計画", "09:00", "09:30")])
    assert [b["status"] for b in timeblock.list_blocks(DAY)] == ["cancelled", "planned"]


def test_cancel_after_response_loss_and_delete_response_loss(export_database: Path) -> None:
    """作成と削除の両方で応答が失われても、予定IDを保持して最終的に削除する。"""
    timeblock.save_blocks(DAY, [Block(1, "資料", "09:00", "09:30")])
    service = fake_service()
    service.lose_response = True
    timeblock.export_blocks_to_calendar(DAY, lambda: service)
    assert timeblock.cancel_blocks_for_task(1) == 1
    service.lose_delete_response = True
    assert timeblock.flush_cancelled_blocks(lambda: service).failed == 1
    assert timeblock.list_blocks(DAY)[0]["calendar_event_id"]
    assert timeblock.flush_cancelled_blocks(lambda: service).synced == 1
    assert not service.remote and timeblock.list_blocks(DAY)[0]["calendar_event_id"] is None


def test_cancel_for_task_can_join_parent_transaction(export_database: Path) -> None:
    """親の削除トランザクションと同じ接続で取消予約し、ロールバックも共有する。"""
    timeblock.save_blocks(DAY, [Block(1, "資料", "09:00", "09:30")])
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert timeblock.cancel_blocks_for_task(1, conn=conn) == 1
        conn.rollback()
    assert timeblock.list_blocks(DAY)[0]["status"] == "planned"


def test_deleted_task_is_reconciled_and_remote_event_removed(export_database: Path) -> None:
    """削除フックが呼ばれなくても、消えたタスクの予定は取消待ちから削除される。"""
    timeblock.save_blocks(DAY, [Block(1, "資料", "09:00", "09:30")])
    service = fake_service()
    timeblock.export_blocks_to_calendar(DAY, lambda: service)
    with connection() as conn, conn:
        conn.execute("DELETE FROM items WHERE id=1")
    assert timeblock.reconcile_task_blocks() == 1
    assert timeblock.flush_cancelled_blocks(lambda: service).synced == 1
    assert not service.remote


@pytest.mark.parametrize(
    "now",
    [dt.datetime(2026, 10, 16, 9, 30), dt.datetime(2026, 10, 16, 0, 30, tzinfo=dt.timezone.utc)],
)
def test_completion_cancels_only_unfinished_blocks_at_timezone_boundary(
    export_database: Path,
    now: dt.datetime,
) -> None:
    """end==nowの実績は残し、実行中と未来の枠は設定タイムゾーンで取り消す。"""
    for day, start, end in [
        ("2026-10-15", "18:00", "18:30"),
        (DAY, "09:00", "09:30"),
        (DAY, "09:30", "10:00"),
        ("2026-10-17", "09:00", "09:30"),
    ]:
        with connection() as conn, conn:
            conn.execute(
                "INSERT INTO time_blocks(day,task_id,title,start_time,end_time,status,created_at) "
                "VALUES (?,1,'実績',?,?,'exported','test')",
                (day, start, end),
            )
    assert timeblock.cancel_blocks_for_task(1, upcoming_only=True, now=now) == 2
    with connection() as conn:
        rows = conn.execute(
            "SELECT day,end_time,status FROM time_blocks ORDER BY day,start_time"
        ).fetchall()
    assert [row["status"] for row in rows] == ["exported", "exported", "cancelled", "cancelled"]
    assert timeblock.cancel_blocks_for_task(1, now=now) == 2


def test_reconcile_completed_task_preserves_past_exported_history(export_database: Path) -> None:
    """完了済みタスクの整合処理は過去の記録を残し、未来の枠だけ取消予約する。"""
    with connection() as conn, conn:
        for day in ("2026-10-15", "2026-10-17"):
            conn.execute(
                "INSERT INTO time_blocks(day,task_id,title,start_time,end_time,status,created_at) "
                "VALUES (?,1,'実績','09:00','09:30','exported','test')",
                (day,),
            )
        conn.execute("UPDATE items SET status='done' WHERE id=1")
    assert timeblock.reconcile_task_blocks(now=dt.datetime(2026, 10, 16, 12)) == 1
    assert timeblock.list_blocks("2026-10-15")[0]["status"] == "exported"
    assert timeblock.list_blocks("2026-10-17")[0]["status"] == "cancelled"


def test_task_completed_before_export_is_not_created(export_database: Path) -> None:
    """未完了確認の後で完了していても、新しい未来の予定を作らない。"""
    future = "2099-01-01"
    timeblock.save_blocks(future, [Block(1, "資料", "09:00", "09:30")])
    with connection() as conn, conn:
        conn.execute("UPDATE items SET status='done' WHERE id=1")
    service = fake_service()
    timeblock.export_blocks_to_calendar(future, lambda: service)
    service.events.return_value.insert.assert_not_called()
    assert timeblock.list_blocks(future)[0]["status"] == "cancelled"


def test_export_does_not_ignore_busy_events_without_id(export_database: Path) -> None:
    """予定IDが欠けていても未送信枠自身の予定と誤認して衝突を無視しない。"""
    timeblock.save_blocks(DAY, [Block(1, "資料", "09:00", "09:30")])
    service = fake_service()
    event = calendar_event("meeting", f"{DAY}T09:00:00+09:00", f"{DAY}T10:00:00+09:00")
    event.pop("id")
    service.remote[("primary", "meeting")] = event
    assert timeblock.export_blocks_to_calendar(DAY, lambda: service).failed == 1
    service.events.return_value.insert.assert_not_called()


def test_export_skips_blocks_replaced_during_calendar_refresh(export_database: Path) -> None:
    """予定取得中に承認し直された枠を、古い時刻の衝突検証結果で送信しない。"""
    timeblock.save_blocks(DAY, [Block(1, "資料", "09:00", "09:30")])
    service = fake_service()
    original = service.events.return_value.list.side_effect

    def refresh(**kwargs: Any) -> MagicMock:
        """予定再取得中に別の承認操作が入る状況を再現する。"""
        timeblock.save_blocks(DAY, [Block(1, "新しい枠", "10:00", "10:30")])
        return original(**kwargs)

    service.events.return_value.list.side_effect = refresh
    timeblock.export_blocks_to_calendar(DAY, lambda: service)
    service.events.return_value.insert.assert_not_called()
    assert timeblock.list_blocks(DAY)[0]["start_time"] == "10:00"


def test_cancel_during_insert_does_not_orphan_event(export_database: Path) -> None:
    """送信中の取消と作成を直列化し、取消処理でリモート予定を回収できる。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    timeblock.save_blocks(DAY, [Block(1, "資料", "09:00", "09:30")])
    service = fake_service()
    original = service.events.return_value.insert.side_effect
    started = Event()
    jobs: List[Any] = []
    with ThreadPoolExecutor(max_workers=1) as executor:

        def cancel() -> int:
            """別接続で送信中の同じ枠を取り消す。"""
            started.set()
            return timeblock.cancel_blocks_for_task(1)

        def insert(*, calendarId: str, body: Dict[str, Any]) -> MagicMock:
            """送信の書込ロック保持中に取消を開始する。"""
            jobs.append(executor.submit(cancel))
            assert started.wait(timeout=2)
            return original(calendarId=calendarId, body=body)

        service.events.return_value.insert.side_effect = insert
        assert timeblock.export_blocks_to_calendar(DAY, lambda: service).synced == 1
        assert jobs[0].result(timeout=2) == 1
    assert timeblock.list_blocks(DAY)[0]["status"] == "cancelled"
    assert timeblock.flush_cancelled_blocks(lambda: service).synced == 1
    assert not service.remote


def test_view_unknown_calendar_and_offline_cancellation(
    export_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取得状態が不明なら警告し、オフライン取消はIDを残して再試行できる表示にする。"""
    from streamlit.testing.v1 import AppTest

    from symnote.views import timeblock_view

    config = timeblock.effective_config()
    from zoneinfo import ZoneInfo

    day = dt.datetime.now(ZoneInfo(config.google_calendar_timezone)).date().isoformat()
    timeblock.save_blocks(day, [Block(1, "資料作成", "09:00", "09:30")])
    service = fake_service()
    assert timeblock.export_blocks_to_calendar(day, lambda: service).synced == 1
    block_id = timeblock.list_blocks(day)[0]["id"]
    monkeypatch.setattr(timeblock_view, "is_connected", lambda: False)
    app = AppTest.from_string(
        "from symnote.views.timeblock_view import render_timeblock\nrender_timeblock([], [])"
    ).run()
    assert not app.exception
    assert any("空き時間未確認" in warning.value for warning in app.warning)
    app.button(key=f"timeblock_cancel_{block_id}").click().run()
    assert not app.exception
    cancelled = timeblock.list_blocks(day)[0]
    assert cancelled["status"] == "cancelled" and cancelled["calendar_event_id"]
    assert any("取消が未送信" in warning.value for warning in app.warning)
    assert app.button(key="timeblock_export").disabled


def test_previous_day_cancellation_remains_retryable_in_today_view(
    export_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前日以前から残る取消待ちも、今日の枠がなくてもUIから再送できる。"""
    from streamlit.testing.v1 import AppTest

    from symnote.views import timeblock_view

    previous_day = "2000-01-01"
    timeblock.save_blocks(previous_day, [Block(1, "資料作成", "09:00", "09:30")])
    service = fake_service()
    timeblock.export_blocks_to_calendar(previous_day, lambda: service)
    timeblock.cancel_blocks_for_task(1)
    assert len(timeblock.list_pending_cancellations()) == 1
    monkeypatch.setattr(timeblock_view, "is_connected", lambda: True)
    monkeypatch.setattr(timeblock_view, "_calendar_service", lambda: service)
    app = AppTest.from_string(
        "from symnote.views.timeblock_view import render_timeblock\nrender_timeblock([], [])"
    ).run()
    assert not app.exception
    assert any("取消が未送信" in warning.value for warning in app.warning)
    assert not app.button(key="timeblock_export").disabled
    app.button(key="timeblock_export").click().run()
    assert not app.exception
    assert not service.remote and not timeblock.list_pending_cancellations()


def test_completion_preserves_history_after_insert_response_loss(export_database: Path) -> None:
    """作成応答が失われたまま過去枠のタスクを完了しても、再送で実績予定を回復する。"""
    past = "2000-01-01"
    timeblock.save_blocks(past, [Block(1, "資料", "09:00", "09:30")])
    service = fake_service()
    service.lose_response = True
    assert timeblock.export_blocks_to_calendar(past, lambda: service).failed == 1
    with connection() as conn, conn:
        conn.execute("UPDATE items SET status='done' WHERE id=1")
    assert timeblock.export_blocks_to_calendar(past, lambda: service).synced == 1
    assert timeblock.list_blocks(past)[0]["status"] == "exported"
    assert len(service.remote) == 1
    assert service.events.return_value.insert.call_count == 1
    assert timeblock.flush_cancelled_blocks(lambda: service).synced == 0


def test_normalize_skips_morning_digest_but_keeps_exported_time_blocks() -> None:
    """朝のまとめ予定は空き時間を塞がず、書き出し済みの時間割の枠は塞ぐ。"""
    day = dt.date(2026, 10, 16)
    zone = "Asia/Tokyo"
    digest = {
        "id": "digest",
        "summary": "SymNote: 今日の課題 2件",
        "start": {"dateTime": "2026-10-16T08:00:00+09:00"},
        "end": {"dateTime": "2026-10-16T08:15:00+09:00"},
    }
    block = {
        "id": "block",
        "summary": "SymNote: 企画書を書く",
        "start": {"dateTime": "2026-10-16T10:00:00+09:00"},
        "end": {"dateTime": "2026-10-16T11:00:00+09:00"},
        "extendedProperties": {"private": {"symnoteTimeBlock": "1"}},
    }
    events = timeblock.normalize_calendar_events([digest, block], day, zone)
    assert [event["id"] for event in events] == ["block"]
