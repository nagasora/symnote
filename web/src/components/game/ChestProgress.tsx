import { Gift } from "lucide-react";
import { ProgressBar } from "../ui/ProgressBar";

export function ChestProgress({
  done,
  target,
  claimed,
}: {
  done: number;
  target: number;
  claimed: boolean;
}) {
  const remaining = Math.max(0, target - done);
  return (
    <div className="chest-progress">
      <div>
        <div className="chest-label">
          <strong>きょうの進み具合</strong>
          <span className="num">
            {done} / {target}
          </span>
        </div>
        <ProgressBar
          value={done}
          max={Math.max(1, target)}
          label="きょうの進み具合"
        />
        <p>
          {claimed
            ? "宝箱を受け取りました"
            : remaining
              ? `あと ${remaining}つで宝箱`
              : "宝箱が開きました"}
        </p>
      </div>
      <span className={claimed || remaining === 0 ? "chest ready" : "chest"}>
        <Gift size={28} />
      </span>
    </div>
  );
}
