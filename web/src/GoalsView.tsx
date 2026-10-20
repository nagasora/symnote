import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch, GoalBreakdownPreview, GoalContext, GoalItem, TaskItem } from "./api";

interface Props {
  goals: GoalItem[];
  tasks: TaskItem[];
  refresh: () => Promise<void>;
  showToast: (message: string) => void;
  onOpenTask: (task: TaskItem) => void;
  onCompleteTask: (task: TaskItem) => Promise<void>;
}

export function GoalsView(props: Props): React.ReactElement {
  const { goals, tasks, refresh, showToast, onOpenTask, onCompleteTask } = props;
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<GoalContext | null>(null);
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newWhy, setNewWhy] = useState("");
  const [childTitle, setChildTitle] = useState("");
  const [childWhy, setChildWhy] = useState("");
  const [assignTaskId, setAssignTaskId] = useState("");
  const [preview, setPreview] = useState<GoalBreakdownPreview | null>(null);
  const [selectedSubs, setSelectedSubs] = useState<Set<number>>(new Set());
  const [selectedTasks, setSelectedTasks] = useState<Set<number>>(new Set());
  const detailRequest = useRef(0);

  const loadDetail = useCallback(async (id: number) => {
    const request = ++detailRequest.current;
    const response = await apiFetch<{ goal: GoalContext }>(`/api/goals/${id}`);
    if (request === detailRequest.current) setDetail(response.goal);
  }, []);

  const selectGoal = (id: number) => {
    if (id === selectedId) return;
    detailRequest.current += 1;
    setDetail(null);
    setSelectedId(id);
  };

  useEffect(() => {
    if (!goals.length) {
      setSelectedId(null);
      setDetail(null);
    } else if (selectedId === null || !goals.some((goal) => goal.id === selectedId)) {
      setSelectedId(goals[0].id);
    }
  }, [goals, selectedId]);

  useEffect(() => {
    if (selectedId === null) return;
    detailRequest.current += 1;
    setDetail(null);
    setPreview(null);
    loadDetail(selectedId).catch((error) =>
      showToast(error instanceof Error ? error.message : "ゴールを読み込めませんでした"),
    );
  }, [selectedId, loadDetail, showToast]);

  const selectedSummary = goals.find((goal) => goal.id === selectedId) ?? null;
  const childCounts = useMemo(() => {
    const result = new Map<number, number>();
    goals.forEach((goal) => {
      if (goal.parent_id !== null) result.set(goal.parent_id, (result.get(goal.parent_id) ?? 0) + 1);
    });
    return result;
  }, [goals]);
  const visibleGoals = useMemo(() => {
    const byId = new Map(goals.map((goal) => [goal.id, goal]));
    return goals.filter((goal) => {
      let parent = goal.parent_id;
      const seen = new Set<number>();
      while (parent !== null && !seen.has(parent)) {
        if (collapsed.has(parent)) return false;
        seen.add(parent);
        parent = byId.get(parent)?.parent_id ?? null;
      }
      return true;
    });
  }, [goals, collapsed]);

  const reload = async () => {
    await refresh();
    if (selectedId !== null) await loadDetail(selectedId);
  };
  const fail = (error: unknown, fallback: string) =>
    showToast(error instanceof Error ? error.message : fallback);

  const createGoal = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!newTitle.trim() || busy) return;
    setBusy(true);
    try {
      const response = await apiFetch<{ goal: GoalItem }>("/api/goals", {
        body: { title: newTitle.trim(), why: newWhy.trim() },
      });
      setNewTitle(""); setNewWhy("");
      await refresh();
      selectGoal(response.goal.id);
      showToast("✓ 新しいゴールを作成しました");
    } catch (error) { fail(error, "ゴールを作成できませんでした"); }
    finally { setBusy(false); }
  };

  const saveGoal = async () => {
    if (!detail || !detail.title.trim() || busy) return;
    setBusy(true);
    try {
      await apiFetch(`/api/goals/${detail.id}`, { method: "PATCH", body: {
        title: detail.title.trim(), why: detail.why,
        success_criteria: detail.success_criteria, target_date: detail.target_date,
      }});
      await reload(); showToast("✓ ゴールの内容を保存しました");
    } catch (error) { fail(error, "ゴールを保存できませんでした"); }
    finally { setBusy(false); }
  };

  const toggleComplete = async () => {
    if (!detail || busy) return;
    setBusy(true);
    try {
      const completing = detail.status !== "done";
      await apiFetch(`/api/goals/${detail.id}`, { method: "PATCH", body: { status: completing ? "done" : "active" } });
      await reload(); showToast(completing ? "✓ ゴールを完了しました" : "ゴールを進行中に戻しました");
    } catch (error) { fail(error, "状態を更新できませんでした"); }
    finally { setBusy(false); }
  };

  const createChild = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!detail || !childTitle.trim() || busy) return;
    setBusy(true);
    try {
      await apiFetch("/api/goals", { body: { title: childTitle.trim(), why: childWhy.trim(), parent_id: detail.id } });
      setChildTitle(""); setChildWhy(""); await reload(); showToast("✓ 子ゴールを追加しました");
    } catch (error) { fail(error, "子ゴールを追加できませんでした"); }
    finally { setBusy(false); }
  };

  const patchTask = async (taskId: number, patch: { goal_id?: number; status?: "today" }) => {
    if (busy) return;
    setBusy(true);
    try {
      await apiFetch(`/api/tasks/${taskId}`, { method: "PATCH", body: patch });
      setAssignTaskId(""); await reload(); showToast("✓ タスクを更新しました");
    } catch (error) { fail(error, "タスクを更新できませんでした"); }
    finally { setBusy(false); }
  };

  const getBreakdown = async () => {
    if (!detail || busy) return;
    setBusy(true);
    try {
      const response = await apiFetch<GoalBreakdownPreview>(`/api/goals/${detail.id}/breakdown`, { body: {} });
      setPreview(response);
      setSelectedSubs(new Set());
      setSelectedTasks(new Set());
    } catch (error) { fail(error, "提案を作成できませんでした"); }
    finally { setBusy(false); }
  };

  const commitBreakdown = async () => {
    if (!detail || !preview || busy) return;
    if (!selectedSubs.size && !selectedTasks.size) {
      showToast("保存する提案を1件以上選んでください"); return;
    }
    setBusy(true);
    try {
      const response = await apiFetch<{ goal_ids: number[]; task_ids: number[] }>(
        `/api/goals/${detail.id}/breakdown/commit`,
        { body: {
          subgoals: preview.subgoals, tasks: preview.tasks,
          selected_subgoals: [...selectedSubs].sort((a, b) => a - b),
          selected_tasks: [...selectedTasks].sort((a, b) => a - b),
        }},
      );
      setPreview(null); await reload();
      showToast(`✓ 子ゴール${response.goal_ids.length}件、タスク${response.task_ids.length}件を保存しました`);
    } catch (error) { fail(error, "提案を保存できませんでした"); }
    finally { setBusy(false); }
  };

  const toggleIndex = (setter: React.Dispatch<React.SetStateAction<Set<number>>>, index: number) => {
    setter((current) => {
      const next = new Set(current);
      if (next.has(index)) next.delete(index); else next.add(index);
      return next;
    });
  };

  const updateSub = (index: number, patch: Partial<GoalBreakdownPreview["subgoals"][number]>) =>
    setPreview((current) => current ? {
      ...current,
      subgoals: current.subgoals.map((item, i) => i === index ? { ...item, ...patch } : item),
    } : current);
  const updateTask = (index: number, patch: Partial<GoalBreakdownPreview["tasks"][number]>) =>
    setPreview((current) => current ? {
      ...current,
      tasks: current.tasks.map((item, i) => i === index ? { ...item, ...patch } : item),
    } : current);

  return (
    <div className="goals-workspace">
      <form className="card goal-create" onSubmit={createGoal}>
        <input className="input-field" placeholder="新しいゴール" value={newTitle} onChange={(e) => setNewTitle(e.target.value)} />
        <input className="input-field" placeholder="なぜ達成したいか（任意）" value={newWhy} onChange={(e) => setNewWhy(e.target.value)} />
        <button className="btn primary" disabled={!newTitle.trim() || busy}>＋ ゴール追加</button>
      </form>

      {!goals.length ? <div className="card empty-box">まだゴールが登録されていません。</div> : (
        <div className="goals-layout">
          <div className="card goal-tree" aria-label="ゴール一覧">
            {visibleGoals.map((goal) => {
              const hasChildren = Boolean(childCounts.get(goal.id));
              const total = goal.open_tasks + goal.done_tasks;
              return <div key={goal.id} className={`goal-tree-row ${selectedId === goal.id ? "selected" : ""}`} style={{ paddingLeft: 10 + goal.depth * 20 }}>
                <button className="goal-collapse" type="button" aria-label="子ゴールの表示を切り替える" onClick={() => {
                  if (!hasChildren) return;
                  setCollapsed((current) => {
                    const next = new Set(current);
                    if (next.has(goal.id)) next.delete(goal.id); else next.add(goal.id);
                    return next;
                  });
                }}>{hasChildren ? (collapsed.has(goal.id) ? "▸" : "▾") : "·"}</button>
                <button type="button" className="goal-tree-main" disabled={busy} onClick={() => selectGoal(goal.id)}>
                  <span className={`goal-tree-title ${goal.status === "done" ? "done" : ""}`}>{goal.title}</span>
                  <span className="goal-tree-meta">
                    {total ? `${goal.done_tasks}/${total}件・${goal.progress_percent}%` : "タスクなし"}
                    {goal.stale_days && goal.stale_days > 0 ? ` ・ ${goal.stale_days}日更新なし` : ""}
                  </span>
                </button>
              </div>;
            })}
          </div>

          <section className="card goal-detail" aria-label="ゴール詳細">
            {!detail ? <div className="empty-box">ゴールを選んでください。</div> : <>
              <div className="goal-detail-head">
                <div><div className="goal-path">{detail.path.join(" / ")}</div><h2>ゴールの詳細</h2></div>
                <div className="goal-head-actions">
                  <button type="button" className="btn ghost" onClick={() => loadDetail(detail.id).catch((error) => fail(error, "ゴールを更新できませんでした"))}>↻ 更新</button>
                  <button type="button" className={detail.status === "done" ? "btn ghost" : "btn primary"} onClick={toggleComplete} disabled={busy}>
                    {detail.status === "done" ? "進行中に戻す" : "✓ ゴール完了"}
                  </button>
                </div>
              </div>
              {selectedSummary && <>
                <div className="goal-metrics">
                  <span>{selectedSummary.done_tasks}件完了</span><span>{selectedSummary.open_tasks}件未完了</span>
                  <span>{selectedSummary.progress_percent}%</span>
                  {selectedSummary.stale_days !== null && <span className={selectedSummary.stale_days >= 3 ? "stale" : ""}>最終活動から{selectedSummary.stale_days}日</span>}
                </div>
                <div className="progress goal-progress"><div className="progress-bar"><span style={{ width: `${selectedSummary.progress_percent}%` }} /></div></div>
              </>}

              <div className="goal-edit-grid">
                <label className="goal-field goal-field-wide"><span>タイトル</span><input className="input-field" value={detail.title} onChange={(e) => setDetail({ ...detail, title: e.target.value })} /></label>
                <label className="goal-field"><span>なぜ達成したいか</span><textarea value={detail.why} onChange={(e) => setDetail({ ...detail, why: e.target.value })} /></label>
                <label className="goal-field"><span>成功の条件</span><textarea value={detail.success_criteria} onChange={(e) => setDetail({ ...detail, success_criteria: e.target.value })} /></label>
                <label className="goal-field"><span>目標日</span><input type="date" className="input-field" value={detail.target_date ?? ""} onChange={(e) => setDetail({ ...detail, target_date: e.target.value || null })} /></label>
              </div>
              <div className="goal-save-row"><button type="button" className="btn primary" onClick={saveGoal} disabled={!detail.title.trim() || busy}>変更を保存</button></div>

              {selectedSummary?.next_task && <div className="goal-next-action">
                <div><span className="eyebrow">NEXT ACTION</span><b>{selectedSummary.next_task.title}</b><small>{selectedSummary.next_task.due_date ? `期限 ${selectedSummary.next_task.due_date}` : "期限なし"}{selectedSummary.next_task.estimate_minutes ? ` ・ 約${selectedSummary.next_task.estimate_minutes}分` : ""}</small></div>
                <button type="button" className="btn primary" disabled={busy || selectedSummary.next_task.status === "today"} onClick={() => patchTask(selectedSummary.next_task!.id, { status: "today" })}>
                  {selectedSummary.next_task.status === "today" ? "今日に設定済み" : "今日やる"}
                </button>
              </div>}

              <div className="goal-section">
                <div className="goal-section-head"><h3>タスク</h3><span>{detail.open_tasks.length}件の未完了</span></div>
                <div className="goal-assign-row">
                  <select className="input-field" value={assignTaskId} onChange={(e) => setAssignTaskId(e.target.value)}>
                    <option value="">既存の未完了タスクを選ぶ</option>
                    {tasks.filter((task) => task.status !== "done" && task.goal_id !== detail.id).map((task) =>
                      <option key={task.id} value={task.id}>{task.title}{task.goal_title ? `（${task.goal_title}から移動）` : ""}</option>,
                    )}
                  </select>
                  <button type="button" className="btn ghost" disabled={!assignTaskId || busy} onClick={() => patchTask(Number(assignTaskId), { goal_id: detail.id })}>割り当てる</button>
                </div>
                <div className="goal-task-list">
                  {!detail.open_tasks.length ? <div className="goal-empty-inline">未完了タスクはありません。</div> : detail.open_tasks.map((task) =>
                    <div key={task.id} className="goal-task-row">
                      <button type="button" className="check" disabled={busy} aria-label={`${task.title}を完了にする`} onClick={async () => {
                        if (busy) return;
                        setBusy(true);
                        try { await onCompleteTask(task); await loadDetail(detail.id); }
                        catch (error) { fail(error, "タスクを完了できませんでした"); }
                        finally { setBusy(false); }
                      }}>✓</button>
                      <button type="button" className="goal-task-title" onClick={() => onOpenTask(task)}>
                        {task.title}<small>{task.status === "today" ? "今日" : task.status === "week" ? "今週" : "あとで"}{task.due_date ? ` ・ ${task.due_date}` : ""}</small>
                      </button>
                    </div>,
                  )}
                </div>
              </div>

              <div className="goal-section">
                <div className="goal-section-head"><h3>子ゴール</h3><span>{detail.subgoals.length}件</span></div>
                {detail.subgoals.length > 0 && <div className="goal-subgoal-list">
                  {detail.subgoals.map((goal) => <button type="button" key={goal.id} disabled={busy} onClick={() => selectGoal(goal.id)}><span>{goal.status === "done" ? "✓" : "○"}</span><b>{goal.title}</b><span>›</span></button>)}
                </div>}
                <form className="goal-child-form" onSubmit={createChild}>
                  <input className="input-field" placeholder="子ゴールのタイトル" value={childTitle} onChange={(e) => setChildTitle(e.target.value)} />
                  <input className="input-field" placeholder="理由（任意）" value={childWhy} onChange={(e) => setChildWhy(e.target.value)} />
                  <button className="btn ghost" disabled={!childTitle.trim() || busy}>＋ 子ゴール</button>
                </form>
              </div>

              <div className="goal-section goal-ai-section">
                <div className="goal-section-head">
                  <div><h3>AIで分解</h3><span>提案を編集・選択してから保存します。</span></div>
                  <button type="button" className="btn ghost" disabled={busy} onClick={getBreakdown}>{busy ? "処理中…" : "✦ 提案を作る"}</button>
                </div>
                {preview && <div className="goal-breakdown">
                  <div className="goal-breakdown-note"><span className={`chip ${preview.ai_used ? "ai" : ""}`}>{preview.ai_used ? "AI提案" : "標準提案"}</span>まだ保存されていません。</div>
                  {preview.subgoals.map((item, index) => <div className="goal-proposal" key={`sub-${index}`}>
                    <input type="checkbox" aria-label={`子ゴール案${index + 1}を選択`} checked={selectedSubs.has(index)} onChange={() => toggleIndex(setSelectedSubs, index)} />
                    <div><span className="proposal-kind">子ゴール</span><input className="input-field" value={item.title} onChange={(e) => updateSub(index, { title: e.target.value })} /><textarea placeholder="理由" value={item.why} onChange={(e) => updateSub(index, { why: e.target.value })} /></div>
                  </div>)}
                  {preview.tasks.map((item, index) => <div className="goal-proposal" key={`task-${index}`}>
                    <input type="checkbox" aria-label={`タスク案${index + 1}を選択`} checked={selectedTasks.has(index)} onChange={() => toggleIndex(setSelectedTasks, index)} />
                    <div><span className="proposal-kind">タスク</span><input className="input-field" value={item.title} onChange={(e) => updateTask(index, { title: e.target.value })} /><textarea placeholder="理由" value={item.why} onChange={(e) => updateTask(index, { why: e.target.value })} />
                      <div className="proposal-options">
                        <select className="input-field" value={item.due_hint} onChange={(e) => updateTask(index, { due_hint: e.target.value as GoalBreakdownPreview["tasks"][number]["due_hint"] })}><option value="">期日指定なし</option><option value="今日">今日</option><option value="今週">今週</option><option value="来週">来週</option><option value="今月">今月</option></select>
                        <label><input type="number" min="1" max="1440" value={item.estimate_minutes ?? ""} onChange={(e) => updateTask(index, { estimate_minutes: e.target.value ? Number(e.target.value) : null })} /> 分</label>
                      </div>
                    </div>
                  </div>)}
                  <div className="goal-breakdown-actions"><button type="button" className="btn quiet" onClick={() => setPreview(null)}>取り消す</button><button type="button" className="btn primary" disabled={busy} onClick={commitBreakdown}>選択した提案を保存</button></div>
                </div>}
              </div>

              {detail.done_tasks.length > 0 && <details className="goal-done-tasks"><summary>完了済みタスク（{detail.done_tasks.length}件）</summary>{detail.done_tasks.map((task) => <button type="button" key={task.id} onClick={() => onOpenTask(task)}>✓ {task.title}</button>)}</details>}
            </>}
          </section>
        </div>
      )}
    </div>
  );
}
