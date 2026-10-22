"""一時DBでゴールツリーと確認付きAI提案の振る舞いを保証する。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict

import pytest

from symnote.core import db, goals, llm


@pytest.fixture()
def database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """ユーザーDBを使わずテスト専用DBを初期化する。"""
    path = tmp_path / "goals.db"
    monkeypatch.setenv("DB_PATH", str(path))
    db.init_db()
    return path


def _task(
    goal_id: int | None,
    *,
    done: bool = False,
    created: str = "2026-10-01T10:00:00",
    completed: str | None = None,
) -> int:
    """時刻を固定したテスト用タスクを登録する。"""
    task_id = db.insert_item(
        kind="task", raw_text="理由", tags="一歩", status="done" if done else "inbox"
    )
    goals.set_task_goal(task_id, goal_id)
    with db.connection() as conn, conn:
        conn.execute(
            "UPDATE items SET created_at = ?, completed_at = ? WHERE id = ?",
            (created, completed, task_id),
        )
    return task_id


def _fake(value: Any) -> Any:
    """指定したJSON値を返す通信不要の生成器を作る。"""

    def generate(prompt: str, **kwargs: Any) -> Any:
        """プロンプトの存在を確認してテスト値を返す。"""
        assert prompt
        return value

    return generate


def test_crud_and_context(database: Path) -> None:
    """祖先パス、成功の定義、配下タスクと更新値が保持される。"""
    root = goals.create_goal("研究", "学ぶため", "論文を公開", "2026-12-31")
    child = goals.create_goal("実験", parent_id=root)
    task = _task(child)
    goals.update_goal(child, title="再現実験", why="確かめる", order_index=2)
    context = goals.get_goal_context(child)
    assert context["path"] == ["研究", "再現実験"]
    assert context["ancestors"][0]["success_criteria"] == "論文を公開"
    assert context["open_tasks"][0]["id"] == task
    assert goals.get_goal_context(root)["subgoals"][0]["id"] == child
    goals.update_goal(child, status="done")
    assert goals.get_goal_context(child)["status"] == "done"


def test_delete_resets_descendant_tasks(database: Path) -> None:
    """ゴール削除は子孫だけを消しタスク本体を未分類で残す。"""
    root = goals.create_goal("親")
    child = goals.create_goal("子", parent_id=root)
    other = goals.create_goal("別")
    task = _task(child)
    other_task = _task(other)
    goals.delete_goal(root)
    assert [row.id for row in goals.list_goals_with_progress()] == [other]
    with db.connection() as conn:
        assert conn.execute("SELECT goal_id FROM items WHERE id = ?", (task,)).fetchone()[0] is None
        assert (
            conn.execute("SELECT goal_id FROM items WHERE id = ?", (other_task,)).fetchone()[0]
            == other
        )


def test_tree_order_depth_progress_and_activity(database: Path) -> None:
    """木順と深さを保持し子孫の活動・完了を親へ集約する。"""
    root = goals.create_goal("親")
    sibling = goals.create_goal("兄弟")
    child = goals.create_goal("子", parent_id=root)
    grandchild = goals.create_goal("孫", parent_id=child)
    _task(root, done=True, completed="2026-10-10T11:00:00")
    next_task = _task(grandchild)
    rows = goals.list_goals_with_progress(today=date(2026, 10, 16))
    assert [(row.id, row.depth) for row in rows] == [
        (root, 0),
        (child, 1),
        (grandchild, 2),
        (sibling, 0),
    ]
    assert rows[0].done_count == rows[0].open_count == 1
    assert rows[0].progress == 0.5
    assert rows[0].stale_days == 6
    assert rows[0].last_activity == "2026-10-10T11:00:00"
    assert rows[0].next_task["id"] == next_task
    assert rows[-1].last_activity is None
    goals.update_goal(child, status="archived")
    assert [row.id for row in goals.list_goals_with_progress()] == [root, sibling]
    assert len(goals.list_goals_with_progress(include_archived=True)) == 4


def test_stale_only_active_with_open_tasks(database: Path) -> None:
    """放置判定は未完了タスクを持つ進行中ゴールだけを返す。"""
    stale = goals.create_goal("放置")
    done = goals.create_goal("完了")
    empty = goals.create_goal("空")
    _task(stale, created="2000-01-01T00:00:00")
    _task(done, done=True)
    assert [row.id for row in goals.stale_goals(3)] == [stale]
    goals.update_goal(stale, status="done")
    assert goals.stale_goals() == []
    assert empty


@pytest.mark.parametrize(
    "fields",
    [
        {"title": ""},
        {"title": 1},
        {"title": "a" * 201},
        {"target_date": "明日"},
        {"status": "invalid"},
        {"evil_column": 1},
    ],
)
def test_update_rejects_invalid_fields(database: Path, fields: Dict[str, Any]) -> None:
    """不正な更新は元のゴールを変えない。"""
    goal = goals.create_goal("元")
    with pytest.raises((ValueError, TypeError)):
        goals.update_goal(goal, **fields)
    assert goals.get_goal_context(goal)["title"] == "元"


def test_reject_cycles_and_invalid_assignments(database: Path) -> None:
    """循環する親変更とメモ・存在しないゴールの割当を拒否する。"""
    root = goals.create_goal("親")
    child = goals.create_goal("子", parent_id=root)
    for parent in (root, child):
        with pytest.raises(ValueError):
            goals.update_goal(root, parent_id=parent)
    task = _task(None)
    with pytest.raises(ValueError):
        goals.set_task_goal(task, 999)
    with pytest.raises(ValueError):
        goals.set_task_goal(db.insert_memo("メモ"), root)
    goals.set_task_goal(task, root)
    goals.set_task_goal(task, None)
    assert goals.list_unclassified_tasks()[0]["id"] == task


def test_breakdown_validates_and_excludes_empty_titles(database: Path) -> None:
    """空タイトルだけを除き型付き提案へ変換し、まだ保存しない。"""
    root = goals.create_goal("公開", why="共有", success_criteria="記事を公開した状態")
    value = {
        "subgoals": [{"title": "準備", "why": "整理"}, {"title": " "}],
        "tasks": [
            {"title": "概要を書く", "why": "構成", "due_hint": "今日", "estimate_minutes": 15},
            {"title": ""},
        ],
    }
    proposal = goals.propose_goal_breakdown(root, generate_json=_fake(value))
    assert proposal.subgoals == [goals.ProposedGoal("準備", "整理")]
    assert proposal.tasks[0].estimate_minutes == 15
    assert len(proposal.tasks) == 1
    assert len(goals.list_goals_with_progress()) == 1
    assert goals.list_unclassified_tasks() == []


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"tasks": "bad"},
        {"tasks": [1]},
        {"tasks": [{"title": 123}]},
        {"tasks": [{"title": "a" * 201}]},
        {"tasks": [{"title": "a", "why": []}]},
        {"tasks": [{"title": "a", "estimate_minutes": True}]},
        {"tasks": [{"title": "a", "estimate_minutes": 0}]},
        {"tasks": [{"title": "a", "due_hint": "いつか"}]},
        {"tasks": [{"title": "a"}] * 31},
        {"subgoals": [{"title": "a"}] * 11},
    ],
)
def test_breakdown_rejects_untrusted_responses(database: Path, value: Any) -> None:
    """不正型・長さ・件数・所要時間・期日目安を拒否する。"""
    root = goals.create_goal("目標")
    with pytest.raises(ValueError):
        goals.propose_goal_breakdown(root, generate_json=_fake(value))


def test_ai_unavailable_preserves_manual_workflow(database: Path) -> None:
    """通信不可でもDBの状態を変更せず手動操作を継続できる。"""
    root = goals.create_goal("目標")

    def unavailable(prompt: str, **kwargs: Any) -> Any:
        """接続不可を再現する。"""
        raise llm.LLMUnavailableError("接続不可")

    with pytest.raises(llm.LLMUnavailableError):
        goals.propose_goal_breakdown(root, generate_json=unavailable)
    goals.create_goal("手動", parent_id=root)
    assert len(goals.list_goals_with_progress()) == 2


@pytest.mark.parametrize(
    "hint, expected",
    [
        ("今日", "2026-10-16"),
        ("今週", "2026-10-16"),
        ("来週", "2026-10-23"),
        ("今月", "2026-10-31"),
        ("", None),
    ],
)
def test_due_hint_conversion(hint: str, expected: str | None) -> None:
    """日本語の期日目安を週の金曜と月末へ変換する。"""
    assert goals._due_date(hint, date(2026, 10, 16)) == expected
    assert goals._due_date("今週", date(2026, 10, 18)) == "2026-10-16"
    assert goals._due_date("来週", date(2026, 12, 31)) == "2027-01-08"
    assert goals._due_date("今月", date(2028, 2, 2)) == "2028-02-29"


def test_apply_selected_and_calendar_outbox(database: Path) -> None:
    """選択した提案だけを紐付けて保存しカレンダー同期も予約する。"""
    root = goals.create_goal("目標")
    proposal = goals.GoalBreakdown(
        [goals.ProposedGoal("子")],
        [goals.ProposedTask("最初の一歩", "理由", "今日", 15), goals.ProposedTask("非選択")],
    )
    children, tasks = goals.apply_goal_breakdown(root, proposal, [0], [0])
    assert len(children) == len(tasks) == 1
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (tasks[0],)).fetchone()
        assert row["tags"] == "最初の一歩"
        assert row["raw_text"] == "理由"
        assert row["goal_id"] == root
        assert row["due_date"] == date.today().isoformat()
        assert row["estimate_minutes"] == 15
        assert conn.execute(
            "SELECT task_id FROM calendar_sync_outbox WHERE task_id = ?", (tasks[0],)
        ).fetchone()


def test_apply_rolls_back_goals_tasks_and_outbox(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """途中で登録に失敗したら子ゴール・タスク・同期予約を全て戻す。"""
    root = goals.create_goal("目標")
    original = db.insert_item_in_transaction
    calls = 0

    def insert(*args: Any, **kwargs: Any) -> int:
        """二件目の保存失敗を再現する。"""
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("保存失敗")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "insert_item_in_transaction", insert)
    proposal = goals.GoalBreakdown(
        [goals.ProposedGoal("子")], [goals.ProposedTask("一"), goals.ProposedTask("二")]
    )
    with pytest.raises(RuntimeError):
        goals.apply_goal_breakdown(root, proposal, [0], [0, 1])
    assert len(goals.list_goals_with_progress()) == 1
    assert goals.get_goal_context(root)["open_tasks"] == []
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM calendar_sync_outbox").fetchone()[0] == 0


@pytest.mark.parametrize("indices", [[-1], [True], [1], [0, 0]])
def test_apply_rejects_invalid_selection(database: Path, indices: Any) -> None:
    """範囲外や重複した提案番号は保存前に拒否する。"""
    root = goals.create_goal("目標")
    with pytest.raises(ValueError):
        goals.apply_goal_breakdown(
            root, goals.GoalBreakdown([], [goals.ProposedTask("一")]), [], indices
        )
    assert goals.get_goal_context(root)["open_tasks"] == []


def test_unclassified_proposals_and_apply_race(database: Path) -> None:
    """割当は確認まで保存せず他画面での分類を一括上書きしない。"""
    root = goals.create_goal("目標")
    tasks = [_task(None), _task(None)]
    proposals = goals.propose_task_goal_assignments(
        generate_json=_fake(
            {
                "assignments": [
                    {"task_id": task, "goal_id": root, "why": "関係する"} for task in tasks
                ]
            }
        )
    )
    assert len(goals.list_unclassified_tasks()) == 2
    goals.set_task_goal(tasks[1], root)
    with pytest.raises(ValueError):
        goals.apply_task_goal_assignments(proposals)
    assert [task["id"] for task in goals.list_unclassified_tasks()] == [tasks[0]]
    goals.apply_task_goal_assignments(proposals[:1])
    assert goals.list_unclassified_tasks() == []


@pytest.mark.parametrize(
    "entry",
    [
        {"task_id": 999, "goal_id": 1},
        {"task_id": True, "goal_id": 1},
        {"task_id": 1, "goal_id": 999},
    ],
)
def test_assignment_rejects_unknown_ids(database: Path, entry: Dict[str, Any]) -> None:
    """AIは実在しないタスクや対象外ゴールへ割り当てられない。"""
    goals.create_goal("目標")
    _task(None)
    with pytest.raises(ValueError):
        goals.propose_task_goal_assignments(generate_json=_fake({"assignments": [entry]}))


def test_manual_task_creation_is_atomic(database: Path) -> None:
    """手動タスク追加は本文・期限・ゴールを一括保存し、不正入力を拒否する。"""
    goal = goals.create_goal("目標")
    task = goals.create_goal_task(goal, "一歩", "理由", "2026-10-31")
    row = goals.get_goal_context(goal)["open_tasks"][0]
    assert row["id"] == task
    assert row["tags"] == "一歩"
    assert row["raw_text"] == "理由"
    assert row["due_date"] == "2026-10-31"
    with pytest.raises(ValueError):
        goals.create_goal_task(9999, "作られない")
    with pytest.raises(ValueError):
        goals.create_goal_task(goal, " ")
    assert len(goals.get_goal_context(goal)["open_tasks"]) == 1


def test_goals_ui_empty_and_manual_creation(database: Path) -> None:
    """空画面から新規ゴールと手動タスクを作成し進捗を表示できる。"""
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        "from symnote.views.goals_view import render_goals_page\nrender_goals_page()",
        default_timeout=10,
    ).run()
    assert not app.exception
    next(field for field in app.text_input if field.label == "タイトル").set_value("公開")
    next(field for field in app.text_area if field.label == "成功の定義1文").set_value("記事を公開")
    next(button for button in app.button if button.label == "ゴールを作成").click().run()
    assert not app.exception
    assert len(goals.list_goals_with_progress()) == 1
    next(field for field in app.text_input if field.label == "タスクのタイトル").set_value(
        "概要を書く"
    )
    next(button for button in app.button if button.label == "タスクを追加").click().run()
    assert not app.exception
    goal = goals.list_goals_with_progress()[0]
    assert goal.open_count == 1
    assert goal.next_task["tags"] == "概要を書く"


def test_goals_ui_ai_confirmation_and_unclassified(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI分解と未分類割当はいずれもチェックして適用するまでDBを変更しない。"""
    from streamlit.testing.v1 import AppTest

    goal = goals.create_goal("公開")
    task = _task(None)
    monkeypatch.setattr(
        goals,
        "propose_goal_breakdown",
        lambda goal_id: goals.GoalBreakdown(
            [], [goals.ProposedTask("概要を書く", due_hint="今日")]
        ),
    )
    monkeypatch.setattr(
        goals,
        "propose_task_goal_assignments",
        lambda: [goals.TaskGoalProposal(task, goal, "関係する")],
    )
    app = AppTest.from_string(
        "from symnote.views.goals_view import render_goals_page\nrender_goals_page()",
        default_timeout=10,
    ).run()
    app.button(key=f"goal_ai_{goal}").click().run()
    assert not app.exception
    assert goals.get_goal_context(goal)["open_tasks"] == []
    app.checkbox(key=f"goal_breakdown_{goal}_task_0").check().run()
    app.button(key=f"goal_breakdown_{goal}_apply").click().run()
    assert not app.exception
    assert len(goals.get_goal_context(goal)["open_tasks"]) == 1
    next(
        button for button in app.button if button.label == "AIでまとめて割当先を提案"
    ).click().run()
    assert len(goals.list_unclassified_tasks()) == 1
    app.checkbox(key=f"assign_proposal_0_{task}_{goal}").check().run()
    next(button for button in app.button if button.label == "確認した割当を適用").click().run()
    assert not app.exception
    assert goals.list_unclassified_tasks() == []


def test_concurrent_parent_updates_cannot_create_cycle(database: Path) -> None:
    """二画面から同時に相互の親へ変更しても一方を拒否して木を保つ。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    first = goals.create_goal("一")
    second = goals.create_goal("二")
    barrier = Barrier(2)

    def reparent(pair: tuple[int, int]) -> bool:
        """同時開始した親更新が成功したかを返す。"""
        barrier.wait(timeout=5)
        try:
            goals.update_goal(pair[0], parent_id=pair[1])
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reparent, [(first, second), (second, first)]))
    assert sorted(outcomes) == [False, True]
    assert len(goals.list_goals_with_progress()) == 2


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"assignments": "bad"},
        {"assignments": [None]},
        {"assignments": [{}] * 51},
        {"assignments": [{"task_id": 1, "goal_id": 1}] * 2},
    ],
)
def test_assignment_rejects_invalid_shape_and_size(database: Path, value: Any) -> None:
    """一括割当提案の不正型・過多件数・重複を拒否する。"""
    goals.create_goal("目標")
    _task(None)
    with pytest.raises(ValueError):
        goals.propose_task_goal_assignments(generate_json=_fake(value))


def test_goals_ui_regeneration_clears_selection(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """再生成した別提案に前の確認チェックを引き継がない。"""
    from streamlit.testing.v1 import AppTest

    goal = goals.create_goal("公開")
    monkeypatch.setattr(
        goals,
        "propose_goal_breakdown",
        lambda goal_id: goals.GoalBreakdown([], [goals.ProposedTask("最初の一歩")]),
    )
    app = AppTest.from_string(
        "from symnote.views.goals_view import render_goals_page\nrender_goals_page()",
        default_timeout=10,
    ).run()
    app.button(key=f"goal_ai_{goal}").click().run()
    app.checkbox(key=f"goal_breakdown_{goal}_task_0").check().run()
    assert not app.button(key=f"goal_breakdown_{goal}_apply").disabled
    app.button(key=f"goal_ai_{goal}").click().run()
    assert not app.exception
    assert not app.checkbox(key=f"goal_breakdown_{goal}_task_0").value
    assert app.button(key=f"goal_breakdown_{goal}_apply").disabled


def test_recurring_next_occurrence_keeps_goal_and_estimate(monkeypatch, tmp_path) -> None:
    """繰り返しの次回分もゴールと所要時間を引き継ぐ。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "recur.db"))
    from symnote.core import db, goals

    db.init_db()
    goal_id = goals.create_goal("健康")
    task_id = db.create_recurring_task(raw_text="ジム", due_date="2026-10-16", frequency="daily")
    goals.set_task_goal(task_id, goal_id)
    db.update_item_fields(task_id, estimate_minutes=60)
    next_id = db.complete_task(task_id)
    with db.connection() as conn:
        row = conn.execute(
            "SELECT goal_id, estimate_minutes FROM items WHERE id = ?", (next_id,)
        ).fetchone()
    assert (row["goal_id"], row["estimate_minutes"]) == (goal_id, 60)


def _seed_plan(*, ai_used: bool = False) -> goals.SeedPlan:
    """Return a complete editable plan used by seed adoption tests."""
    return goals.SeedPlan(
        stages=[
            goals.Stage("準備", "必要なものをそろえる"),
            goals.Stage("実行", "小さく進める"),
            goals.Stage("完成", "達成条件を確認する", "2026-12-31"),
        ],
        first_quests=[
            goals.ProposedTask("材料を確認", "不足を知る", "今日", 15),
            goals.ProposedTask("たたき台を作る", "形にする", "", 30),
        ],
        ai_used=ai_used,
        why="使えるようにするため",
        done_when="成果物を公開できた状態",
        target_date="2026-12-31",
    )


def test_seed_plan_preview_validates_without_writes_and_falls_back(database: Path) -> None:
    """Preview uses valid AI output, while invalid or unavailable AI returns a safe local plan."""
    value = {
        "stages": [
            {"title": "調べる", "why": "現在地を知る"},
            {"title": "作る", "why": "形にする"},
            {"title": "届ける", "why": "完了させる", "target_date": "2026-12-31"},
        ],
        "first_quests": [
            {
                "title": "資料を集める",
                "why": "判断する",
                "due_hint": "今日",
                "estimate_minutes": 15,
            },
            {"title": "構成を書く", "why": "見通す", "due_hint": "今週", "estimate_minutes": 30},
        ],
    }
    plan = goals.propose_seed_plan(
        "発表する",
        "共有するため",
        "発表が終わった状態",
        "2026-12-31",
        today=date(2026, 10, 23),
        generate_json=_fake(value),
    )
    assert plan.ai_used is True
    assert len(plan.stages) == 3
    assert [quest.estimate_minutes for quest in plan.first_quests] == [15, 30]
    assert goals.list_goals_with_progress() == []

    fallback = goals.propose_seed_plan(
        "発表する",
        "共有するため",
        "発表が終わった状態",
        None,
        today=date(2026, 10, 23),
        generate_json=_fake({"stages": [], "first_quests": []}),
    )
    assert fallback.ai_used is False
    assert [stage.title for stage in fallback.stages] == ["準備する", "進める", "仕上げる"]
    assert all(5 <= (quest.estimate_minutes or 0) <= 60 for quest in fallback.first_quests)

    def unavailable(prompt: str, **kwargs: Any) -> Any:
        raise llm.LLMUnavailableError("offline")

    offline = goals.propose_seed_plan(
        "発表する",
        "共有するため",
        "発表が終わった状態",
        None,
        today=date(2026, 10, 23),
        generate_json=unavailable,
    )
    assert offline.ai_used is False
    assert len(offline.stages) == 3
    assert goals.list_goals_with_progress() == []


@pytest.mark.parametrize(
    "change",
    [
        {"stages": [goals.Stage("一"), goals.Stage("二")]},
        {"first_quests": [goals.ProposedTask("一", estimate_minutes=15)]},
        {
            "first_quests": [
                goals.ProposedTask("一", estimate_minutes=4),
                goals.ProposedTask("二", estimate_minutes=15),
            ]
        },
        {
            "first_quests": [
                goals.ProposedTask("一", estimate_minutes=16),
                goals.ProposedTask("二", estimate_minutes=30),
            ]
        },
        {"target_date": "いつか"},
    ],
)
def test_adopt_seed_rejects_invalid_edited_plan_without_writes(
    database: Path, change: dict[str, Any]
) -> None:
    """An edited preview must still satisfy the seed plan schema before adoption."""
    seed = db.insert_item(kind="memo", raw_text="新しい企画", ai_category="idea")
    values = _seed_plan().__dict__ | change
    with pytest.raises(ValueError):
        goals.adopt_seed_plan(seed, goals.SeedPlan(**values), today_first=True)
    with db.connection() as conn:
        assert conn.execute("SELECT goal_id FROM items WHERE id = ?", (seed,)).fetchone()[0] is None
        assert conn.execute("SELECT count(*) FROM goals").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM calendar_sync_outbox").fetchone()[0] == 0


def test_adopt_seed_is_atomic_idempotent_and_marks_first_quest_today(database: Path) -> None:
    """Adoption creates one map transaction and a repeated request returns the existing IDs."""
    seed = db.insert_item(kind="memo", raw_text="新しい企画\n詳細", ai_category="idea")
    created = goals.adopt_seed_plan(seed, _seed_plan(), today_first=True)
    repeated = goals.adopt_seed_plan(seed, _seed_plan(ai_used=True), today_first=False)
    assert repeated == created
    assert len(created["stage_ids"]) == 3
    assert len(created["task_ids"]) == 2

    context = goals.get_goal_context(created["goal_id"])
    assert context["title"] == "新しい企画"
    assert context["why"] == "使えるようにするため"
    assert context["success_criteria"] == "成果物を公開できた状態"
    assert [stage["order_index"] for stage in context["subgoals"]] == [0, 1, 2]
    with db.connection() as conn:
        seed_goal = conn.execute("SELECT goal_id FROM items WHERE id = ?", (seed,)).fetchone()[0]
        tasks = conn.execute(
            "SELECT id, goal_id, status, estimate_minutes "
            "FROM items WHERE id IN (?, ?) ORDER BY id",
            created["task_ids"],
        ).fetchall()
        assert seed_goal == created["goal_id"]
        assert [row["goal_id"] for row in tasks] == [created["stage_ids"][0]] * 2
        assert [row["status"] for row in tasks] == ["today", "inbox"]
        assert [row["estimate_minutes"] for row in tasks] == [15, 30]
        assert conn.execute("SELECT count(*) FROM goals").fetchone()[0] == 4
        assert conn.execute("SELECT count(*) FROM calendar_sync_outbox").fetchone()[0] == 2


def test_adopt_seed_uses_display_title_for_long_single_line_memo(database: Path) -> None:
    """A long seed keeps its full memo while the adopted map uses the displayed 120-char title."""
    raw = "長" * 10_000
    seed = db.insert_item(kind="memo", raw_text=raw, ai_category="idea")
    created = goals.adopt_seed_plan(seed, _seed_plan(), today_first=False)
    assert goals.get_goal_context(created["goal_id"])["title"] == raw[:120]
    with db.connection() as conn:
        stored = conn.execute("SELECT raw_text FROM items WHERE id = ?", (seed,)).fetchone()[0]
    assert stored == raw


def test_adopt_seed_rolls_back_everything_on_task_failure(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-adoption failure leaves no parent, stage, quest, outbox, or seed marker behind."""
    seed = db.insert_item(kind="memo", raw_text="失敗する企画", ai_category="someday")
    original = db.insert_item_in_transaction
    calls = 0

    def insert(*args: Any, **kwargs: Any) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("forced failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "insert_item_in_transaction", insert)
    with pytest.raises(RuntimeError, match="forced failure"):
        goals.adopt_seed_plan(seed, _seed_plan(), today_first=False)
    with db.connection() as conn:
        assert conn.execute("SELECT goal_id FROM items WHERE id = ?", (seed,)).fetchone()[0] is None
        assert conn.execute("SELECT count(*) FROM goals").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM items WHERE kind = 'task'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM calendar_sync_outbox").fetchone()[0] == 0


def test_list_map_marks_current_path_and_advances_to_empty_next_stage(database: Path) -> None:
    """Map state follows the first open quest, then advances to the next rolling-plan stage."""
    seed = db.insert_item(kind="memo", raw_text="冒険", ai_category="idea")
    created = goals.adopt_seed_plan(seed, _seed_plan(), today_first=False)
    view = goals.list_map(created["goal_id"])
    assert view.current_stage_id == created["stage_ids"][0]
    assert view.current_task_id == created["task_ids"][0]
    assert [stage.state for stage in view.stages] == ["current", "upcoming", "upcoming"]
    assert [task.state for task in view.stages[0].tasks] == ["current", "upcoming"]
    assert view.progress == 0

    with db.connection() as conn, conn:
        conn.execute("UPDATE items SET status = 'done' WHERE id IN (?, ?)", created["task_ids"])
    advanced = goals.list_map(created["goal_id"])
    assert advanced.current_stage_id == created["stage_ids"][1]
    assert advanced.current_task_id is None
    assert [stage.state for stage in advanced.stages] == ["cleared", "current", "upcoming"]
    assert advanced.done_count == advanced.total_count == 2
    assert advanced.progress == 1


def test_seed_requires_an_eligible_memo(database: Path) -> None:
    """Ordinary memos and tasks cannot be adopted as seeds."""
    memo = db.insert_memo("ただのメモ")
    task = db.insert_task("ただのタスク")
    for item_id in (memo, task, 9999):
        with pytest.raises(ValueError):
            goals.adopt_seed_plan(item_id, _seed_plan(), today_first=False)
    assert goals.list_goals_with_progress() == []


def test_mcp_goal_tools_return_only_serialized_task_fields(monkeypatch, tmp_path) -> None:
    """MCP のゴールツールは内部列（embedding など）を外部に返さない。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "mcp.db"))
    from symnote import mcp_server
    from symnote.core import db, goals

    db.init_db()
    goal_id = goals.create_goal("発表")
    task_id = db.insert_task("スライド", tags="スライドを作る")
    goals.set_task_goal(task_id, goal_id)
    with db.connection() as conn, conn:
        conn.execute("UPDATE items SET embedding = ? WHERE id = ?", (b"\xff\xfe", task_id))
    listed = mcp_server.list_goals()
    assert set(listed[0]["next_task"]) == {
        "id",
        "kind",
        "title",
        "details",
        "status",
        "due_date",
        "due_time",
    }
    context = mcp_server.get_goal(goal_id)
    assert all("embedding" not in task for task in context["open_tasks"])
    import json

    json.dumps(listed, ensure_ascii=False)
