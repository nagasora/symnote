import { Buddy } from "./Buddy";

export function BuddyBubble({
  line,
  name = "ミナ",
  expression = "normal",
}: {
  line: string;
  name?: string;
  expression?: "normal" | "happy" | "sleepy";
}) {
  return (
    <div className="buddy-bubble">
      <Buddy size={68} expression={expression} />
      <div className="bubble-copy">
        <span>{name}</span>
        <p>{line}</p>
      </div>
    </div>
  );
}
