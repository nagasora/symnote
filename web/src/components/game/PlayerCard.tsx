import { Flame, Coins } from "lucide-react";
import type { Player } from "../../api";
import { Card } from "../ui/Card";
import { Chip } from "../ui/Chip";
import { ProgressBar } from "../ui/ProgressBar";

export function PlayerCard({
  player,
  compact = false,
}: {
  player: Player;
  compact?: boolean;
}) {
  return (
    <Card className={`player-card ${compact ? "compact" : ""}`}>
      <div className="player-level">
        <span className="level-orb num">{player.level}</span>
        <div>
          <span>レベル {player.level}</span>
          <strong>{player.title}</strong>
        </div>
      </div>
      <ProgressBar
        value={player.xp_into_level}
        max={player.xp_for_next}
        label="次のレベルまでの経験値"
      />
      <div className="player-meta">
        <span className="num">
          {player.xp_into_level} / {player.xp_for_next} XP
        </span>
        <div>
          <Chip tone="berry">
            <Flame size={15} />
            {player.streak_days}日
          </Chip>
          <Chip tone="coin">
            <Coins size={15} />
            {player.gold} G
          </Chip>
        </div>
      </div>
    </Card>
  );
}
