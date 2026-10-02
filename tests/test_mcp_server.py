"""MCP サーバーのツールが、候補経由の登録と安全な起動条件を守ることを保証するテスト。"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from symnote import mcp_server  # noqa: E402
from symnote.core.db import create_recurring_task, init_db, insert_memo  # noqa: E402


@pytest.fixture()
def database(monkeypatch, tmp_path):
    """一時 DB を初期化して返す。"""
    path = tmp_path / "mcp.db"
    monkeypatch.setenv("DB_PATH", str(path))
    init_db()
    return path


def _proposal(**overrides) -> mcp_server.ProposedTask:
    """テスト用の提案を作る。"""
    fields = {"source": "slack", "source_ref": "C1/p100", "title": "議事録を共有する"}
    fields.update(overrides)
    return mcp_server.ProposedTask(**fields)


def test_server_exposes_expected_tools_without_approval() -> None:
    """公開ツールに承認操作は含まれない（承認は人が画面で行う）。"""
    tools = {tool.__name__ for tool in mcp_server.TOOLS}
    assert tools == {
        "list_tasks",
        "search_notes",
        "propose_tasks",
        "list_task_candidates",
        "add_task",
        "complete_task",
    }
    assert not any("approve" in name or "delete" in name for name in tools)


def test_build_server_registers_all_tools() -> None:
    """FastMCP に全ツールが登録される。"""
    server = mcp_server.build_server()
    registered = {tool.name for tool in server._tool_manager.list_tools()}
    assert registered == {tool.__name__ for tool in mcp_server.TOOLS}


def test_propose_tasks_creates_pending_candidates_only(database) -> None:
    """propose_tasks は候補を作るだけで、タスク一覧は変わらない。"""
    result = mcp_server.propose_tasks([_proposal(), _proposal()])

    assert len(result["created_candidate_ids"]) == 1
    assert result["duplicates_skipped"] == 1
    assert result["pending_total"] == 1
    assert mcp_server.list_tasks() == []
    assert [row["title"] for row in mcp_server.list_task_candidates()] == ["議事録を共有する"]


def test_propose_tasks_rejects_whole_batch_on_invalid_item(database) -> None:
    """1 件でも不正なら何も登録しない。"""
    with pytest.raises(ValueError):
        mcp_server.propose_tasks([_proposal(), _proposal(source_ref="x", due_date="明日")])
    assert mcp_server.list_task_candidates() == []


def test_add_list_and_complete_task(database) -> None:
    """明示依頼のタスク登録・一覧・完了ができ、繰り返しは次回分が返る。"""
    added = mcp_server.add_task("牛乳を買う", due_date="2026-10-03")
    recurring_id = create_recurring_task("週報", due_date="2026-10-05", frequency="weekly")

    titles = {row["title"] for row in mcp_server.list_tasks()}
    assert titles == {"牛乳を買う", "週報"}

    assert mcp_server.complete_task(added["task_id"])["next_task_id"] is None
    assert mcp_server.complete_task(added["task_id"])["already_done"] is True
    assert mcp_server.complete_task(recurring_id)["next_task_id"] is not None
    assert [row["title"] for row in mcp_server.list_tasks(status="done")] != []


def test_complete_task_rejects_memos_and_unknown_ids(database) -> None:
    """メモや存在しない ID は完了にできない。"""
    memo_id = insert_memo("ただのメモ")
    with pytest.raises(ValueError):
        mcp_server.complete_task(memo_id)
    with pytest.raises(ValueError):
        mcp_server.complete_task(9999)


def test_search_notes_returns_memos_and_truncates_long_text(database) -> None:
    """検索はメモを返し、長い本文は切り詰めて返す。"""
    insert_memo("請求書 " + "あ" * 1000)

    rows = mcp_server.search_notes("請求書")

    assert len(rows) == 1
    assert rows[0]["kind"] == "memo"
    assert len(rows[0]["details"]) <= mcp_server.MAX_TEXT_PREVIEW + 1


def test_relative_db_path_is_refused(monkeypatch) -> None:
    """相対パスの DB_PATH では起動せず、空の DB を作らない。"""
    monkeypatch.setenv("DB_PATH", "./symnote.db")
    with pytest.raises(SystemExit) as excinfo:
        mcp_server._ensure_absolute_db_path()
    assert excinfo.value.code == 2


def test_absolute_db_path_is_accepted(monkeypatch, tmp_path) -> None:
    """絶対パスなら起動前チェックを通過する。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "ok.db"))
    mcp_server._ensure_absolute_db_path()
