import { useEffect, useState } from "react";
import {
  Award,
  Check,
  Clock3,
  Coins,
  Crown,
  Flame,
  Footprints as FootprintsIcon,
  Gift,
  LockKeyhole,
  Medal,
  Plus,
  Settings,
  ShieldCheck,
  Sparkles,
  Star,
  Sunrise,
  Trophy,
} from "lucide-react";
import { Link } from "react-router-dom";
import { apiFetch } from "../../api/client";
import type { Achievement, RewardItem } from "../../api/types";
import { useGame } from "../game/GameContext";
import { PlayerCard } from "../../components/game/PlayerCard";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { Chip } from "../../components/ui/Chip";
import { EmptyState } from "../../components/ui/EmptyState";

interface HistoryDay {
  day: string;
  count: number;
  rest_day: boolean;
}
const achievementIcons = {
  first_quest: Star,
  daily_5: ShieldCheck,
  streak_3: Sparkles,
  streak_7: Flame,
  streak_30: Crown,
  total_10: Medal,
  total_50: Trophy,
  total_100: Award,
  early_bird: Sunrise,
  on_time_10: Clock3,
} as const;

export function MePage() {
  const { player, refresh, showToast } = useGame();
  const [history, setHistory] = useState<HistoryDay[]>([]);
  const [achievements, setAchievements] = useState<Achievement[]>([]);
  const [rewards, setRewards] = useState<RewardItem[]>([]);
  const [review, setReview] = useState<Record<string, unknown> | null>(null);
  const [newReward, setNewReward] = useState("");
  const [cost, setCost] = useState(50);
  const load = async () => {
    const [h, a, r, w] = await Promise.all([
      apiFetch<{ days: HistoryDay[] }>("/api/quests/history?days=28"),
      apiFetch<{ achievements: Achievement[] } | Achievement[]>(
        "/api/achievements",
      ),
      apiFetch<{ rewards: RewardItem[] } | RewardItem[]>("/api/rewards"),
      apiFetch<{ review?: Record<string, unknown> } | Record<string, unknown>>(
        "/api/review/weekly",
      ),
    ]);
    setHistory(h.days);
    setAchievements(Array.isArray(a) ? a : a.achievements);
    setRewards(Array.isArray(r) ? r : r.rewards);
    setReview(
      ("review" in w ? (w.review ?? {}) : w) as Record<string, unknown>,
    );
  };
  useEffect(() => {
    void load();
  }, []);
  async function createReward() {
    if (!newReward.trim()) return;
    await apiFetch("/api/rewards", { body: { title: newReward, cost } });
    setNewReward("");
    await load();
  }
  async function redeem(reward: RewardItem) {
    try {
      await apiFetch(`/api/rewards/${reward.id}/redeem`, { body: {} });
      await Promise.all([load(), refresh()]);
      showToast(`「${reward.title}」と交換しました`);
    } catch (error) {
      showToast(
        error instanceof Error && error.message.includes("insufficient_gold")
          ? "ゴールドがもう少し必要です"
          : error instanceof Error
            ? error.message
            : "交換できませんでした",
      );
    }
  }
  const unlocked = achievements.filter(
    (achievement) => achievement.unlocked || achievement.unlocked_at,
  );
  const reviewText = String(
    review?.summary ??
      review?.reflection ??
      review?.message ??
      "今週の記録がたまると、ここに冒険のふりかえりが現れます。",
  );
  return (
    <section className="game-screen me-screen">
      <header className="game-page-head">
        <div>
          <span className="eyebrow">
            <FootprintsIcon size={16} />
            YOUR ADVENTURE
          </span>
          <h1>じぶん</h1>
        </div>
        <Link to="/me/settings" className="game-button ghost sm">
          <Settings size={18} />
          設定
        </Link>
      </header>
      {player && <PlayerCard player={player} />}
      <section className="me-section">
        <header>
          <div>
            <FootprintsIcon />
            <h2>冒険のあしあと</h2>
          </div>
          <span>この4週間</span>
        </header>
        <Card className="footprints">
          <div className="footprint-grid">
            {history.map((day) => (
              <div
                key={day.day}
                className={`footprint-day count-${Math.min(4, day.count)} ${day.rest_day ? "rest" : ""}`}
                title={`${day.day}: ${day.count}つクリア`}
              >
                <span>{day.count > 0 ? day.count : ""}</span>
              </div>
            ))}
          </div>
          <div className="footprint-legend">
            <span>ゆっくり</span>
            <i className="count-1" />
            <i className="count-2" />
            <i className="count-3" />
            <i className="count-4" />
            <span>たくさん</span>
          </div>
        </Card>
      </section>
      <section className="me-section">
        <header>
          <div>
            <Award />
            <h2>実績</h2>
          </div>
          <span>
            {unlocked.length} / {achievements.length}
          </span>
        </header>
        <div className="achievement-grid">
          {achievements.map((achievement) => {
            const Icon =
              achievementIcons[
                achievement.code as keyof typeof achievementIcons
              ] ?? Trophy;
            const open = achievement.unlocked || achievement.unlocked_at;
            return (
              <Card
                key={achievement.code}
                className={`achievement ${open ? "unlocked" : "locked"}`}
              >
                <span>{open ? <Icon /> : <LockKeyhole />}</span>
                <div>
                  <strong>{achievement.title}</strong>
                  <p>{achievement.description}</p>
                </div>
                {open && <Check size={18} />}
              </Card>
            );
          })}
        </div>
      </section>
      <section className="me-section reward-shop">
        <header>
          <div>
            <Gift />
            <h2>ごほうびショップ</h2>
          </div>
          {player && (
            <Chip tone="coin">
              <Coins size={15} />
              {player.gold} G
            </Chip>
          )}
        </header>
        <div className="reward-grid">
          {rewards.map((reward) => (
            <Card key={reward.id} className="reward-card">
              <Gift />
              <div>
                <h3>{reward.title}</h3>
                <Chip tone="coin" className="num">
                  {reward.cost} G
                </Chip>
              </div>
              <Button
                variant="sun"
                size="sm"
                onClick={() => void redeem(reward)}
              >
                交換する
              </Button>
            </Card>
          ))}
        </div>
        <form
          className="new-reward"
          onSubmit={(event) => {
            event.preventDefault();
            void createReward();
          }}
        >
          <input
            value={newReward}
            onChange={(event) => setNewReward(event.target.value)}
            placeholder="自分へのごほうび"
          />
          <input
            type="number"
            min="1"
            value={cost}
            onChange={(event) => setCost(Number(event.target.value))}
            aria-label="必要なゴールド"
          />
          <Button>
            <Plus size={17} />
            追加
          </Button>
        </form>
      </section>
      <section className="me-section" id="journal">
        <header>
          <div>
            <Trophy />
            <h2>冒険日誌</h2>
          </div>
          <span>週次ふりかえり</span>
        </header>
        {review ? (
          <Card className="journal-card">
            <Chip tone="grape">今週の記録</Chip>
            <p>{reviewText}</p>
            {(
              [
                ...(Array.isArray(review.good_points)
                  ? review.good_points
                  : []),
                ...(Array.isArray(review.learnings) ? review.learnings : []),
                ...(Array.isArray(review.focus_next) ? review.focus_next : []),
              ] as unknown[]
            ).length > 0 && (
              <ul>
                {(
                  [
                    ...(Array.isArray(review.good_points)
                      ? review.good_points
                      : []),
                    ...(Array.isArray(review.learnings)
                      ? review.learnings
                      : []),
                    ...(Array.isArray(review.focus_next)
                      ? review.focus_next
                      : []),
                  ] as unknown[]
                ).map((item, index) => (
                  <li key={index}>{String(item)}</li>
                ))}
              </ul>
            )}
          </Card>
        ) : (
          <EmptyState title="冒険日誌を準備しています" />
        )}
      </section>
    </section>
  );
}
