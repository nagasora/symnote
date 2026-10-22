import type { HTMLAttributes } from "react";

export function Card({
  className = "",
  ...props
}: HTMLAttributes<HTMLDivElement> & { tone?: "sun" | "plain" }) {
  const tone = "tone" in props ? props.tone : undefined;
  const rest = { ...props };
  delete rest.tone;
  return (
    <div
      className={`game-card ${tone === "sun" ? "tone-sun" : ""} ${className}`}
      {...rest}
    />
  );
}
