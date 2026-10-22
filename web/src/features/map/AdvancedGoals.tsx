import { useEffect, useMemo, useState } from "react";
import { Archive, Check, FolderTree, Plus, Save } from "lucide-react";
import { apiFetch } from "../../api/client";
import type { GoalContext, GoalItem } from "../../api";
import { useGame } from "../game/GameContext";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { Chip } from "../../components/ui/Chip";
import { EmptyState } from "../../components/ui/EmptyState";

export function AdvancedGoals({
  onChanged,
}: {
  onChanged: () => Promise<void>;
}) {
  const { tasks, complete, openQuest, refresh, showToast } = useGame();
  const [goals, setGoals] = useState<GoalItem[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [detail, setDetail] = useState<GoalContext | null>(null);
  const [newTitle, setNewTitle] = useState("");
  const [newWhy, setNewWhy] = useState("");
  const [childTitle, setChildTitle] = useState("");
  const [assignTask, setAssignTask] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function loadGoals() {
    try {
      const result = await apiFetch<{ goals: GoalItem[] }>(
        "/api/goals?include_archived=true",
      );
      setGoals(result.goals);
      setSelected((current) => current ?? result.goals[0]?.id ?? null);
      setError(null);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "マップを読み込めませんでした",
      );
    }
  }
  async function loadDetail(id: number) {
    try {
      setDetail(
        (await apiFetch<{ goal: GoalContext }>(`/api/goals/${id}`)).goal,
      );
      setError(null);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "マップの詳細を読み込めませんでした",
      );
    }
  }
  useEffect(() => {
    void loadGoals();
  }, []);
  useEffect(() => {
    setDetail(null);
    if (selected) void loadDetail(selected);
  }, [selected]);
  const ordered = useMemo(
    () => [...goals].sort((a, b) => a.depth - b.depth || a.id - b.id),
    [goals],
  );

  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (reason) {
      const message =
        reason instanceof Error
          ? reason.message
          : "マップを更新できませんでした";
      setError(message);
      showToast(message);
    } finally {
      setBusy(false);
    }
  }
  async function sync() {
    await Promise.all([loadGoals(), refresh(), onChanged()]);
    if (selected) await loadDetail(selected);
  }
  async function create(parentId: number | null = null) {
    const title = parentId ? childTitle.trim() : newTitle.trim();
    if (!title) return;
    await run(async () => {
      const result = await apiFetch<{ goal: GoalItem }>("/api/goals", {
        body: {
          title,
          why: parentId ? "" : newWhy.trim(),
          parent_id: parentId,
        },
      });
      setNewTitle("");
      setNewWhy("");
      setChildTitle("");
      await loadGoals();
      setSelected(result.goal.id);
      await onChanged();
      showToast(parentId ? "子マップを追加しました" : "マップを追加しました");
    });
  }
  async function save() {
    if (!detail?.title.trim()) return;
    await run(async () => {
      await apiFetch(`/api/goals/${detail.id}`, {
        method: "PATCH",
        body: {
          title: detail.title,
          why: detail.why,
          success_criteria: detail.success_criteria,
          target_date: detail.target_date,
          parent_id: detail.parent_id,
        },
      });
      await sync();
      showToast("マップを保存しました");
    });
  }
  async function changeStatus(status: "active" | "done" | "archived") {
    if (!detail) return;
    await run(async () => {
      await apiFetch(`/api/goals/${detail.id}`, {
        method: "PATCH",
        body: { status },
      });
      await sync();
      showToast(
        status === "done"
          ? "マップを達成しました"
          : status === "archived"
            ? "マップをしまいました"
            : "進行中に戻しました",
      );
    });
  }
  async function assign() {
    if (!detail || !assignTask) return;
    await run(async () => {
      await apiFetch(`/api/tasks/${assignTask}`, {
        method: "PATCH",
        body: { goal_id: detail.id },
      });
      setAssignTask("");
      await sync();
      showToast("クエストをマップに置きました");
    });
  }

  return (
    <section className="advanced-goals">
      <header>
        <FolderTree />
        <div>
          <h2>マップの詳しい管理</h2>
          <p>親子関係、達成条件、クエストの置き場所を編集できます。</p>
        </div>
      </header>
      <Card className="goal-create-advanced">
        <input
          value={newTitle}
          onChange={(event) => setNewTitle(event.target.value)}
          placeholder="新しいマップ"
        />
        <input
          value={newWhy}
          onChange={(event) => setNewWhy(event.target.value)}
          placeholder="進みたい理由"
        />
        <Button
          variant="sun"
          disabled={busy || !newTitle.trim()}
          onClick={() => void create()}
        >
          <Plus size={17} />
          追加
        </Button>
      </Card>
      {error && (
        <Card className="board-error" role="alert">
          <p>{error}</p>
          <Button size="sm" onClick={() => void loadGoals()}>
            もう一度試す
          </Button>
        </Card>
      )}
      <div className="advanced-goal-layout">
        <Card className="advanced-goal-tree" aria-label="マップのツリー">
          {ordered.map((goal) => (
            <button
              type="button"
              className={selected === goal.id ? "active" : ""}
              style={{ paddingLeft: 14 + goal.depth * 18 }}
              key={goal.id}
              onClick={() => setSelected(goal.id)}
            >
              <span>{goal.depth ? "└" : ""}</span>
              <div>
                <strong>{goal.title}</strong>
                <small>
                  {goal.done_tasks}/{goal.open_tasks + goal.done_tasks} 完了
                </small>
              </div>
              {goal.status !== "active" && (
                <Chip>{goal.status === "done" ? "達成" : "保管中"}</Chip>
              )}
            </button>
          ))}
        </Card>
        <div>
          {detail ? (
            <Card className="advanced-goal-detail">
              <div className="goal-detail-actions">
                <Chip tone="grape">{detail.path.join(" / ")}</Chip>
                <div>
                  <Button
                    size="sm"
                    onClick={() =>
                      void changeStatus(
                        detail.status === "done" ? "active" : "done",
                      )
                    }
                  >
                    <Check size={16} />
                    {detail.status === "done" ? "進行中に戻す" : "達成"}
                  </Button>
                  <Button
                    size="sm"
                    onClick={() =>
                      void changeStatus(
                        detail.status === "archived" ? "active" : "archived",
                      )
                    }
                  >
                    <Archive size={16} />
                    {detail.status === "archived"
                      ? "進行中に戻す"
                      : "アーカイブ"}
                  </Button>
                </div>
              </div>
              <label>
                タイトル
                <input
                  value={detail.title}
                  onChange={(event) =>
                    setDetail({ ...detail, title: event.target.value })
                  }
                />
              </label>
              <label>
                進みたい理由
                <textarea
                  value={detail.why}
                  onChange={(event) =>
                    setDetail({ ...detail, why: event.target.value })
                  }
                />
              </label>
              <label>
                達成の条件
                <textarea
                  value={detail.success_criteria}
                  onChange={(event) =>
                    setDetail({
                      ...detail,
                      success_criteria: event.target.value,
                    })
                  }
                />
              </label>
              <div className="goal-detail-grid">
                <label>
                  目標日
                  <input
                    type="date"
                    value={detail.target_date ?? ""}
                    onChange={(event) =>
                      setDetail({
                        ...detail,
                        target_date: event.target.value || null,
                      })
                    }
                  />
                </label>
                <label>
                  親マップ
                  <select
                    value={detail.parent_id ?? ""}
                    onChange={(event) =>
                      setDetail({
                        ...detail,
                        parent_id: event.target.value
                          ? Number(event.target.value)
                          : null,
                      })
                    }
                  >
                    <option value="">親なし</option>
                    {goals
                      .filter(
                        (goal) =>
                          goal.id !== detail.id && goal.status === "active",
                      )
                      .map((goal) => (
                        <option key={goal.id} value={goal.id}>
                          {goal.title}
                        </option>
                      ))}
                  </select>
                </label>
              </div>
              <Button
                variant="sun"
                disabled={busy || !detail.title.trim()}
                onClick={() => void save()}
              >
                <Save size={17} />
                変更を保存
              </Button>
              <section className="advanced-goal-section">
                <h3>クエスト</h3>
                <div className="goal-assign-advanced">
                  <select
                    aria-label="マップに置くクエスト"
                    value={assignTask}
                    onChange={(event) => setAssignTask(event.target.value)}
                  >
                    <option value="">未完了のクエストを選ぶ</option>
                    {tasks
                      .filter(
                        (task) =>
                          task.status !== "done" && task.goal_id !== detail.id,
                      )
                      .map((task) => (
                        <option key={task.id} value={task.id}>
                          {task.title}
                        </option>
                      ))}
                  </select>
                  <Button
                    disabled={busy || !assignTask}
                    onClick={() => void assign()}
                  >
                    置く
                  </Button>
                </div>
                {detail.open_tasks.map((task) => (
                  <div className="goal-task-advanced" key={task.id}>
                    <button
                      type="button"
                      aria-label={`「${task.title}」をクリア`}
                      onClick={() => void complete(task)}
                    >
                      <Check />
                    </button>
                    <button type="button" onClick={() => openQuest(task)}>
                      {task.title}
                    </button>
                  </div>
                ))}
              </section>
              <section className="advanced-goal-section">
                <h3>子マップ</h3>
                {detail.subgoals.map((goal) => (
                  <button
                    className="subgoal-link"
                    key={goal.id}
                    onClick={() => setSelected(goal.id)}
                  >
                    {goal.title}
                  </button>
                ))}
                <div className="goal-assign-advanced">
                  <input
                    value={childTitle}
                    onChange={(event) => setChildTitle(event.target.value)}
                    placeholder="子マップのタイトル"
                  />
                  <Button
                    disabled={busy || !childTitle.trim()}
                    onClick={() => void create(detail.id)}
                  >
                    <Plus size={16} />
                    追加
                  </Button>
                </div>
              </section>
            </Card>
          ) : (
            <EmptyState title="マップを選んでください" />
          )}
        </div>
      </div>
    </section>
  );
}
