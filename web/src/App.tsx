import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  apiFetch,
  AuthState,
  CandidateItem,
  GoalItem,
  MemoItem,
  QuickPreviewResult,
  SplitDraftItem,
  TaskItem,
  TodayPayload,
  UnsortedItem,
} from "./api";
import { GoalsView } from "./GoalsView";
import { isNativeApp, nativeSignIn, nativeSignOut } from "./nativeClient";
import { Share } from "@capacitor/share";

type ScreenId = "today" | "inbox" | "all" | "ideas" | "settings";

interface ToastState {
  message: string;
  undoLabel?: string;
  onUndo?: () => Promise<void>;
}

const SCREEN_TITLES: Record<ScreenId, string> = {
  today: "今日",
  inbox: "受信箱",
  all: "すべて",
  ideas: "ひらめき",
  settings: "設定",
};

export function App(): React.ReactElement {
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    const saved = localStorage.getItem("symnote_theme");
    return saved === "dark" ? "dark" : "light";
  });
  const [auth, setAuth] = useState<AuthState | null>(null);
  const [authLoadError, setAuthLoadError] = useState("");
  const [authReload, setAuthReload] = useState(0);
  const [nativeLoginPending, setNativeLoginPending] = useState(false);
  const [nativeLogoutPending, setNativeLogoutPending] = useState(false);
  const [screen, setScreen] = useState<ScreenId>("today");
  const [todayData, setTodayData] = useState<TodayPayload | null>(null);
  const [inboxData, setInboxData] = useState<{
    pending_candidates: CandidateItem[];
    unsorted_items: UnsortedItem[];
  } | null>(null);
  const [allTasks, setAllTasks] = useState<TaskItem[]>([]);
  const [allMemos, setAllMemos] = useState<MemoItem[]>([]);
  const [goals, setGoals] = useState<GoalItem[]>([]);
  const [ideasData, setIdeasData] = useState<{
    sessions: Array<{ id: number; title: string; summary: string }>;
    active_session_id: number | null;
    tree: Array<{
      id: number;
      parent_id: number | null;
      kind: string;
      title: string;
      body: string;
      linked_item_id: number | null;
      children: unknown[];
    }>;
  } | null>(null);
  const [settingsData, setSettingsData] = useState<{
    ai_usage: { today_used: number; daily_limit: number; remaining: number };
    calendar_connected: boolean;
    owner_features_enabled: boolean;
    invites?: Array<{ email: string; is_owner: boolean; note: string; accepted_at: string | null }>;
  } | null>(null);

  const [selectedTask, setSelectedTask] = useState<TaskItem | null>(null);
  const [selectedMemo, setSelectedMemo] = useState<MemoItem | null>(null);
  const [completedOpen, setCompletedOpen] = useState<boolean>(false);

  // 頭の中をまるごと書き出す（Brain Dump）
  const [brainText, setBrainText] = useState<string>("");
  const [brainSplitting, setBrainSplitting] = useState<boolean>(false);
  const [brainDrafts, setBrainDrafts] = useState<SplitDraftItem[] | null>(null);
  const [brainAiUsed, setBrainAiUsed] = useState<boolean>(false);

  // クイック入力モーダル (⌘K / スマホ中央＋ボタン)
  const [captureOpen, setCaptureOpen] = useState<boolean>(false);
  const [captureText, setCaptureText] = useState<string>("");
  const [capturePreview, setCapturePreview] = useState<QuickPreviewResult | null>(null);
  const [captureKind, setCaptureKind] = useState<"auto" | "task" | "memo" | "unsorted">("auto");
  const [captureSaving, setCaptureSaving] = useState<boolean>(false);

  // 受信箱のタブ & 編集モード
  const [inboxTab, setInboxTab] = useState<"candidates" | "unsorted">("candidates");
  const [editingCandidateId, setEditingCandidateId] = useState<number | null>(null);
  const [editingCandidateTitle, setEditingCandidateTitle] = useState<string>("");
  const [editingCandidateDue, setEditingCandidateDue] = useState<string>("");

  // すべて画面のフィルタ・検索・ビュー切替
  const [allViewMode, setAllViewMode] = useState<"list" | "goals" | "calendar">("list");
  const [allFilter, setAllFilter] = useState<"all" | "task" | "memo" | "recurring" | "done">("all");
  const [searchQuery, setSearchQuery] = useState<string>("");

  // ひらめき（マインドマップ）追加用
  const [newSessionTitle, setNewSessionTitle] = useState<string>("");
  const [newNodeTitle, setNewNodeTitle] = useState<string>("");
  const [newNodeParentId, setNewNodeParentId] = useState<number | null>(null);

  // 設定（招待管理）
  const [inviteEmail, setInviteEmail] = useState<string>("");
  const [inviteNote, setInviteNote] = useState<string>("");

  // ログインフォーム
  const [passcodeInput, setPasscodeInput] = useState<string>("");
  const [e2eEmail, setE2eEmail] = useState<string>("owner@example.com");
  const [loginError, setLoginError] = useState<string>("");
  const [onboardName, setOnboardName] = useState<string>("");

  // トースト（5秒間 Undo）
  const [toast, setToast] = useState<ToastState | null>(null);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("symnote_theme", theme);
  }, [theme]);

  const showToast = useCallback(
    (message: string, undoLabel?: string, onUndo?: () => Promise<void>) => {
      setToast({ message, undoLabel, onUndo });
    },
    [],
  );

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 5500);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const refreshAllData = useCallback(async () => {
    const [tData, iData, tasksRes, memosRes, goalsRes] = await Promise.all([
      apiFetch<TodayPayload>("/api/today"),
      apiFetch<{ pending_candidates: CandidateItem[]; unsorted_items: UnsortedItem[] }>(
        "/api/inbox",
      ),
      apiFetch<{ tasks: TaskItem[] }>("/api/tasks?status=all&include_done=true"),
      apiFetch<{ memos: MemoItem[] }>("/api/memos"),
      apiFetch<{ goals: GoalItem[] }>("/api/goals"),
    ]);
    setTodayData(tData);
    setInboxData(iData);
    setAllTasks(tasksRes.tasks);
    setAllMemos(memosRes.memos);
    setGoals(goalsRes.goals);
    setSelectedTask((prev) =>
      prev ? tasksRes.tasks.find((x) => x.id === prev.id) ?? null : null,
    );
    setSelectedMemo((prev) =>
      prev ? memosRes.memos.find((x) => x.id === prev.id) ?? null : null,
    );
  }, []);

  const loadIdeas = useCallback(async (sessionId?: number) => {
    const q = sessionId ? `?session_id=${sessionId}` : "";
    const res = await apiFetch<{
      sessions: Array<{ id: number; title: string; summary: string }>;
      active_session_id: number | null;
      tree: Array<{
        id: number;
        parent_id: number | null;
        kind: string;
        title: string;
        body: string;
        linked_item_id: number | null;
        children: unknown[];
      }>;
    }>(`/api/ideas${q}`);
    setIdeasData(res);
  }, []);

  const loadSettings = useCallback(async () => {
    const res = await apiFetch<{
      ai_usage: { today_used: number; daily_limit: number; remaining: number };
      calendar_connected: boolean;
      owner_features_enabled: boolean;
      invites?: Array<{
        email: string;
        is_owner: boolean;
        note: string;
        accepted_at: string | null;
      }>;
    }>("/api/settings");
    setSettingsData(res);
  }, []);

  useEffect(() => {
    setAuthLoadError("");
    apiFetch<AuthState>("/api/auth/me")
      .then((a) => {
        setAuth(a);
        if (a.authenticated) {
          setOnboardName(a.user?.name || "");
          refreshAllData().catch(console.error);
        }
      })
      .catch(() => setAuthLoadError("サーバーに接続できませんでした。通信状況を確認してください。"));
  }, [refreshAllData, authReload]);

  useEffect(() => {
    if (!auth?.authenticated) return;
    if (screen === "ideas") loadIdeas().catch(console.error);
    if (screen === "settings") loadSettings().catch(console.error);
  }, [auth?.authenticated, screen, loadIdeas, loadSettings]);

  // ⌘K ショートカット / Escape
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCaptureOpen((v) => !v);
      } else if (e.key === "Escape") {
        setCaptureOpen(false);
        setSelectedTask(null);
        setSelectedMemo(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // クイック入力のリアルタイム解析プレビュー
  useEffect(() => {
    if (!captureOpen || !captureText.trim()) {
      setCapturePreview(null);
      return;
    }
    const timer = window.setTimeout(() => {
      apiFetch<QuickPreviewResult>("/api/capture/preview", {
        body: { text: captureText },
      })
        .then(setCapturePreview)
        .catch(() => {});
    }, 120);
    return () => window.clearTimeout(timer);
  }, [captureOpen, captureText]);

  // --- アクション群 ---
  const handleCompleteTask = async (task: TaskItem, e?: React.MouseEvent) => {
    e?.stopPropagation();
    if (task.status === "done") {
      await apiFetch(`/api/tasks/${task.id}/reopen`, {
        body: { status: "today" },
      });
      await refreshAllData();
      showToast(`「${task.title}」を未完了に戻しました`);
      return;
    }
    const prevStatus = task.status;
    const res = await apiFetch<{ task: TaskItem; next_task: TaskItem | null }>(
      `/api/tasks/${task.id}/complete`,
      { body: {} },
    );
    await refreshAllData();
    showToast(`✓「${task.title}」を完了しました`, "元に戻す", async () => {
      await apiFetch(`/api/tasks/${task.id}/reopen`, {
        body: {
          status: prevStatus,
          discard_next_task_id: res.next_task?.id ?? null,
        },
      });
      await refreshAllData();
    });
  };

  const handleBreakdownTask = async (taskId: number) => {
    const res = await apiFetch<{ subtasks: string[]; ai_used: boolean }>(
      `/api/tasks/${taskId}/breakdown`,
      { body: {} },
    );
    const todayIso = todayData?.today ?? new Date().toISOString().slice(0, 10);
    for (const stepTitle of res.subtasks) {
      await apiFetch("/api/tasks", {
        body: {
          title: stepTitle,
          status: "today",
          due_date: todayIso,
          estimate_minutes: 15,
        },
      });
    }
    await refreshAllData();
    showToast(
      `✦ ${res.subtasks.length}個の具体的なステップに分解して「今日」に追加しました`,
    );
  };

  const handleBrainSplit = async () => {
    if (!brainText.trim()) return;
    setBrainSplitting(true);
    try {
      const res = await apiFetch<{ items: SplitDraftItem[]; ai_used: boolean }>(
        "/api/capture/split",
        { body: { text: brainText } },
      );
      setBrainDrafts(res.items);
      setBrainAiUsed(res.ai_used);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "分解に失敗しました");
    } finally {
      setBrainSplitting(false);
    }
  };

  const handleBrainCommit = async () => {
    if (!brainDrafts || brainDrafts.length === 0) return;
    await apiFetch("/api/capture/commit-split", {
      body: {
        source_text: brainText,
        save_source_memo: true,
        items: brainDrafts,
      },
    });
    const count = brainDrafts.length;
    setBrainText("");
    setBrainDrafts(null);
    await refreshAllData();
    showToast(`✓ ${count}件のタスクをまとめて登録しました`);
  };

  const handleQuickCaptureSubmit = async () => {
    if (!captureText.trim() || captureSaving) return;
    setCaptureSaving(true);
    try {
      await apiFetch("/api/capture", {
        body: {
          text: captureText,
          kind: captureKind,
          status: "today",
        },
      });
      setCaptureText("");
      setCaptureOpen(false);
      await refreshAllData();
      showToast("✓ ノートに追加しました");
    } catch (err) {
      showToast(err instanceof Error ? err.message : "保存に失敗しました");
    } finally {
      setCaptureSaving(false);
    }
  };

  const handleApproveCandidate = async (
    c: CandidateItem,
    overrides?: { title?: string; due_date?: string | null },
  ) => {
    await apiFetch(`/api/candidates/${c.id}/approve`, {
      body: {
        status: "today",
        title: overrides?.title ?? c.title,
        due_date: overrides?.due_date !== undefined ? overrides.due_date : c.due_date,
      },
    });
    setEditingCandidateId(null);
    await refreshAllData();
    showToast(`✓「${overrides?.title ?? c.title}」を今日のタスクに追加しました`);
  };

  const handleRejectCandidate = async (c: CandidateItem) => {
    await apiFetch(`/api/candidates/${c.id}/reject`, { body: {} });
    await refreshAllData();
    showToast(`「${c.title}」を見送りました`, "元に戻す", async () => {
      await apiFetch(`/api/candidates/${c.id}/restore`, { body: {} });
      await refreshAllData();
    });
  };

  const handleSortUnsorted = async (item: UnsortedItem, asKind: "task" | "memo") => {
    await apiFetch(`/api/unsorted/${item.id}/sort`, {
      body: { as_kind: asKind, status: "today" },
    });
    await refreshAllData();
    showToast(
      asKind === "task"
        ? `✓「${item.title}」を今日のタスクにしました`
        : `✓「${item.title}」をメモとして保存しました`,
    );
  };

  const handleTaskFieldPatch = async (
    task: TaskItem,
    patch: Partial<{
      title: string;
      details: string;
      status: "inbox" | "today" | "week" | "done";
      due_date: string | null;
      due_time: string | null;
      importance: number;
      goal_id: number | null;
      estimate_minutes: number | null;
    }>,
  ) => {
    const res = await apiFetch<{ task: TaskItem }>(`/api/tasks/${task.id}`, {
      method: "PATCH",
      body: patch,
    });
    setSelectedTask(res.task);
    await refreshAllData();
  };

  const handleDeleteTask = async (task: TaskItem) => {
    await apiFetch(`/api/tasks/${task.id}`, { method: "DELETE" });
    setSelectedTask(null);
    await refreshAllData();
    showToast(`「${task.title}」を削除しました`);
  };

  const handleMemoPatch = async (memo: MemoItem, raw_text: string) => {
    const res = await apiFetch<{ memo: MemoItem }>(`/api/memos/${memo.id}`, {
      method: "PATCH",
      body: { raw_text },
    });
    setSelectedMemo(res.memo);
    await refreshAllData();
  };

  const handleMemoToTask = async (memo: MemoItem) => {
    const res = await apiFetch<{ task: TaskItem }>(`/api/memos/${memo.id}/to-task`, {
      body: { status: "today", keep_memo: false },
    });
    setSelectedMemo(null);
    setSelectedTask(res.task);
    await refreshAllData();
    showToast(`✓ メモをタスクに変換しました`);
  };

  // --- フィルタリングされた一覧 ---
  const filteredItems = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    const list: Array<{ type: "task"; item: TaskItem } | { type: "memo"; item: MemoItem }> = [];
    if (allFilter !== "memo") {
      for (const t of allTasks) {
        if (allFilter === "done" && t.status !== "done") continue;
        if (allFilter !== "done" && allFilter !== "all" && t.status === "done") continue;
        if (allFilter === "recurring" && !t.recurrence) continue;
        if (
          q &&
          !t.title.toLowerCase().includes(q) &&
          !t.details.toLowerCase().includes(q)
        ) {
          continue;
        }
        list.push({ type: "task", item: t });
      }
    }
    if (allFilter === "all" || allFilter === "memo") {
      for (const m of allMemos) {
        if (q && !m.raw_text.toLowerCase().includes(q)) continue;
        list.push({ type: "memo", item: m });
      }
    }
    return list;
  }, [allTasks, allMemos, allFilter, searchQuery]);

  // --- 1. ローディング / 未ログイン画面 ---
  if (!auth) {
    return (
      <div className="auth-wrap">
        <div className="card auth-card" style={{ textAlign: "center" }}>
          <div className="brand-mark" style={{ margin: "0 auto 12px" }}>
            S
          </div>
          <p role="status" style={{ margin: 0, color: "var(--ink-2)" }}>{authLoadError || "ノートを開いています…"}</p>
          {authLoadError && <button className="btn" style={{ marginTop: 16 }} onClick={() => setAuthReload((value) => value + 1)}>もう一度接続する</button>}
        </div>
      </div>
    );
  }

  if (!auth.authenticated) {
    const params = new URLSearchParams(window.location.search);
    const oauthErr = params.get("auth_error");
    return (
      <div className="auth-wrap">
        <div className="card auth-card">
          <div className="brand" style={{ padding: 0, marginBottom: 16 }}>
            <span className="brand-mark">S</span>
            <span className="brand-name">SymNote</span>
          </div>
          <h1 className="serif" style={{ fontSize: 24, margin: "0 0 8px" }}>
            AI時代のやわらかい紙のノート
          </h1>
          <p style={{ color: "var(--ink-2)", fontSize: 14, margin: "0 0 20px" }}>
            頭の中を書き出すだけでAIが整理し、今日やるべき1件に集中できます（招待制ベータ）。
          </p>

          {(loginError || oauthErr) && (
            <div
              className="chip danger"
              style={{
                height: "auto",
                padding: "8px 12px",
                marginBottom: 16,
                display: "block",
                whiteSpace: "normal",
              }}
            >
              {loginError ||
                (oauthErr === "not_invited"
                  ? "このGoogleアカウントはまだ招待されていません。管理者に招待を依頼してください。"
                  : "ログインに失敗しました。もう一度お試しください。")}
            </div>
          )}

          {auth.google_enabled && isNativeApp() && auth.mobile_enabled && (
            <button className="btn primary" disabled={nativeLoginPending} style={{ width: "100%", minHeight: 44, marginBottom: 12 }} onClick={async () => {
              setLoginError("");
              setNativeLoginPending(true);
              try { await nativeSignIn(); }
              catch { setLoginError("ログインを完了できませんでした。もう一度お試しください。"); }
              finally { setNativeLoginPending(false); }
            }}>{nativeLoginPending ? "ログインを確認中…" : "Google アカウントでログイン"}</button>
          )}
          {auth.google_enabled && isNativeApp() && !auth.mobile_enabled && (
            <p role="status">このサーバーではアプリからのログインをまだ利用できません。更新後にもう一度お試しください。</p>
          )}
          {auth.google_enabled && !isNativeApp() && (
            <a
              href="/api/auth/google/start"
              className="btn primary"
              style={{
                width: "100%",
                height: 44,
                textDecoration: "none",
                marginBottom: 12,
              }}
            >
              Google アカウントでログイン
            </a>
          )}

          {auth.passcode_enabled && (
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                setLoginError("");
                try {
                  const res = await apiFetch<{ user: AuthState["user"] }>(
                    "/api/auth/login",
                    { body: { passcode: passcodeInput } },
                  );
                  setAuth({ ...auth, authenticated: true, user: res.user });
                  await refreshAllData();
                } catch (err) {
                  setLoginError(
                    err instanceof Error ? err.message : "ログインに失敗しました",
                  );
                }
              }}
              style={{ marginTop: 12 }}
            >
              <label
                style={{
                  display: "block",
                  fontSize: 13,
                  color: "var(--ink-2)",
                  marginBottom: 6,
                }}
              >
                パスコードでログイン
              </label>
              <div style={{ display: "flex", gap: 8 }}>
                <input
                  type="password"
                  className="input-field"
                  placeholder="パスコードを入力"
                  value={passcodeInput}
                  onChange={(e) => setPasscodeInput(e.target.value)}
                />
                <button
                  type="submit"
                  className="btn primary"
                  style={{ height: 42 }}
                >
                  開く
                </button>
              </div>
            </form>
          )}

          {auth.e2e_enabled && (
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                setLoginError("");
                try {
                  const res = await apiFetch<{ user: AuthState["user"] }>(
                    "/api/auth/e2e-login",
                    { body: { email: e2eEmail, name: "テストユーザー" } },
                  );
                  setAuth({ ...auth, authenticated: true, user: res.user });
                  await refreshAllData();
                } catch (err) {
                  setLoginError(
                    err instanceof Error ? err.message : "ログインに失敗しました",
                  );
                }
              }}
              style={{
                marginTop: 16,
                paddingTop: 16,
                borderTop: "1px solid var(--line)",
              }}
            >
              <label
                style={{
                  display: "block",
                  fontSize: 12,
                  color: "var(--ink-3)",
                  marginBottom: 6,
                }}
              >
                開発・テスト用ログイン (SYMNOTE_E2E)
              </label>
              <div style={{ display: "flex", gap: 8 }}>
                <input
                  type="email"
                  className="input-field"
                  value={e2eEmail}
                  onChange={(e) => setE2eEmail(e.target.value)}
                />
                <button type="submit" className="btn ghost" style={{ height: 42 }}>
                  ログイン
                </button>
              </div>
            </form>
          )}
        </div>
      </div>
    );
  }

  // --- 2. 初回オンボーディング ---
  if (auth.user && !auth.user.onboarded) {
    return (
      <div className="auth-wrap">
        <div className="card auth-card">
          <span className="chip ai" style={{ marginBottom: 12 }}>
            ✦ ようこそ SymNote へ
          </span>
          <h1 className="serif" style={{ fontSize: 24, margin: "0 0 8px" }}>
            あなたのノートを準備しました
          </h1>
          <p style={{ color: "var(--ink-2)", fontSize: 14, margin: "0 0 20px" }}>
            あなたのデータは専用領域に安全に分離されています。表示名を確認して始めましょう。
          </p>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              const res = await apiFetch<{ user: AuthState["user"] }>(
                "/api/auth/onboarding",
                {
                  body: {
                    name: onboardName || auth.user?.name || "ユーザー",
                    seed_sample_tasks: true,
                  },
                },
              );
              setAuth({ ...auth, user: res.user });
              await refreshAllData();
            }}
          >
            <label
              style={{
                display: "block",
                fontSize: 13,
                color: "var(--ink-2)",
                marginBottom: 6,
              }}
            >
              表示名
            </label>
            <input
              type="text"
              className="input-field"
              value={onboardName}
              onChange={(e) => setOnboardName(e.target.value)}
              placeholder="あなたの名前"
              style={{ marginBottom: 16 }}
            />
            <button
              type="submit"
              className="btn primary"
              style={{ width: "100%", height: 44 }}
            >
              サンプルを入れてノートを開く
            </button>
          </form>
        </div>
      </div>
    );
  }

  const todayCount =
    (todayData?.overdue.length ?? 0) + (todayData?.due_today.length ?? 0);
  const inboxCount = todayData?.inbox_badge_count ?? 0;
  const totalTodayTarget =
    todayCount + (todayData?.completed_today.length ?? 0);
  const doneTodayCount = todayData?.completed_today.length ?? 0;
  const progressPct =
    totalTodayTarget > 0
      ? Math.round((doneTodayCount / totalTodayTarget) * 100)
      : 0;

  const upcomingWeekTasks = allTasks.filter(
    (t) =>
      t.status !== "done" &&
      !todayData?.overdue.some((o) => o.id === t.id) &&
      !todayData?.due_today.some((d) => d.id === t.id),
  );

  const renderTaskRow = (t: TaskItem, isOverdue = false) => {
    const isSelected = selectedTask?.id === t.id;
    const isDone = t.status === "done";
    return (
      <div
        key={t.id}
        className={`task ${isDone ? "done" : ""} ${
          t.importance >= 4 ? "important" : ""
        } ${isSelected ? "selected" : ""}`}
        onClick={() => {
          setSelectedMemo(null);
          setSelectedTask(t);
        }}
      >
        <button
          type="button"
          className="check"
          aria-label="完了にする"
          onClick={(e) => handleCompleteTask(t, e)}
        >
          ✓
        </button>
        <div className="task-body">
          <div className="task-title">{t.title}</div>
          <div className="task-meta">
            {(t.due_date || t.due_time) && (
              <span className={`m num ${isOverdue && !isDone ? "danger" : ""}`}>
                {t.due_date ?? ""} {t.due_time ?? ""}
              </span>
            )}
            {t.estimate_minutes && (
              <span className="m num">約{t.estimate_minutes}分</span>
            )}
            {t.recurrence && (
              <span className="m">↻ {t.recurrence.label}</span>
            )}
            {t.goal_title && <span className="chip">{t.goal_title}</span>}
            {t.source && (
              <span className="chip ai">✦ {t.source.type}から追加</span>
            )}
          </div>
        </div>
      </div>
    );
  };

  const detailOpen = Boolean(selectedTask || selectedMemo);

  return (
    <div className={`app ${detailOpen ? "detail-open" : ""}`}>
      {/* ───────── 左サイドバー（PC / タブレット） ───────── */}
      <aside className="sidebar" aria-label="メインナビゲーション">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            S
          </span>
          <span className="brand-name">SymNote</span>
        </div>

        <button
          type="button"
          className="capture-btn"
          onClick={() => setCaptureOpen(true)}
        >
          <span>＋ すぐ書く…</span>
          <kbd className="kbd">⌘K</kbd>
        </button>

        <nav className="nav">
          <button
            type="button"
            className={`nav-link ${screen === "today" ? "active" : ""}`}
            onClick={() => setScreen("today")}
          >
            <span>☀ 今日</span>
            <span className="count num">{todayCount}</span>
          </button>
          <button
            type="button"
            className={`nav-link ${screen === "inbox" ? "active" : ""}`}
            onClick={() => setScreen("inbox")}
          >
            <span>📥 受信箱</span>
            {inboxCount > 0 && <span className="count ai num">{inboxCount}</span>}
          </button>
          <button
            type="button"
            className={`nav-link ${screen === "all" ? "active" : ""}`}
            onClick={() => setScreen("all")}
          >
            <span>☰ すべて</span>
            <span className="count num">
              {allTasks.filter((t) => t.status !== "done").length +
                allMemos.length}
            </span>
          </button>
          <button
            type="button"
            className={`nav-link ${screen === "ideas" ? "active" : ""}`}
            onClick={() => setScreen("ideas")}
          >
            <span>✦ ひらめき</span>
          </button>
        </nav>

        <div className="sidebar-foot">
          <div className="local-note">
            <span className="dot" />
            <span>{auth.user?.name || auth.user?.email}</span>
          </div>
          <nav className="nav">
            <button
              type="button"
              className="nav-link"
              onClick={() => setTheme(theme === "light" ? "dark" : "light")}
            >
              <span>{theme === "light" ? "🌙 ダーク表示" : "☀ ライト表示"}</span>
            </button>
            <button
              type="button"
              className={`nav-link ${screen === "settings" ? "active" : ""}`}
              onClick={() => setScreen("settings")}
            >
              <span>⚙ 設定</span>
            </button>
          </nav>
        </div>
      </aside>

      {/* ───────── メイン領域 ───────── */}
      <main className="main">
        {/* スマホ用ヘッダー（Safe Area 対応） */}
        <header className="mobile-header">
          <span className="brand-mark" aria-hidden="true">
            S
          </span>
          <strong className="serif" style={{ fontSize: 17 }}>
            {SCREEN_TITLES[screen]}
          </strong>
          <span className="spacer" />
          <button
            type="button"
            className="btn quiet"
            aria-label="テーマ切替"
            onClick={() => setTheme(theme === "light" ? "dark" : "light")}
          >
            {theme === "light" ? "🌙" : "☀"}
          </button>
          <button
            type="button"
            className="btn quiet"
            aria-label="設定を開く"
            onClick={() => setScreen("settings")}
          >
            ⚙ 設定
          </button>
        </header>

        <div className="content">
          {/* ══════════ 1. 今日 ══════════ */}
          {screen === "today" && (
            <section className="screen">
              <header className="page-head">
                <div className="eyebrow num">TODAY · {todayData?.today}</div>
                <h1 className="page-title">
                  今日
                  {todayData?.weekday_ja && (
                    <small>（{todayData.weekday_ja}）</small>
                  )}
                </h1>
                <p className="page-lead">
                  {todayCount === 0 ? (
                    <>今日のタスクはすべて片付いています。穏やかな一日を。</>
                  ) : (
                    <>
                      今日は <b>あと {todayCount} 件</b>。
                      {(todayData?.overdue.length ?? 0) > 0 && (
                        <>
                          {" "}
                          うち{" "}
                          <span className="warn">
                            期限切れ {todayData?.overdue.length} 件
                          </span>{" "}
                          を先に片付けましょう。
                        </>
                      )}
                    </>
                  )}
                </p>
                <div className="progress">
                  <div className="progress-bar" aria-hidden="true">
                    <span style={{ width: `${progressPct}%` }} />
                  </div>
                  <span className="num">
                    今日 {doneTodayCount} / {totalTodayTarget} 完了
                  </span>
                </div>
              </header>

              {/* いま着手する1件（AI ネクストアクション） */}
              {todayData?.next_action && (
                <div className="card next-action-card">
                  <div className="next-action-head">
                    <span>✦ いま着手する1件（AIおすすめ）</span>
                    {todayData.next_action.estimate_minutes && (
                      <span className="chip accent num">
                        約{todayData.next_action.estimate_minutes}分
                      </span>
                    )}
                  </div>
                  <div className="next-action-title">
                    {todayData.next_action.title}
                  </div>
                  <p className="next-action-reason">
                    {todayData.next_action.reason}
                  </p>
                  <div className="next-action-actions">
                    <button
                      type="button"
                      className="btn primary"
                      onClick={() => {
                        const t = allTasks.find(
                          (x) => x.id === todayData.next_action?.task_id,
                        );
                        if (t) handleCompleteTask(t);
                      }}
                    >
                      ✓ 完了にする
                    </button>
                    <button
                      type="button"
                      className="btn ghost"
                      onClick={() =>
                        handleBreakdownTask(todayData.next_action!.task_id)
                      }
                    >
                      ✦ 3ステップに分解
                    </button>
                    <button
                      type="button"
                      className="btn quiet"
                      onClick={() => {
                        const t = allTasks.find(
                          (x) => x.id === todayData.next_action?.task_id,
                        );
                        if (t) setSelectedTask(t);
                      }}
                    >
                      詳細を開く
                    </button>
                  </div>
                </div>
              )}

              {/* 頭の中をまるごと書き出す（Brain Dump AI一括分解） */}
              <div className="card brain-dump-card">
                <div className="brain-dump-head">
                  <span>✦ 頭の中をまるごと書き出す（AIが自動でタスク分解）</span>
                </div>
                <textarea
                  className="brain-dump-area"
                  placeholder="気になっていること、連絡すべきこと、やりたいことを改行や箇条書きでそのまま書いてください…"
                  value={brainText}
                  onChange={(e) => setBrainText(e.target.value)}
                />
                <div className="brain-dump-foot">
                  <span style={{ fontSize: 12, color: "var(--ink-3)" }}>
                    複数行のメモから日付・優先度つきのタスクへ一括変換します
                  </span>
                  <div style={{ display: "flex", gap: 8 }}>
                    {brainText.trim() && (
                      <button
                        type="button"
                        className="btn ghost"
                        onClick={async () => {
                          await apiFetch("/api/capture", {
                            body: {
                              text: brainText,
                              kind: "task",
                              status: "today",
                            },
                          });
                          setBrainText("");
                          await refreshAllData();
                          showToast("✓ 1件のタスクとして今日に追加しました");
                        }}
                      >
                        そのまま1件で追加
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn primary"
                      disabled={!brainText.trim() || brainSplitting}
                      onClick={handleBrainSplit}
                    >
                      {brainSplitting ? "分解中…" : "✦ タスクに分解して確認"}
                    </button>
                  </div>
                </div>

                {brainDrafts && (
                  <div className="split-draft-list">
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        marginBottom: 4,
                      }}
                    >
                      <strong style={{ fontSize: 13 }}>
                        分解プレビュー（{brainDrafts.length}件 ·{" "}
                        {brainAiUsed ? "AI解析" : "ルール解析"}）
                      </strong>
                      <button
                        type="button"
                        className="btn quiet"
                        onClick={() => setBrainDrafts(null)}
                      >
                        閉じる
                      </button>
                    </div>
                    {brainDrafts.map((d, idx) => (
                      <div key={idx} className="split-draft-item">
                        <input
                          type="text"
                          value={d.title}
                          onChange={(e) => {
                            const next = [...brainDrafts];
                            next[idx] = { ...d, title: e.target.value };
                            setBrainDrafts(next);
                          }}
                        />
                        <input
                          type="date"
                          value={d.due_date ?? ""}
                          onChange={(e) => {
                            const next = [...brainDrafts];
                            next[idx] = {
                              ...d,
                              due_date: e.target.value || null,
                            };
                            setBrainDrafts(next);
                          }}
                          style={{
                            border: "1px solid var(--line)",
                            borderRadius: 6,
                            padding: "2px 6px",
                            fontSize: 12,
                            background: "var(--surface)",
                          }}
                        />
                        <button
                          type="button"
                          className="btn quiet"
                          onClick={() =>
                            setBrainDrafts(
                              brainDrafts.filter((_, i) => i !== idx),
                            )
                          }
                        >
                          削除
                        </button>
                      </div>
                    ))}
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "flex-end",
                        gap: 8,
                        marginTop: 8,
                      }}
                    >
                      <button
                        type="button"
                        className="btn primary"
                        onClick={handleBrainCommit}
                      >
                        ✓ この {brainDrafts.length} 件を登録する
                      </button>
                    </div>
                  </div>
                )}
              </div>

              {/* 期限切れ */}
              {(todayData?.overdue.length ?? 0) > 0 && (
                <div className="section overdue">
                  <div className="section-head">
                    <h2 className="section-title">期限切れ</h2>
                    <span className="section-count num">
                      {todayData?.overdue.length}
                    </span>
                  </div>
                  <div className="task-list">
                    {todayData?.overdue.map((t) => renderTaskRow(t, true))}
                  </div>
                </div>
              )}

              {/* 今日やる */}
              <div className="section">
                <div className="section-head">
                  <h2 className="section-title">今日やる</h2>
                  <span className="section-count num">
                    {todayData?.due_today.length ?? 0}
                  </span>
                  <span className="spacer" />
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => setCaptureOpen(true)}
                  >
                    ＋ 今日に追加
                  </button>
                </div>
                <div className="task-list">
                  {(todayData?.due_today.length ?? 0) === 0 ? (
                    <div className="empty-box">
                      今日やるタスクはまだありません。「＋ すぐ書く」から追加できます。
                    </div>
                  ) : (
                    todayData?.due_today.map((t) => renderTaskRow(t, false))
                  )}
                  {(todayData?.completed_today.length ?? 0) > 0 && (
                    <>
                      <button
                        type="button"
                        className="collapse-row"
                        onClick={() => setCompletedOpen((v) => !v)}
                      >
                        <span>{completedOpen ? "▾" : "▸"}</span>
                        <span>
                          今日完了したタスク（{todayData?.completed_today.length}
                          ）
                        </span>
                      </button>
                      {completedOpen && (
                        <div>
                          {todayData?.completed_today.map((t) =>
                            renderTaskRow(t, false),
                          )}
                        </div>
                      )}
                    </>
                  )}
                </div>
              </div>

              {/* 今週・あとで */}
              {upcomingWeekTasks.length > 0 && (
                <div className="section">
                  <div className="section-head">
                    <h2 className="section-title">今週・あとで</h2>
                    <span className="section-count num">
                      {upcomingWeekTasks.length}
                    </span>
                  </div>
                  <div className="task-list">
                    {upcomingWeekTasks
                      .slice(0, 8)
                      .map((t) => renderTaskRow(t, false))}
                  </div>
                </div>
              )}
            </section>
          )}

          {/* ══════════ 2. 受信箱 ══════════ */}
          {screen === "inbox" && (
            <section className="screen">
              <header className="page-head">
                <div className="eyebrow">INBOX</div>
                <h1 className="page-title">受信箱</h1>
                <p className="page-lead">
                  AIが見つけたToDo候補と、まだ仕分けていないメモを確認します。承認した項目だけがタスクになります。
                </p>
              </header>

              <div className="toolbar">
                <div className="seg" role="tablist">
                  <button
                    type="button"
                    className={inboxTab === "candidates" ? "on" : ""}
                    onClick={() => setInboxTab("candidates")}
                  >
                    ✦ AIが見つけた候補
                    <span className="n num">
                      {inboxData?.pending_candidates.length ?? 0}
                    </span>
                  </button>
                  <button
                    type="button"
                    className={inboxTab === "unsorted" ? "on" : ""}
                    onClick={() => setInboxTab("unsorted")}
                  >
                    未仕分けのメモ
                    <span className="n num">
                      {inboxData?.unsorted_items.length ?? 0}
                    </span>
                  </button>
                </div>
              </div>

              {inboxTab === "candidates" ? (
                <div>
                  {(inboxData?.pending_candidates.length ?? 0) === 0 ? (
                    <div className="card empty-box">
                      承認待ちのToDo候補はありません。すべて整理されています。
                    </div>
                  ) : (
                    inboxData?.pending_candidates.map((c) => (
                      <article key={c.id} className="card cand">
                        <div className="cand-src">
                          <span className="chip ai">✦ {c.source_type}</span>
                          {c.due_date && (
                            <span className="chip accent num">
                              期限候補: {c.due_date} {c.due_time ?? ""}
                            </span>
                          )}
                        </div>
                        {editingCandidateId === c.id ? (
                          <div
                            style={{
                              margin: "10px 0",
                              display: "flex",
                              gap: 8,
                              flexWrap: "wrap",
                            }}
                          >
                            <input
                              type="text"
                              className="input-field"
                              value={editingCandidateTitle}
                              onChange={(e) =>
                                setEditingCandidateTitle(e.target.value)
                              }
                              style={{ flex: 1, minWidth: 200 }}
                            />
                            <input
                              type="date"
                              className="input-field"
                              value={editingCandidateDue}
                              onChange={(e) =>
                                setEditingCandidateDue(e.target.value)
                              }
                              style={{ width: 160 }}
                            />
                          </div>
                        ) : (
                          <h3 className="cand-title">{c.title}</h3>
                        )}
                        {c.excerpt && (
                          <blockquote className="quote">{c.excerpt}</blockquote>
                        )}
                        <div className="cand-foot">
                          {editingCandidateId === c.id ? (
                            <>
                              <button
                                type="button"
                                className="btn primary"
                                onClick={() =>
                                  handleApproveCandidate(c, {
                                    title: editingCandidateTitle,
                                    due_date: editingCandidateDue || null,
                                  })
                                }
                              >
                                ✓ この内容で追加
                              </button>
                              <button
                                type="button"
                                className="btn ghost"
                                onClick={() => setEditingCandidateId(null)}
                              >
                                キャンセル
                              </button>
                            </>
                          ) : (
                            <>
                              <button
                                type="button"
                                className="btn primary"
                                onClick={() => handleApproveCandidate(c)}
                              >
                                ✓ タスクに追加
                              </button>
                              <button
                                type="button"
                                className="btn ghost"
                                onClick={() => {
                                  setEditingCandidateId(c.id);
                                  setEditingCandidateTitle(c.title);
                                  setEditingCandidateDue(c.due_date ?? "");
                                }}
                              >
                                編集して追加
                              </button>
                            </>
                          )}
                          <span className="spacer" />
                          <button
                            type="button"
                            className="btn quiet"
                            onClick={() => handleRejectCandidate(c)}
                          >
                            見送る
                          </button>
                        </div>
                      </article>
                    ))
                  )}
                </div>
              ) : (
                <div className="task-list">
                  {(inboxData?.unsorted_items.length ?? 0) === 0 ? (
                    <div className="empty-box">
                      未仕分けのメモはありません。
                    </div>
                  ) : (
                    inboxData?.unsorted_items.map((u) => (
                      <div
                        key={u.id}
                        className="task"
                        style={{ cursor: "default" }}
                      >
                        <span className="memo-ico">✎</span>
                        <div className="task-body">
                          <div className="task-title">{u.title}</div>
                          <div className="task-meta">
                            <span className="m">未仕分け</span>
                          </div>
                        </div>
                        <div
                          style={{
                            display: "flex",
                            gap: 6,
                            alignSelf: "center",
                            flexWrap: "wrap",
                          }}
                        >
                          <button
                            type="button"
                            className="btn primary"
                            style={{ height: 30, padding: "0 12px" }}
                            onClick={() => handleSortUnsorted(u, "task")}
                          >
                            タスクにする
                          </button>
                          <button
                            type="button"
                            className="btn ghost"
                            style={{ height: 30, padding: "0 12px" }}
                            onClick={() => handleSortUnsorted(u, "memo")}
                          >
                            メモに残す
                          </button>
                        </div>
                      </div>
                    ))
                  )}
                </div>
              )}
            </section>
          )}

          {/* ══════════ 3. すべて（一覧・ゴール・カレンダー） ══════════ */}
          {screen === "all" && (
            <section className="screen">
              <header className="page-head">
                <div className="eyebrow">ALL NOTES, TASKS & GOALS</div>
                <h1 className="page-title">すべて</h1>
                <p className="page-lead">
                  タスク・メモ・長期ゴール・期限カレンダーをひとつの場所で俯瞰できます。
                </p>
              </header>

              <div className="toolbar">
                <div className="seg">
                  <button
                    type="button"
                    className={allViewMode === "list" ? "on" : ""}
                    onClick={() => setAllViewMode("list")}
                  >
                    ☰ リスト
                  </button>
                  <button
                    type="button"
                    className={allViewMode === "goals" ? "on" : ""}
                    onClick={() => setAllViewMode("goals")}
                  >
                    🎯 ゴール ({goals.length})
                  </button>
                  <button
                    type="button"
                    className={allViewMode === "calendar" ? "on" : ""}
                    onClick={() => setAllViewMode("calendar")}
                  >
                    📅 カレンダー
                  </button>
                </div>
                <span className="spacer" />
                {allViewMode === "list" && (
                  <label className="search">
                    <span>🔍</span>
                    <input
                      type="search"
                      placeholder="タイトル・メモを検索…"
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.target.value)}
                    />
                  </label>
                )}
              </div>

              {allViewMode === "list" && (
                <>
                  <div className="filters">
                    {(
                      [
                        ["all", "すべて"],
                        ["task", "未完了タスク"],
                        ["memo", "メモ"],
                        ["recurring", "↻ 繰り返し"],
                        ["done", "✓ 完了済み"],
                      ] as const
                    ).map(([key, label]) => (
                      <button
                        key={key}
                        type="button"
                        className={`fchip ${allFilter === key ? "on" : ""}`}
                        onClick={() => setAllFilter(key)}
                      >
                        {label}
                      </button>
                    ))}
                  </div>

                  <div className="task-list">
                    {filteredItems.length === 0 ? (
                      <div className="empty-box">
                        該当する項目がありません。
                      </div>
                    ) : (
                      filteredItems.map((entry) =>
                        entry.type === "task" ? (
                          renderTaskRow(entry.item, false)
                        ) : (
                          <div
                            key={`memo-${entry.item.id}`}
                            className={`task ${
                              selectedMemo?.id === entry.item.id
                                ? "selected"
                                : ""
                            }`}
                            onClick={() => {
                              setSelectedTask(null);
                              setSelectedMemo(entry.item);
                            }}
                          >
                            <span className="memo-ico">📄</span>
                            <div className="task-body">
                              <div className="task-title">
                                {entry.item.title}
                              </div>
                              <div className="task-meta">
                                <span className="chip">メモ</span>
                                {entry.item.body && (
                                  <span className="m">
                                    {entry.item.body.slice(0, 60)}
                                  </span>
                                )}
                              </div>
                            </div>
                          </div>
                        ),
                      )
                    )}
                  </div>
                </>
              )}

              {allViewMode === "goals" && (
                <GoalsView
                  goals={goals}
                  tasks={allTasks}
                  refresh={refreshAllData}
                  showToast={showToast}
                  onOpenTask={(task) => {
                    setSelectedMemo(null);
                    setSelectedTask(task);
                  }}
                  onCompleteTask={handleCompleteTask}
                />
              )}

              {allViewMode === "calendar" && (
                <div className="task-list">
                  {allTasks.filter((t) => t.due_date && t.status !== "done")
                    .length === 0 ? (
                    <div className="empty-box">
                      期限付きの未完了タスクはありません。
                    </div>
                  ) : (
                    allTasks
                      .filter((t) => t.due_date && t.status !== "done")
                      .sort((a, b) =>
                        (a.due_date || "").localeCompare(b.due_date || ""),
                      )
                      .map((t) => renderTaskRow(t, false))
                  )}
                </div>
              )}
            </section>
          )}

          {/* ══════════ 4. ひらめき（マインドマップ） ══════════ */}
          {screen === "ideas" && (
            <section className="screen">
              <header className="page-head">
                <div className="eyebrow">MINDMAP & IDEAS</div>
                <h1 className="page-title">ひらめき</h1>
                <p className="page-lead">
                  テーマから枝を広げて思考を深め、実行したいノードを1タップでタスク化できます。
                </p>
              </header>

              <form
                className="card"
                style={{
                  padding: 16,
                  marginBottom: 16,
                  display: "flex",
                  gap: 8,
                  flexWrap: "wrap",
                }}
                onSubmit={async (e) => {
                  e.preventDefault();
                  if (!newSessionTitle.trim()) return;
                  await apiFetch("/api/ideas/sessions", {
                    body: { title: newSessionTitle },
                  });
                  setNewSessionTitle("");
                  await loadIdeas();
                  showToast("✓ 新しい思考マップを作成しました");
                }}
              >
                <input
                  type="text"
                  className="input-field"
                  placeholder="新しいテーマ（例: 新機能のアイデア出し）"
                  value={newSessionTitle}
                  onChange={(e) => setNewSessionTitle(e.target.value)}
                  style={{ flex: 1, minWidth: 200 }}
                />
                <button
                  type="submit"
                  className="btn primary"
                  style={{ height: 42 }}
                >
                  ＋ テーマ作成
                </button>
              </form>

              {ideasData && ideasData.sessions.length > 0 && (
                <>
                  <div className="filters">
                    {ideasData.sessions.map((s) => (
                      <button
                        key={s.id}
                        type="button"
                        className={`fchip ${
                          ideasData.active_session_id === s.id ? "on" : ""
                        }`}
                        onClick={() => loadIdeas(s.id)}
                      >
                        {s.title}
                      </button>
                    ))}
                  </div>

                  <div className="card" style={{ padding: 20 }}>
                    {ideasData.tree.map((root) => {
                      const renderBranch = (
                        node: {
                          id: number;
                          title: string;
                          kind: string;
                          linked_item_id: number | null;
                          children?: unknown[];
                        },
                        depth = 0,
                      ): React.ReactNode => (
                        <div
                          key={node.id}
                          style={{
                            marginLeft: depth * 18,
                            padding: "8px 0",
                            borderLeft:
                              depth > 0 ? "2px solid var(--line)" : "none",
                            paddingLeft: depth > 0 ? 12 : 0,
                          }}
                        >
                          <div
                            style={{
                              display: "flex",
                              alignItems: "center",
                              gap: 8,
                              flexWrap: "wrap",
                            }}
                          >
                            <strong
                              style={{ fontSize: depth === 0 ? 17 : 15 }}
                            >
                              {node.title}
                            </strong>
                            {node.linked_item_id ? (
                              <span className="chip success">✓ タスク化済</span>
                            ) : (
                              depth > 0 && (
                                <button
                                  type="button"
                                  className="btn ghost"
                                  style={{
                                    height: 26,
                                    padding: "0 10px",
                                    fontSize: 12,
                                  }}
                                  onClick={async () => {
                                    await apiFetch(
                                      `/api/ideas/nodes/${node.id}/to-task`,
                                      { body: {} },
                                    );
                                    await loadIdeas(
                                      ideasData.active_session_id ?? undefined,
                                    );
                                    await refreshAllData();
                                    showToast(
                                      `✓「${node.title}」を今日のタスクに追加しました`,
                                    );
                                  }}
                                >
                                  ＋ タスク化
                                </button>
                              )
                            )}
                            <button
                              type="button"
                              className="btn quiet"
                              style={{ height: 26, fontSize: 12 }}
                              onClick={() => setNewNodeParentId(node.id)}
                            >
                              ＋ 枝を追加
                            </button>
                          </div>
                          {newNodeParentId === node.id && (
                            <form
                              style={{
                                display: "flex",
                                gap: 8,
                                marginTop: 8,
                                flexWrap: "wrap",
                              }}
                              onSubmit={async (e) => {
                                e.preventDefault();
                                if (
                                  !newNodeTitle.trim() ||
                                  !ideasData.active_session_id
                                )
                                  return;
                                await apiFetch(
                                  `/api/ideas/sessions/${ideasData.active_session_id}/nodes`,
                                  {
                                    body: {
                                      parent_id: node.id,
                                      title: newNodeTitle,
                                      kind: "idea",
                                    },
                                  },
                                );
                                setNewNodeTitle("");
                                setNewNodeParentId(null);
                                await loadIdeas(ideasData.active_session_id);
                              }}
                            >
                              <input
                                type="text"
                                className="input-field"
                                placeholder="子アイデアを入力…"
                                value={newNodeTitle}
                                onChange={(e) =>
                                  setNewNodeTitle(e.target.value)
                                }
                                style={{ height: 34, flex: 1, minWidth: 160 }}
                                autoFocus
                              />
                              <button
                                type="submit"
                                className="btn primary"
                                style={{ height: 34 }}
                              >
                                追加
                              </button>
                            </form>
                          )}
                          {Array.isArray(node.children) &&
                            node.children.map((child) =>
                              renderBranch(
                                child as {
                                  id: number;
                                  title: string;
                                  kind: string;
                                  linked_item_id: number | null;
                                  children?: unknown[];
                                },
                                depth + 1,
                              ),
                            )}
                        </div>
                      );
                      return renderBranch(root, 0);
                    })}
                  </div>
                </>
              )}
            </section>
          )}

          {/* ══════════ 5. 設定 ══════════ */}
          {screen === "settings" && (
            <section className="screen">
              <header className="page-head">
                <div className="eyebrow">SETTINGS</div>
                <h1 className="page-title">設定</h1>
                <p className="page-lead">
                  表示テーマ、AI利用枠、データ書き出し、招待メンバー管理を行えます。
                </p>
              </header>

              <div className="set-group">
                <h2>表示とアカウント</h2>
                <div className="card">
                  <div className="set-row">
                    <div className="tx">
                      <b>ログイン中のアカウント</b>
                      <span>
                        {auth.user?.name} ({auth.user?.email}){" "}
                        {auth.user?.is_owner ? "· オーナー" : "· 招待メンバー"}
                      </span>
                    </div>
                    <button
                      type="button"
                      className="btn ghost"
                      disabled={nativeLogoutPending}
                      onClick={async () => {
                        if (isNativeApp()) {
                          setNativeLogoutPending(true);
                          try { await nativeSignOut(); }
                          catch { showToast("ログアウトを完了できませんでした。もう一度お試しください。"); }
                          finally { setNativeLogoutPending(false); }
                          return;
                        }
                        await apiFetch("/api/auth/logout", { body: {} });
                        const nextAuth = await apiFetch<AuthState>(
                          "/api/auth/me",
                        );
                        setAuth(nextAuth);
                      }}
                    >
                      ログアウト
                    </button>
                  </div>
                  <div className="set-row">
                    <div className="tx">
                      <b>外観テーマ</b>
                      <span>紙のノート（ライト）と夜間の墨色（ダーク）</span>
                    </div>
                    <div className="mini-seg">
                      <button
                        type="button"
                        className={theme === "light" ? "on" : ""}
                        onClick={() => setTheme("light")}
                      >
                        ライト
                      </button>
                      <button
                        type="button"
                        className={theme === "dark" ? "on" : ""}
                        onClick={() => setTheme("dark")}
                      >
                        ダーク
                      </button>
                    </div>
                  </div>
                  {settingsData && (
                    <div className="set-row">
                      <div className="tx">
                        <b>本日のAIアシスタント利用回数</b>
                        <span>
                          本日 {settingsData.ai_usage.today_used} /{" "}
                          {settingsData.ai_usage.daily_limit} 回使用（残り{" "}
                          {settingsData.ai_usage.remaining} 回）
                        </span>
                      </div>
                      <span className="chip ai num">
                        残り {settingsData.ai_usage.remaining} 回
                      </span>
                    </div>
                  )}
                </div>
              </div>

              {/* オーナー専用：招待メンバー管理 */}
              {auth.user?.is_owner && (
                <div className="set-group">
                  <h2>招待メンバー管理（オーナー専用）</h2>
                  <div className="card" style={{ padding: 20 }}>
                    <p
                      style={{
                        margin: "0 0 12px",
                        fontSize: 13,
                        color: "var(--ink-2)",
                      }}
                    >
                      招待されたGoogleメールアドレスのみがログインできます。メンバーごとに専用のSQLiteデータベースが自動作成され、あなたのタスクやメモとは完全に分離されます。
                    </p>
                    <form
                      style={{
                        display: "flex",
                        gap: 8,
                        flexWrap: "wrap",
                        marginBottom: 16,
                      }}
                      onSubmit={async (e) => {
                        e.preventDefault();
                        if (!inviteEmail.trim()) return;
                        await apiFetch("/api/settings/invites", {
                          body: { email: inviteEmail, note: inviteNote },
                        });
                        setInviteEmail("");
                        setInviteNote("");
                        await loadSettings();
                        showToast(`✓ ${inviteEmail} を招待リストに追加しました`);
                      }}
                    >
                      <input
                        type="email"
                        className="input-field"
                        placeholder="招待するGoogleメールアドレス (example@gmail.com)"
                        value={inviteEmail}
                        onChange={(e) => setInviteEmail(e.target.value)}
                        style={{ flex: 2, minWidth: 220 }}
                      />
                      <input
                        type="text"
                        className="input-field"
                        placeholder="メモ（友人名など）"
                        value={inviteNote}
                        onChange={(e) => setInviteNote(e.target.value)}
                        style={{ flex: 1, minWidth: 140 }}
                      />
                      <button
                        type="submit"
                        className="btn primary"
                        style={{ height: 42 }}
                      >
                        ＋ 招待する
                      </button>
                    </form>

                    {settingsData?.invites && settingsData.invites.length > 0 && (
                      <div>
                        {settingsData.invites.map((inv) => (
                          <div
                            key={inv.email}
                            className="set-row"
                            style={{ padding: "10px 4px" }}
                          >
                            <div className="tx">
                              <b>
                                {inv.email} {inv.is_owner && "（オーナー）"}
                              </b>
                              <span>
                                {inv.note || "メモなし"} ·{" "}
                                {inv.accepted_at ? "参加済み" : "未ログイン"}
                              </span>
                            </div>
                            {!inv.is_owner && (
                              <button
                                type="button"
                                className="btn quiet"
                                onClick={async () => {
                                  await apiFetch(
                                    `/api/settings/invites/${encodeURIComponent(
                                      inv.email,
                                    )}`,
                                    { method: "DELETE" },
                                  );
                                  await loadSettings();
                                  showToast(
                                    `${inv.email} の招待を取り消しました`,
                                  );
                                }}
                              >
                                招待取消
                              </button>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              )}

              <div className="set-group">
                <h2>データの保護とバックアップ</h2>
                <div className="card">
                  <div className="set-row">
                    <div className="tx">
                      <b>JSON形式でバックアップを書き出す</b>
                      <span>
                        {isNativeApp() ? "あなたのタスク・メモ・ゴールをJSON形式で共有します" : "あなたのタスク・メモ・ゴールを1つのJSONファイルとして保存します"}
                      </span>
                    </div>
                    {isNativeApp() ? <button className="btn ghost" onClick={async () => {
                      try {
                        const backup = await apiFetch<unknown>("/api/export");
                        await Share.share({ title: "SymNoteのバックアップ", text: JSON.stringify(backup, null, 2) });
                      } catch { showToast("バックアップの共有を完了できませんでした。"); }
                    }}>共有する</button> : <a
                      href="/api/export"
                      download="symnote-backup.json"
                      className="btn ghost"
                      style={{ textDecoration: "none" }}
                    >
                      書き出す
                    </a>}
                  </div>
                </div>
              </div>
            </section>
          )}
        </div>
      </main>

      {/* スマホ用ボトムシート背景 */}
      <div
        className="backdrop"
        onClick={() => {
          setSelectedTask(null);
          setSelectedMemo(null);
        }}
      />

      {/* ───────── 右ドロワー（スマホでは下から出るボトムシート）：タスク / メモ詳細 ───────── */}
      <aside className="detail" aria-label="項目の詳細">
        {selectedTask && (
          <div className="detail-inner">
            <span className="sheet-handle" aria-hidden="true" />
            <div className="detail-top">
              <span>タスク #{selectedTask.id}</span>
              <span className="spacer" />
              <button
                type="button"
                className="btn quiet"
                onClick={() => setSelectedTask(null)}
              >
                ✕ 閉じる
              </button>
            </div>

            <input
              type="text"
              className="detail-title-input"
              value={selectedTask.title}
              onChange={(e) =>
                setSelectedTask({ ...selectedTask, title: e.target.value })
              }
              onBlur={() =>
                handleTaskFieldPatch(selectedTask, { title: selectedTask.title })
              }
            />

            <textarea
              className="detail-notes-area"
              placeholder="補足メモ・手順・リンクを自由に書く…"
              value={selectedTask.details}
              onChange={(e) =>
                setSelectedTask({ ...selectedTask, details: e.target.value })
              }
              onBlur={() =>
                handleTaskFieldPatch(selectedTask, {
                  details: selectedTask.details,
                })
              }
            />

            <dl className="props">
              <dt>状態</dt>
              <dd>
                <div className="mini-seg">
                  {(
                    [
                      ["today", "今日"],
                      ["week", "今週"],
                      ["inbox", "あとで"],
                      ["done", "完了"],
                    ] as const
                  ).map(([st, label]) => (
                    <button
                      key={st}
                      type="button"
                      className={selectedTask.status === st ? "on" : ""}
                      onClick={() =>
                        handleTaskFieldPatch(selectedTask, { status: st })
                      }
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </dd>

              <dt>期限日</dt>
              <dd>
                <input
                  type="date"
                  className="input-field"
                  style={{ height: 32, width: 165 }}
                  value={selectedTask.due_date ?? ""}
                  onChange={(e) =>
                    handleTaskFieldPatch(selectedTask, {
                      due_date: e.target.value || null,
                    })
                  }
                />
                <input
                  type="time"
                  className="input-field"
                  style={{ height: 32, width: 120 }}
                  value={selectedTask.due_time ?? ""}
                  onChange={(e) =>
                    handleTaskFieldPatch(selectedTask, {
                      due_time: e.target.value || null,
                    })
                  }
                />
              </dd>

              <dt>見積時間</dt>
              <dd>
                <div className="mini-seg">
                  {[15, 25, 45, 60].map((m) => (
                    <button
                      key={m}
                      type="button"
                      className={
                        selectedTask.estimate_minutes === m ? "on" : ""
                      }
                      onClick={() =>
                        handleTaskFieldPatch(selectedTask, {
                          estimate_minutes: m,
                        })
                      }
                    >
                      {m}分
                    </button>
                  ))}
                </div>
              </dd>

              <dt>重要度</dt>
              <dd>
                <div className="mini-seg">
                  {[
                    [2, "低"],
                    [3, "標準"],
                    [4, "高"],
                    [5, "最優先"],
                  ].map(([imp, label]) => (
                    <button
                      key={imp}
                      type="button"
                      className={selectedTask.importance === imp ? "on" : ""}
                      onClick={() =>
                        handleTaskFieldPatch(selectedTask, {
                          importance: Number(imp),
                        })
                      }
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </dd>

              <dt>ゴール</dt>
              <dd>
                <select
                  className="input-field"
                  style={{ height: 32 }}
                  value={selectedTask.goal_id ?? ""}
                  onChange={(e) =>
                    handleTaskFieldPatch(selectedTask, {
                      goal_id: e.target.value ? Number(e.target.value) : null,
                    })
                  }
                >
                  <option value="">（紐付けなし）</option>
                  {goals.map((g) => (
                    <option key={g.id} value={g.id}>
                      {g.title}
                    </option>
                  ))}
                </select>
              </dd>
            </dl>

            <div
              style={{
                marginTop: 24,
                paddingTop: 16,
                borderTop: "1px solid var(--line)",
                display: "flex",
                justifyContent: "space-between",
                gap: 8,
                flexWrap: "wrap",
              }}
            >
              <button
                type="button"
                className="btn ghost"
                onClick={() => handleBreakdownTask(selectedTask.id)}
              >
                ✦ 3ステップに分解
              </button>
              <button
                type="button"
                className="btn quiet"
                onClick={() => handleDeleteTask(selectedTask)}
              >
                削除する
              </button>
            </div>
          </div>
        )}

        {selectedMemo && (
          <div className="detail-inner">
            <span className="sheet-handle" aria-hidden="true" />
            <div className="detail-top">
              <span>メモ #{selectedMemo.id}</span>
              <span className="spacer" />
              <button
                type="button"
                className="btn quiet"
                onClick={() => setSelectedMemo(null)}
              >
                ✕ 閉じる
              </button>
            </div>
            <textarea
              className="detail-notes-area"
              style={{ minHeight: 240 }}
              value={selectedMemo.raw_text}
              onChange={(e) =>
                setSelectedMemo({ ...selectedMemo, raw_text: e.target.value })
              }
              onBlur={() =>
                handleMemoPatch(selectedMemo, selectedMemo.raw_text)
              }
            />
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                marginTop: 12,
              }}
            >
              <button
                type="button"
                className="btn primary"
                onClick={() => handleMemoToTask(selectedMemo)}
              >
                ✓ このメモをタスクに変換
              </button>
              <button
                type="button"
                className="btn quiet"
                onClick={async () => {
                  await apiFetch(`/api/memos/${selectedMemo.id}`, {
                    method: "DELETE",
                  });
                  setSelectedMemo(null);
                  await refreshAllData();
                  showToast("メモを削除しました");
                }}
              >
                削除する
              </button>
            </div>
          </div>
        )}
      </aside>

      {/* ───────── スマホ用下部タブバー（5タブ + Safe Area 対応） ───────── */}
      <nav className="tabbar" aria-label="モバイルナビゲーション">
        <button
          type="button"
          className={`tab-item ${screen === "today" ? "active" : ""}`}
          onClick={() => setScreen("today")}
        >
          <span>☀</span>
          <span>今日</span>
        </button>
        <button
          type="button"
          className={`tab-item ${screen === "inbox" ? "active" : ""}`}
          onClick={() => setScreen("inbox")}
        >
          <span>📥</span>
          <span>受信箱</span>
          {inboxCount > 0 && <span className="badge num">{inboxCount}</span>}
        </button>
        <button
          type="button"
          aria-label="すぐ書く"
          onClick={() => setCaptureOpen(true)}
        >
          <span className="fab">＋</span>
        </button>
        <button
          type="button"
          className={`tab-item ${screen === "all" ? "active" : ""}`}
          onClick={() => setScreen("all")}
        >
          <span>☰</span>
          <span>すべて</span>
        </button>
        <button
          type="button"
          className={`tab-item ${screen === "ideas" ? "active" : ""}`}
          onClick={() => setScreen("ideas")}
        >
          <span>✦</span>
          <span>ひらめき</span>
        </button>
      </nav>

      {/* ───────── クイック入力モーダル (⌘K / スマホ下部シート) ───────── */}
      <div
        className={`overlay ${captureOpen ? "open" : ""}`}
        onClick={() => setCaptureOpen(false)}
      >
        <div className="capture" onClick={(e) => e.stopPropagation()}>
          <span className="sheet-handle" aria-hidden="true" />
          <textarea
            placeholder="思いついたことを書く…（例：明日 15時 歯医者予約 / 毎週月曜 9:00 ゴミ出し）"
            value={captureText}
            onChange={(e) => setCaptureText(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault();
                handleQuickCaptureSubmit();
              }
            }}
            autoFocus={captureOpen}
          />
          {capturePreview && capturePreview.chips.length > 0 && (
            <div className="parsed">
              {capturePreview.chips.map((ch, idx) => (
                <span key={idx} className="chip accent">
                  {ch.label}
                </span>
              ))}
            </div>
          )}
          <div className="capture-foot">
            <div className="mini-seg">
              {(
                [
                  ["auto", "自動判定"],
                  ["task", "タスク"],
                  ["memo", "メモ"],
                ] as const
              ).map(([k, label]) => (
                <button
                  key={k}
                  type="button"
                  className={captureKind === k ? "on" : ""}
                  onClick={() => setCaptureKind(k)}
                >
                  {label}
                </button>
              ))}
            </div>
            <span className="spacer" />
            <button
              type="button"
              className="btn ghost"
              onClick={() => setCaptureOpen(false)}
            >
              キャンセル
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={!captureText.trim() || captureSaving}
              onClick={handleQuickCaptureSubmit}
            >
              保存する ⌘↵
            </button>
          </div>
        </div>
      </div>

      {/* ───────── トースト（Undo対応） ───────── */}
      <div className={`toast ${toast ? "show" : ""}`} role="status">
        <span>{toast?.message}</span>
        {toast?.onUndo && (
          <button
            type="button"
            onClick={async () => {
              const fn = toast.onUndo;
              setToast(null);
              if (fn) await fn();
            }}
          >
            {toast.undoLabel ?? "元に戻す"}
          </button>
        )}
      </div>
    </div>
  );
}
