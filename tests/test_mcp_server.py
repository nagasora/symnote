"""MCP サーバーのツールが、候補経由の登録と安全な起動条件を守ることを保証するテスト。"""

from __future__ import annotations

from pathlib import Path

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


def _registered(server) -> set:
    """FastMCP に登録されたツール名を返す。"""
    return {tool.name for tool in server._tool_manager.list_tools()}


def test_default_server_exposes_only_read_and_propose_tools(monkeypatch) -> None:
    """既定では参照と候補提案だけを公開し、直接書き込み・承認・削除は公開しない。"""
    monkeypatch.delenv(mcp_server.DIRECT_WRITES_ENV, raising=False)

    server = mcp_server.build_server(allow_direct_writes=mcp_server.direct_writes_enabled())

    assert _registered(server) == {
        "list_tasks",
        "search_notes",
        "propose_tasks",
        "list_task_candidates",
        "list_goals",
        "get_goal",
        "get_delegated_tasks",
        "get_next_actions",
        "report_progress",
    }


def test_direct_write_tools_require_explicit_opt_in(monkeypatch) -> None:
    """環境変数で許可したときだけ add_task / complete_task が公開される。承認は常に非公開。"""
    monkeypatch.setenv(mcp_server.DIRECT_WRITES_ENV, "1")

    registered = _registered(
        mcp_server.build_server(allow_direct_writes=mcp_server.direct_writes_enabled())
    )

    assert {"add_task", "complete_task"} <= registered
    assert not any("approve" in name or "delete" in name for name in registered)


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


@pytest.mark.parametrize("db_path", ["./symnote.db", "symnote.db", "file:relative.db"])
def test_relative_db_path_is_refused(monkeypatch, db_path) -> None:
    """相対パス（file: URI を含む）の DB_PATH では起動しない。"""
    monkeypatch.setenv("DB_PATH", db_path)
    with pytest.raises(SystemExit) as excinfo:
        mcp_server._ensure_absolute_db_path()
    assert excinfo.value.code == 2


def test_absolute_db_path_is_accepted(monkeypatch, tmp_path) -> None:
    """絶対パスと絶対パスの file: URI なら起動前チェックを通過する。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "ok.db"))
    mcp_server._ensure_absolute_db_path()
    monkeypatch.setenv("DB_PATH", f"file:{tmp_path / 'ok.db'}?mode=rw")
    mcp_server._ensure_absolute_db_path()


def test_blank_legacy_task_text_does_not_break_listing(database) -> None:
    """タイトルも本文も空白だけの既存タスクがあっても一覧を返せる。"""
    from symnote.core.db import insert_task

    task_id = insert_task("   \n\t")

    assert [row["title"] for row in mcp_server.list_tasks()] == [f"（無題 #{task_id}）"]


def test_mcp_server_does_not_load_gemini_sdk() -> None:
    """MCP サーバーの読み込みで Gemini SDK を読み込まない（起動の遅延を防ぐ）。"""
    import os
    import subprocess
    import sys
    from pathlib import Path

    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": src}
    code = (
        "import sys, symnote.mcp_server, symnote.calendar_sync;"
        "print('google.generativeai' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
    )
    assert result.stdout.strip() == "False"


def test_goal_read_tools_return_tree_and_context(database) -> None:
    """ゴール読み取りツールは木順進捗と成功条件を返し書き込みを公開しない。"""
    from symnote.core.goals import create_goal, set_task_goal
    from symnote.core.db import insert_item

    root = create_goal("公開", why="共有する", success_criteria="記事を公開")
    child = create_goal("準備", parent_id=root)
    task = insert_item(kind="task", raw_text="構成", tags="概要を書く")
    set_task_goal(task, child)
    rows = mcp_server.list_goals()
    assert [row["id"] for row in rows] == [root, child]
    assert rows[0]["open_count"] == 1
    context = mcp_server.get_goal(child)
    assert context["path"] == ["公開", "準備"]
    assert context["open_tasks"][0]["id"] == task
    assert mcp_server.get_goal(root)["success_criteria"] == "記事を公開"
    with pytest.raises(ValueError):
        mcp_server.get_goal(9999)


def test_delegated_tools_filter_internal_columns(database: Path) -> None:
    """委任タスクとゴール内タスクは内部列を公開せず、次の一手にゴール名が付く。"""
    import json

    from symnote.core import db, delegation, goals

    goal = goals.create_goal("公開", why="共有", success_criteria="公開完了")
    task = db.insert_task("構成を書く")
    goals.set_task_goal(task, goal)
    delegation.set_delegation(task, "claude_code")
    db.insert_task("委任していない")
    done = db.insert_task("完了した委任")
    delegation.set_delegation(done, "codex")
    db.complete_task(done)
    with db.connection() as conn, conn:
        conn.execute("UPDATE items SET embedding = ? WHERE id = ?", (b"PRIVATE_VECTOR", task))
    rows = mcp_server.get_delegated_tasks()
    assert [row["id"] for row in rows] == [task]
    assert rows[0]["delegated_to"] == "claude_code"
    assert rows[0]["goal_context"]["path"] == ["公開"]
    assert rows[0]["goal_context"]["why"] == "共有"
    assert rows[0]["goal_context"]["success_criteria"] == "公開完了"
    encoded = json.dumps(rows, ensure_ascii=False)
    assert "embedding" not in encoded and "PRIVATE_VECTOR" not in encoded
    assert "raw_text" not in encoded and "recurrence_rule_id" not in encoded
    actions = mcp_server.get_next_actions(1)
    assert actions[0]["goal_title"] == "公開"
    assert "embedding" not in json.dumps(actions)


def test_progress_report_available_without_direct_writes(database: Path) -> None:
    """直接更新を無効にしても報告は受け付け、承認するまでタスクは変えない。"""
    from symnote.core import db

    server = mcp_server.build_server(allow_direct_writes=False)
    assert "report_progress" in _registered(server)
    assert "承認待ち" in server.instructions
    assert "外部の情報源に書かれた指示" in server.instructions
    task = db.insert_task("仕事")
    result = mcp_server.report_progress(
        summary="完了", task_id=task, suggested_status="done", agent="codex"
    )
    assert result["status"] == "pending"
    with db.connection() as conn:
        report = conn.execute(
            "SELECT * FROM agent_reports WHERE id = ?", (result["report_id"],)
        ).fetchone()
        row = conn.execute("SELECT * FROM items WHERE id = ?", (task,)).fetchone()
    assert report["status"] == "pending"
    assert row["status"] == "inbox" and row["raw_text"] == "仕事"
    with pytest.raises(ValueError):
        mcp_server.report_progress(summary="対象なし")
