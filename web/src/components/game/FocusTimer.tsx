import { useEffect, useState } from "react";
import { Check, Square, Timer } from "lucide-react";
import { apiFetch } from "../../api/client";
import type { CompletionReward, Player } from "../../api/types";
import { useGame } from "../../features/game/GameContext";
import { Button } from "../ui/Button";

interface Session {
  id: number;
  task_id: number;
  started_at: string;
  planned_minutes: number;
}

export function FocusTimer() {
  const { refresh, showToast, tasks, setCelebration } = useGame();
  const [session, setSession] = useState<Session | null>(null);
  const [now, setNow] = useState(Date.now());
  const load = () =>
    apiFetch<{ session: Session | null }>("/api/focus")
      .then((result) => setSession(result.session))
      .catch(() => setSession(null));
  useEffect(() => {
    const refreshSession = () => void load();
    refreshSession();
    addEventListener("symnote:focus", refreshSession);
    const id = window.setInterval(() => {
      setNow(Date.now());
      void load();
    }, 15000);
    return () => {
      clearInterval(id);
      removeEventListener("symnote:focus", refreshSession);
    };
  }, []);
  if (!session) return null;
  const started = new Date(session.started_at).getTime();
  const remaining = Math.max(
    0,
    session.planned_minutes * 60 - Math.floor((now - started) / 1000),
  );
  const display = `${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")}`;
  async function end(kind: "finish" | "stop") {
    const task = tasks.find((item) => item.id === session!.task_id);
    const response = await apiFetch<{
      reward: CompletionReward | null;
      daily_clear: { xp: number; gold: number } | null;
      player?: Player;
    }>(`/api/focus/${session!.id}/${kind}`, {
      body: kind === "finish" ? { complete_task: true } : {},
    });
    setSession(null);
    await refresh();
    if (
      kind === "finish" &&
      task &&
      (response.reward || response.daily_clear)
    ) {
      const reward = response.reward ?? {
        xp: 0,
        gold: 0,
        bonuses: [],
        level_before: response.player?.level ?? 1,
        level_after: response.player?.level ?? 1,
        new_achievements: [],
      };
      const full =
        reward.level_after > reward.level_before ||
        reward.new_achievements.length > 0 ||
        Boolean(response.daily_clear) ||
        Boolean(task.quest?.is_boss);
      if (full) setCelebration({ task, reward, daily: response.daily_clear });
      else showToast(`集中タイム完了　+${reward.xp} XP`);
    } else
      showToast(
        kind === "finish" ? "集中した時間を記録しました" : "ひと休みにしました",
      );
  }
  return (
    <aside className="focus-timer" aria-live="polite">
      <Timer />
      <div>
        <span>集中タイム</span>
        <strong className="num">{display}</strong>
      </div>
      <Button size="sm" variant="leaf" onClick={() => void end("finish")}>
        <Check size={17} />
        できた
      </Button>
      <Button
        size="sm"
        variant="quiet"
        aria-label="フォーカスを止める"
        onClick={() => void end("stop")}
      >
        <Square size={16} />
      </Button>
    </aside>
  );
}
