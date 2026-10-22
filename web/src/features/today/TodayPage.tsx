import { useState } from "react";
import {
  CalendarDays,
  Clock3,
  Play,
  Scissors,
  Sunrise,
  TimerReset,
} from "lucide-react";
import { apiFetch } from "../../api/client";
import type { TaskItem } from "../../api/types";
import { useGame } from "../game/GameContext";
import { BuddyBubble } from "../../components/buddy/BuddyBubble";
import { ChestProgress } from "../../components/game/ChestProgress";
import { PlayerCard } from "../../components/game/PlayerCard";
import { QuestList } from "../../components/quest/QuestList";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { Chip } from "../../components/ui/Chip";
import { EmptyState } from "../../components/ui/EmptyState";
import { ProgressBar } from "../../components/ui/ProgressBar";

export function TodayPage() {
  const {
    today,
    tasks,
    player,
    loading,
    complete,
    breakdown,
    openQuest,
    completingId,
    refresh,
    showToast,
    setCaptureOpen,
  } = useGame();
  const [completedOpen, setCompletedOpen] = useState(false);
  const [planning, setPlanning] = useState(false);
  const [suggestions, setSuggestions] = useState<TaskItem[]>([]);
  const due = today?.due_today ?? [];
  const overdue = today?.overdue ?? [];
  const completed = today?.completed_today ?? [];
  const next = today?.next_action
    ? tasks.find((task) => task.id === today.next_action?.task_id)
    : (due[0] ?? overdue[0]);
  const capacity = today?.capacity ?? {
    planned_minutes: due.reduce(
      (sum, task) => sum + (task.estimate_minutes ?? 15),
      0,
    ),
    free_minutes: null,
  };
  const daily = today?.daily_clear ?? {
    target: due.length + overdue.length + completed.length,
    done: completed.length,
    claimed: false,
  };

  async function startFocus(task: TaskItem) {
    try {
      await apiFetch("/api/focus", {
        body: {
          task_id: task.id,
          minutes:
            task.estimate_minutes === 50
              ? 50
              : task.estimate_minutes === 15
                ? 15
                : 25,
        },
      });
      window.dispatchEvent(new Event("symnote:focus"));
      showToast(`「${task.title}」に集中する時間を始めました`);
    } catch (error) {
      showToast(
        error instanceof Error
          ? error.message
          : "フォーカスを始められませんでした",
      );
    }
  }

  async function planDay() {
    setPlanning(true);
    try {
      const result = await apiFetch<{
        suggestions?: TaskItem[];
        tasks?: TaskItem[];
      }>("/api/today/plan", { body: {} });
      setSuggestions(result.suggestions ?? result.tasks ?? []);
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : "作戦を考えられませんでした",
      );
    } finally {
      setPlanning(false);
    }
  }

  if (loading && !today)
    return <div className="screen-loading">きょうの冒険を準備しています…</div>;
  return (
    <div className="today-layout">
      <section className="game-screen today-screen">
        <header className="game-page-head">
          <div>
            <span className="eyebrow">
              <Sunrise size={16} />
              {today?.weekday_ja ?? "きょう"}
            </span>
            <h1>きょうの冒険</h1>
          </div>
          <Chip tone="plain" className="num">
            {today?.today}
          </Chip>
        </header>
        <BuddyBubble
          name={player?.buddy.name}
          line={
            today?.buddy_line ??
            (due.length + overdue.length
              ? `きょうは${due.length + overdue.length}つで十分。ひとつずつ行こう。`
              : "きょうの分はひと区切り。よく進んだね。")
          }
          expression={
            completed.length && !due.length && !overdue.length
              ? "happy"
              : "normal"
          }
        />

        {next ? (
          <Card tone="sun" className="next-action-card">
            <div className="card-kicker">つぎの一手</div>
            <h2>{next.title}</h2>
            <div className="quest-chips">
              <Chip>
                {"★".repeat(next.quest?.stars ?? 1)}
                {next.estimate_minutes ? ` ${next.estimate_minutes}分` : ""}
              </Chip>
              {next.goal_title && <Chip tone="grape">{next.goal_title}</Chip>}
              <Chip tone="xp" className="num">
                +{next.quest?.xp ?? 10} XP
              </Chip>
            </div>
            <div className="next-actions">
              <Button variant="sun" onClick={() => void startFocus(next)}>
                <Play size={18} />
                はじめる
              </Button>
              <Button onClick={() => void complete(next)}>
                もうクリアした
              </Button>
              {next.quest?.is_boss && (
                <Button variant="quiet" onClick={() => void breakdown(next)}>
                  <Scissors size={17} />
                  小分けにする
                </Button>
              )}
            </div>
          </Card>
        ) : (
          <Card className="strategy-card">
            <BuddyBubble
              name={player?.buddy.name}
              line="きょうの作戦を一緒に決めよう。3つくらいで十分だよ。"
            />
            <Button
              variant="sun"
              onClick={() => void planDay()}
              disabled={planning}
            >
              {planning ? "考え中…" : "作戦会議をはじめる"}
            </Button>
            {suggestions.length > 0 && (
              <QuestList
                tasks={suggestions}
                onToggle={async (task) => {
                  await apiFetch(`/api/tasks/${task.id}`, {
                    method: "PATCH",
                    body: { status: "today" },
                  });
                  await refresh();
                }}
                onOpen={openQuest}
              />
            )}
          </Card>
        )}

        <Card className="day-progress-card">
          <ChestProgress
            done={daily.done}
            target={daily.target}
            claimed={daily.claimed}
          />
          <div className="capacity">
            <div>
              <span>きょうの体力</span>
              <strong className="num">
                {capacity.planned_minutes}分
                {capacity.free_minutes != null &&
                  ` / ${capacity.free_minutes}分`}
              </strong>
            </div>
            {capacity.free_minutes == null ? (
              <p className="capacity-unverified">空き時間は未確認</p>
            ) : (
              <>
                <ProgressBar
                  label="きょうの予定時間"
                  tone={
                    capacity.planned_minutes > capacity.free_minutes
                      ? "sky"
                      : "leaf"
                  }
                  value={capacity.planned_minutes}
                  max={capacity.free_minutes}
                />
                {capacity.planned_minutes > capacity.free_minutes && (
                  <p>ちょっと多いかも。1つ あしたにする？</p>
                )}
              </>
            )}
          </div>
        </Card>

        {overdue.length > 0 && (
          <section className="quest-section carryover">
            <header>
              <div>
                <h2>持ち越し</h2>
                <span>{overdue.length}つ</span>
              </div>
              <p>きのうまでの予定でした。きょうに置くか、日を変えられます。</p>
            </header>
            <QuestList
              tasks={overdue}
              carryover
              completingId={completingId}
              onToggle={complete}
              onOpen={openQuest}
              onBreakdown={breakdown}
              onMoveToday={async (task) => {
                await apiFetch(`/api/tasks/${task.id}`, {
                  method: "PATCH",
                  body: { status: "today", due_date: today?.today },
                });
                await refresh();
                showToast("きょうの冒険に入れました");
              }}
              onChangeDate={async (task, day) => {
                await apiFetch(`/api/tasks/${task.id}`, {
                  method: "PATCH",
                  body: { status: "week", due_date: day },
                });
                await refresh();
                showToast("予定日を変えました");
              }}
            />
          </section>
        )}
        <section className="quest-section">
          <header>
            <div>
              <h2>きょうのクエスト</h2>
              <span>{due.length}つ</span>
            </div>
            <Button
              variant="quiet"
              size="sm"
              onClick={() => setCaptureOpen(true)}
            >
              追加する
            </Button>
          </header>
          {due.length ? (
            <QuestList
              tasks={due}
              completingId={completingId}
              onToggle={complete}
              onOpen={openQuest}
              onBreakdown={breakdown}
            />
          ) : (
            <EmptyState title="きょうのクエストはありません">
              気になることがあれば、ミナに渡しておこう。
            </EmptyState>
          )}
        </section>
        {completed.length > 0 && (
          <section className="quest-section completed-section">
            <button
              type="button"
              className="section-toggle"
              aria-expanded={completedOpen}
              onClick={() => setCompletedOpen(!completedOpen)}
            >
              クリア済み <span>{completed.length}つ</span>
            </button>
            {completedOpen && (
              <QuestList
                tasks={completed}
                onToggle={complete}
                onOpen={openQuest}
              />
            )}
          </section>
        )}
        {today?.period === "evening" && (
          <Card className="evening-card">
            <div>
              <Clock3 />
              <h2>きょうのふりかえり</h2>
            </div>
            <p>
              きょうは {completed.length}
              つクリア。獲得した経験を持って、あしたも小さく進もう。
            </p>
            <Button onClick={() => location.assign("/me#journal")}>
              冒険日誌を見る
            </Button>
          </Card>
        )}
      </section>
      <aside className="right-rail">
        {player && <PlayerCard player={player} />}
        <Card>
          <h3>
            <CalendarDays size={18} />
            きょうの予定
          </h3>
          {today?.calendar_events?.length ? (
            <ul className="schedule-list">
              {today.calendar_events.slice(0, 5).map((event) => (
                <li key={event.id}>
                  <span className="num">
                    {event.is_all_day
                      ? "終日"
                      : /^\d{1,2}:\d{2}/.test(event.start)
                        ? event.start.slice(0, 5)
                        : new Date(event.start).toLocaleTimeString("ja-JP", {
                            hour: "2-digit",
                            minute: "2-digit",
                          })}
                  </span>
                  <strong>{event.summary}</strong>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted-copy">
              予定はありません。余白も冒険の一部だよ。
            </p>
          )}
        </Card>
        <Card>
          <h3>
            <TimerReset size={18} />
            進めている冒険
          </h3>
          {Array.from(
            new Set(
              tasks
                .filter((task) => task.goal_title && task.status !== "done")
                .map((task) => task.goal_title),
            ),
          )
            .slice(0, 3)
            .map((goal) => (
              <Chip key={goal ?? ""} tone="grape">
                {goal}
              </Chip>
            ))}
        </Card>
      </aside>
    </div>
  );
}
