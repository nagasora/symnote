import type { ButtonHTMLAttributes } from "react";

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "sun" | "ghost" | "quiet" | "leaf";
  size?: "md" | "sm";
};

export function Button({
  variant = "ghost",
  size = "md",
  className = "",
  ...props
}: Props) {
  return (
    <button
      className={`game-button ${variant} ${size} ${className}`}
      {...props}
    />
  );
}
