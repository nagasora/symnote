import { Check } from "lucide-react";

export function CheckButton({
  done,
  boss,
  onClick,
}: {
  done: boolean;
  boss?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={`quest-check ${done ? "done" : ""} ${boss ? "boss" : ""}`}
      aria-label={done ? "クリア済み。元に戻す" : "クリアにする"}
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
    >
      {done && <Check size={25} strokeWidth={3.2} />}
    </button>
  );
}
