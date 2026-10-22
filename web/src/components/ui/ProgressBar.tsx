export function ProgressBar({
  value,
  max = 100,
  tone = "leaf",
  label = "進み具合",
}: {
  value: number;
  max?: number;
  tone?: "leaf" | "sky";
  label?: string;
}) {
  const pct = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  return (
    <div className="progress-wrap">
      <div
        className="game-progress"
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={value}
      >
        <span className={tone} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
