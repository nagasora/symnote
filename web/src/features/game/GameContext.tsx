import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { apiFetch } from "../../api/client";
import type {
  CompletionReward,
  Player,
  TaskItem,
  TodayPayload,
} from "../../api/types";
import { tapFeedback } from "../../lib/haptics";
import { playClearSound } from "../../lib/sound";

interface CelebrationState {
  task: TaskItem;
  reward: CompletionReward;
  daily: { xp: number; gold: number } | null;
}
interface ToastState {
  message: string;
  actionLabel?: string;
  action?: () => void;
}
export interface BreakdownDraft {
  title: string;
  estimate_minutes: number;
  selected: boolean;
}
interface BreakdownState {
  task: TaskItem;
  drafts: BreakdownDraft[];
}
interface GameContextValue {
  today: TodayPayload | null;
  tasks: TaskItem[];
  player: Player | null;
  loading: boolean;
  captureOpen: boolean;
  celebration: CelebrationState | null;
  completingId: number | null;
  toast: ToastState | null;
  breakdownPreview: BreakdownState | null;
  setBreakdownPreview: (value: BreakdownState | null) => void;
  refresh: () => Promise<void>;
  complete: (task: TaskItem) => Promise<void>;
  reopen: (task: TaskItem, status?: string) => Promise<void>;
  breakdown: (task: TaskItem) => Promise<void>;
  openQuest: (task: TaskItem) => void;
  setCaptureOpen: (open: boolean) => void;
  setCelebration: (value: CelebrationState | null) => void;
  showToast: (
    message: string,
    actionLabel?: string,
    action?: () => void,
  ) => void;
}

const GameContext = createContext<GameContextValue | null>(null);

export function GameProvider({ children }: { children: ReactNode }) {
  const [today, setToday] = useState<TodayPayload | null>(null);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [player, setPlayer] = useState<Player | null>(null);
  const [loading, setLoading] = useState(true);
  const [captureOpen, setCaptureOpen] = useState(false);
  const [celebration, setCelebration] = useState<CelebrationState | null>(null);
  const [completingId, setCompletingId] = useState<number | null>(null);
  const [toast, setToast] = useState<ToastState | null>(null);
  const [breakdownPreview, setBreakdownPreview] =
    useState<BreakdownState | null>(null);
  const navigate = useNavigate();
  const location = useLocation();

  const showToast = useCallback(
    (message: string, actionLabel?: string, action?: () => void) =>
      setToast({ message, actionLabel, action }),
    [],
  );
  useEffect(() => {
    if (!toast) return;
    const id = window.setTimeout(() => setToast(null), 5200);
    return () => clearTimeout(id);
  }, [toast]);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [todayResult, taskResult, playerResult] = await Promise.allSettled([
        apiFetch<TodayPayload>("/api/today"),
        apiFetch<{ tasks: TaskItem[] }>(
          "/api/tasks?status=all&include_done=true",
        ),
        apiFetch<Player>("/api/player"),
      ]);
      if (todayResult.status === "fulfilled") setToday(todayResult.value);
      if (taskResult.status === "fulfilled") setTasks(taskResult.value.tasks);
      if (playerResult.status === "fulfilled") setPlayer(playerResult.value);
      else if (todayResult.status === "fulfilled" && todayResult.value.player)
        setPlayer(todayResult.value.player);
      if (
        todayResult.status === "rejected" ||
        taskResult.status === "rejected"
      ) {
        showToast(
          "一部を読み込めませんでした。接続を確認して再読み込みしてね。",
        );
      }
    } finally {
      setLoading(false);
    }
  }, [showToast]);
  useEffect(() => {
    void refresh().catch((error) =>
      showToast(
        error instanceof Error ? error.message : "読み込めませんでした",
      ),
    );
  }, [refresh, showToast]);

  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCaptureOpen(true);
      }
      if (event.key === "Escape") {
        setCaptureOpen(false);
        setCelebration(null);
        setBreakdownPreview(null);
        if (new URLSearchParams(location.search).has("quest"))
          navigate(location.pathname);
      }
    };
    addEventListener("keydown", key);
    return () => removeEventListener("keydown", key);
  }, [location.pathname, location.search, navigate]);

  const reopen = useCallback(
    async (task: TaskItem, status = "today") => {
      await apiFetch(`/api/tasks/${task.id}/reopen`, { body: { status } });
      setCelebration(null);
      await refresh();
      showToast(`「${task.title}」を元に戻しました`);
    },
    [refresh, showToast],
  );

  const complete = useCallback(
    async (task: TaskItem) => {
      if (task.status === "done") return reopen(task);
      setCompletingId(task.id);
      tapFeedback();
      try {
        const response = await apiFetch<{
          task: TaskItem;
          reward: CompletionReward | null;
          daily_clear: { xp: number; gold: number } | null;
          player?: Player;
        }>(`/api/tasks/${task.id}/complete`, { body: {} });
        if (response.player) setPlayer(response.player);
        await refresh();
        if (response.reward || response.daily_clear) {
          const earned = response.reward ?? {
            xp: 0,
            gold: 0,
            bonuses: [],
            level_before: response.player?.level ?? 1,
            level_after: response.player?.level ?? 1,
            new_achievements: [],
          };
          playClearSound(
            Boolean(
              response.player?.buddy.sound_enabled ??
              player?.buddy.sound_enabled,
            ),
          );
          const full =
            earned.level_after > earned.level_before ||
            earned.new_achievements.length > 0 ||
            Boolean(response.daily_clear) ||
            Boolean(task.quest?.is_boss);
          if (full)
            setCelebration({
              task,
              reward: earned,
              daily: response.daily_clear,
            });
          else
            showToast(
              `+${earned.xp} XP　クエストクリア`,
              "元に戻す",
              () => void reopen(task, task.status),
            );
        }
      } catch (error) {
        showToast(
          error instanceof Error
            ? error.message
            : "クリアを記録できませんでした",
        );
      } finally {
        window.setTimeout(() => setCompletingId(null), 900);
      }
    },
    [player?.buddy.sound_enabled, refresh, reopen, showToast],
  );

  const breakdown = useCallback(
    async (task: TaskItem) => {
      try {
        const result = await apiFetch<{
          subtasks: Array<{ title: string; estimate_minutes: number }>;
        }>(`/api/tasks/${task.id}/breakdown`, { body: {} });
        setBreakdownPreview({
          task,
          drafts: result.subtasks.map((item) => ({ ...item, selected: true })),
        });
      } catch (error) {
        showToast(
          error instanceof Error
            ? error.message
            : "小さな一歩を考えられませんでした",
        );
      }
    },
    [showToast],
  );

  const openQuest = useCallback(
    (task: TaskItem) => {
      const query = new URLSearchParams(location.search);
      query.set("quest", String(task.id));
      navigate(`${location.pathname}?${query}`);
    },
    [location.pathname, location.search, navigate],
  );

  const value = useMemo(
    () => ({
      today,
      tasks,
      player,
      loading,
      captureOpen,
      celebration,
      completingId,
      toast,
      breakdownPreview,
      setBreakdownPreview,
      refresh,
      complete,
      reopen,
      breakdown,
      openQuest,
      setCaptureOpen,
      setCelebration,
      showToast,
    }),
    [
      today,
      tasks,
      player,
      loading,
      captureOpen,
      celebration,
      completingId,
      toast,
      breakdownPreview,
      refresh,
      complete,
      reopen,
      breakdown,
      openQuest,
      showToast,
    ],
  );
  return <GameContext.Provider value={value}>{children}</GameContext.Provider>;
}

export function useGame() {
  const value = useContext(GameContext);
  if (!value) throw new Error("useGame must be used inside GameProvider");
  return value;
}
