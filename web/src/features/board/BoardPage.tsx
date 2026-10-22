import { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  Check,
  Clock3,
  FileText,
  Mail,
  Pencil,
  ScrollText,
  X,
} from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { apiFetch } from "../../api/client";
import type {
  CandidateItem,
  GoalItem,
  ReportItem,
  TaskItem,
  UnsortedItem,
} from "../../api/types";
import { useGame } from "../game/GameContext";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { Chip } from "../../components/ui/Chip";
import { EmptyState } from "../../components/ui/EmptyState";
import { Segmented } from "../../components/ui/Segmented";

type Tab = "requests" | "memos" | "reports";

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

export function BoardPage() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) || "requests";
  const [candidates, setCandidates] = useState<CandidateItem[]>([]);
  const [memos, setMemos] = useState<UnsortedItem[]>([]);
  const [reports, setReports] = useState<ReportItem[]>([]);
  const [goals, setGoals] = useState<GoalItem[]>([]);
  const [candidateGoals, setCandidateGoals] = useState<
    Record<number, number | null>
  >({});
  const [editing, setEditing] = useState<CandidateItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const { refresh, showToast } = useGame();

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [candidateResult, memoResult, reportResult, goalResult] =
        await Promise.all([
          apiFetch<
            | {
                candidates?: CandidateItem[];
                pending_candidates?: CandidateItem[];
              }
            | CandidateItem[]
          >("/api/candidates"),
          apiFetch<
            | { unsorted?: UnsortedItem[]; unsorted_items?: UnsortedItem[] }
            | UnsortedItem[]
          >("/api/unsorted"),
          apiFetch<{ reports?: ReportItem[] } | ReportItem[]>("/api/reports"),
          apiFetch<{ goals: GoalItem[] }>("/api/goals"),
        ]);
      setCandidates(
        Array.isArray(candidateResult)
          ? candidateResult
          : (candidateResult.candidates ??
              candidateResult.pending_candidates ??
              []),
      );
      setMemos(
        Array.isArray(memoResult)
          ? memoResult
          : (memoResult.unsorted ?? memoResult.unsorted_items ?? []),
      );
      setReports(
        Array.isArray(reportResult)
          ? reportResult
          : (reportResult.reports ?? []),
      );
      setGoals(goalResult.goals);
    } catch (error) {
      setLoadError(errorMessage(error, "依頼を読み込めませんでした"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function runAction(key: string, action: () => Promise<void>) {
    if (busy) return;
    setBusy(key);
    setActionError(null);
    try {
      await action();
    } catch (error) {
      const message = errorMessage(error, "操作を完了できませんでした");
      setActionError(message);
      showToast(message);
    } finally {
      setBusy(null);
    }
  }

  async function approve(candidate: CandidateItem, edited = candidate) {
    await runAction(`candidate-${candidate.id}`, async () => {
      const result = await apiFetch<{
        task?: TaskItem | null;
        triage_bonus?: { xp: number } | null;
      }>(`/api/candidates/${candidate.id}/approve`, {
        body: {
          status: "today",
          title: edited.title,
          due_date: edited.due_date,
        },
      });
      const goalId = candidateGoals[candidate.id];
      if (result.task && goalId)
        await apiFetch(`/api/tasks/${result.task.id}`, {
          method: "PATCH",
          body: { goal_id: goalId },
        });
      setEditing(null);
      await Promise.all([load(), refresh()]);
      showToast(
        result.triage_bonus
          ? `受けました　+${result.triage_bonus.xp} XP`
          : "依頼を受けました",
      );
    });
  }
  async function reject(candidate: CandidateItem) {
    await runAction(`candidate-${candidate.id}`, async () => {
      const result = await apiFetch<{ triage_bonus?: { xp: number } | null }>(
        `/api/candidates/${candidate.id}/reject`,
        { body: {} },
      );
      await load();
      showToast(
        result.triage_bonus
          ? `見送りました　+${result.triage_bonus.xp} XP`
          : "見送りました",
      );
    });
  }
  async function sort(item: UnsortedItem, asKind: "task" | "memo") {
    await runAction(`memo-${item.id}`, async () => {
      const result = await apiFetch<{ triage_bonus?: { xp: number } | null }>(
        `/api/unsorted/${item.id}/sort`,
        { body: { as_kind: asKind, status: "today" } },
      );
      await Promise.all([load(), refresh()]);
      showToast(
        result.triage_bonus
          ? `仕分けました　+${result.triage_bonus.xp} XP`
          : "仕分けました",
      );
    });
  }
  async function decideReport(
    report: ReportItem,
    decision: "approve" | "reject",
  ) {
    await runAction(`report-${report.id}`, async () => {
      const result = await apiFetch<{ triage_bonus?: { xp: number } | null }>(
        `/api/reports/${report.id}/${decision}`,
        { body: {} },
      );
      await load();
      showToast(
        result.triage_bonus
          ? `確認しました　+${result.triage_bonus.xp} XP`
          : "確認しました",
      );
    });
  }

  const locked = Boolean(busy);
  return (
    <section className="game-screen board-screen">
      <header className="game-page-head">
        <div>
          <span className="eyebrow">
            <ScrollText size={16} />
            REQUEST BOARD
          </span>
          <h1>依頼ボード</h1>
          <p>届いたことを、受けるか見送るか自分で決めます。</p>
        </div>
      </header>
      <Segmented
        value={tab}
        onChange={(value) => setParams({ tab: value })}
        label="依頼ボードの種類"
        options={[
          { value: "requests", label: "依頼", count: candidates.length },
          { value: "memos", label: "メモ", count: memos.length },
          { value: "reports", label: "報告", count: reports.length },
        ]}
      />
      {actionError && (
        <Card className="board-error" role="alert">
          <AlertCircle />
          <p>{actionError}</p>
          <Button size="sm" onClick={() => setActionError(null)}>
            閉じる
          </Button>
        </Card>
      )}
      {loading ? (
        <Card className="board-loading" role="status">
          依頼を読み込んでいます…
        </Card>
      ) : loadError ? (
        <Card className="board-error" role="alert">
          <AlertCircle />
          <div>
            <strong>依頼を読み込めませんでした</strong>
            <p>{loadError}</p>
          </div>
          <Button onClick={() => void load()}>もう一度試す</Button>
        </Card>
      ) : (
        <>
          {tab === "requests" && (
            <div className="request-list">
              {candidates.length ? (
                candidates.map((candidate) => {
                  const itemBusy = busy === `candidate-${candidate.id}`;
                  return (
                    <Card key={candidate.id} className="request-card">
                      <header>
                        <Chip tone="sky">
                          <Mail size={14} />
                          {candidate.source_type}
                        </Chip>
                        <span className="elapsed">
                          <Clock3 size={14} />
                          {new Date(candidate.created_at).toLocaleDateString(
                            "ja-JP",
                          )}
                        </span>
                      </header>
                      {editing?.id === candidate.id ? (
                        <>
                          <input
                            className="edit-request-title"
                            value={editing.title}
                            disabled={locked}
                            onChange={(event) =>
                              setEditing({
                                ...editing,
                                title: event.target.value,
                              })
                            }
                          />
                          <input
                            type="date"
                            value={editing.due_date ?? ""}
                            disabled={locked}
                            onChange={(event) =>
                              setEditing({
                                ...editing,
                                due_date: event.target.value || null,
                              })
                            }
                          />
                        </>
                      ) : (
                        <h2>{candidate.title}</h2>
                      )}
                      <blockquote>
                        {candidate.excerpt || candidate.details}
                      </blockquote>
                      {candidate.due_date && (
                        <Chip tone="sky">{candidate.due_date}まで</Chip>
                      )}
                      <label className="candidate-goal">
                        冒険マップ
                        <select
                          disabled={locked}
                          value={candidateGoals[candidate.id] ?? ""}
                          onChange={(event) =>
                            setCandidateGoals({
                              ...candidateGoals,
                              [candidate.id]: event.target.value
                                ? Number(event.target.value)
                                : null,
                            })
                          }
                        >
                          <option value="">マップなし</option>
                          {goals.map((goal) => (
                            <option key={goal.id} value={goal.id}>
                              {"　".repeat(goal.depth)}
                              {goal.title}
                            </option>
                          ))}
                        </select>
                      </label>
                      <footer>
                        <Button
                          variant="quiet"
                          disabled={locked}
                          onClick={() => void reject(candidate)}
                        >
                          <X size={17} />
                          {itemBusy ? "処理中…" : "見送る"}
                        </Button>
                        {editing?.id === candidate.id ? (
                          <Button
                            disabled={locked || !editing.title.trim()}
                            onClick={() => void approve(candidate, editing)}
                          >
                            <Check size={17} />
                            {itemBusy ? "処理中…" : "直して受ける"}
                          </Button>
                        ) : (
                          <Button
                            disabled={locked}
                            onClick={() => setEditing({ ...candidate })}
                          >
                            <Pencil size={17} />
                            直して受ける
                          </Button>
                        )}
                        <Button
                          variant="sun"
                          disabled={locked}
                          onClick={() => void approve(candidate)}
                        >
                          <Check size={17} />
                          {itemBusy ? "処理中…" : "受ける"}
                        </Button>
                      </footer>
                    </Card>
                  );
                })
              ) : (
                <EmptyState title="届いている依頼はありません">
                  すべて整理されています。
                </EmptyState>
              )}
            </div>
          )}
          {tab === "memos" && (
            <div className="request-list">
              {memos.length ? (
                memos.map((memo) => {
                  const itemBusy = busy === `memo-${memo.id}`;
                  return (
                    <Card key={memo.id} className="memo-request">
                      <FileText />
                      <div>
                        <h2>{memo.title}</h2>
                        <p>{memo.raw_text}</p>
                      </div>
                      <footer>
                        <Button
                          disabled={locked}
                          onClick={() => void sort(memo, "memo")}
                        >
                          {itemBusy ? "処理中…" : "メモにしまう"}
                        </Button>
                        <Button
                          variant="sun"
                          disabled={locked}
                          onClick={() => void sort(memo, "task")}
                        >
                          {itemBusy ? "処理中…" : "クエストにする"}
                        </Button>
                      </footer>
                    </Card>
                  );
                })
              ) : (
                <EmptyState title="未仕分けのメモはありません">
                  気になることはミナに渡せます。
                </EmptyState>
              )}
            </div>
          )}
          {tab === "reports" && (
            <div className="request-list">
              {reports.length ? (
                reports.map((report) => {
                  const itemBusy = busy === `report-${report.id}`;
                  return (
                    <Card key={report.id} className="report-card">
                      <Chip tone="grape">ミナからの報告</Chip>
                      <h2>
                        {report.task_title ||
                          report.goal_title ||
                          report.title ||
                          report.task_text?.split("\n")[0] ||
                          "進み具合の報告"}
                      </h2>
                      <Chip tone="plain">
                        {report.task_id != null &&
                        report.suggested_status === "done"
                          ? "クリアの提案"
                          : report.task_id != null &&
                              report.suggested_status === "blocked"
                            ? "対応待ちの提案"
                            : "進捗の報告"}
                      </Chip>
                      <p>{report.summary || report.body}</p>
                      {report.details && (
                        <blockquote>{report.details}</blockquote>
                      )}
                      <p>
                        {report.task_id != null &&
                        report.suggested_status === "done"
                          ? "受け取ると、このクエストをクリアにします。"
                          : report.task_id != null &&
                              report.suggested_status === "blocked"
                            ? "受け取ると、記録を加えて重要度を上げます。"
                            : "受け取ると、対象に進捗の記録を加えます。"}
                      </p>
                      <footer>
                        <Button
                          variant="quiet"
                          disabled={locked}
                          onClick={() => void decideReport(report, "reject")}
                        >
                          {itemBusy ? "処理中…" : "見送る"}
                        </Button>
                        <Button
                          variant="sun"
                          disabled={locked}
                          onClick={() => void decideReport(report, "approve")}
                        >
                          {itemBusy ? "処理中…" : "受け取る"}
                        </Button>
                      </footer>
                    </Card>
                  );
                })
              ) : (
                <EmptyState title="新しい報告はありません">
                  届いた報告をここで確認できます。
                </EmptyState>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}
