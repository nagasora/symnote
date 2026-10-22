import { useRef } from "react";
import { Check, Gift, RotateCcw } from "lucide-react";
import { useGame } from "../../features/game/GameContext";
import { useModalFocus } from "../../hooks/useModalFocus";
import { BuddyBubble } from "../buddy/BuddyBubble";
import { Button } from "../ui/Button";
import { Chip } from "../ui/Chip";
import { ProgressBar } from "../ui/ProgressBar";
import { ChestProgress } from "./ChestProgress";

export function ClearCelebration() {
  const { celebration, player, today, setCelebration, reopen } = useGame();
  const touchY = useRef<number | null>(null);
  const close = () => setCelebration(null);
  const modalRef = useModalFocus(Boolean(celebration), close);
  if (!celebration) return null;
  const { task, reward, daily } = celebration;
  return (
    <div
      ref={modalRef as React.RefObject<HTMLDivElement>}
      className="celebration-layer"
      role="dialog"
      aria-modal="true"
      aria-label="クエストクリア"
      tabIndex={-1}
      onTouchStart={(event) => {
        touchY.current = event.touches[0].clientY;
      }}
      onTouchEnd={(event) => {
        if (
          touchY.current != null &&
          event.changedTouches[0].clientY - touchY.current > 80
        )
          close();
        touchY.current = null;
      }}
    >
      <div className="celebration-card">
        <span className="clear-medal">
          <Check size={54} strokeWidth={3} />
        </span>
        <h2>クエストクリア！</h2>
        <p className="celebration-title">{task.title}</p>
        <div className="celebration-rewards">
          <Chip tone="xp" className="num">
            +{reward.xp + (daily?.xp ?? 0)} XP
          </Chip>
          <Chip tone="coin" className="num">
            +{reward.gold + (daily?.gold ?? 0)} G
          </Chip>
          {daily && (
            <Chip tone="coin">
              <Gift size={15} />
              きょうの宝箱
            </Chip>
          )}
          {reward.bonuses.map((bonus) => (
            <Chip key={bonus} tone="xp">
              {bonus}
            </Chip>
          ))}
          {reward.new_achievements.map((achievement) => (
            <Chip key={achievement.code} tone="grape">
              {achievement.title}
            </Chip>
          ))}
        </div>
        {player && (
          <div className="celebration-level">
            <div>
              <strong>
                Lv {player.level} {player.title}
              </strong>
              <span className="num">
                {player.xp_into_level} / {player.xp_for_next} XP
              </span>
            </div>
            <ProgressBar
              label="次のレベルまでの経験値"
              value={player.xp_into_level}
              max={player.xp_for_next}
            />
          </div>
        )}
        {today?.daily_clear && <ChestProgress {...today.daily_clear} />}
        <BuddyBubble
          expression="happy"
          name={player?.buddy.name}
          line={
            reward.level_after > reward.level_before
              ? "レベルアップ！ また行ける場所が増えたね。"
              : daily
                ? "きょうの冒険、ぜんぶできたね。宝箱をどうぞ！"
                : reward.new_achievements.length
                  ? "新しい実績だよ。小さな一歩が形になったね。"
                  : task.quest?.is_boss
                    ? "大物クリア！ しっかり前に進んだね。"
                    : "一歩進んだね。きょうもおつかれさま。"
          }
        />
        <div className="celebration-actions">
          <Button variant="sun" onClick={close}>
            つぎのクエストへ
          </Button>
          <Button onClick={close}>ひと休み</Button>
          <Button
            variant="quiet"
            onClick={() => void reopen(task, task.status)}
          >
            <RotateCcw size={17} />
            元に戻す
          </Button>
        </div>
      </div>
    </div>
  );
}
