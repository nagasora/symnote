import type { HTMLAttributes } from "react";

export function Chip({
  tone = "plain",
  className = "",
  ...props
}: HTMLAttributes<HTMLSpanElement> & {
  tone?: "xp" | "coin" | "sky" | "berry" | "grape" | "plain";
}) {
  return <span className={`game-chip ${tone} ${className}`} {...props} />;
}
