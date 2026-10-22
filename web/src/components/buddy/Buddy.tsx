export function Buddy({
  expression = "normal",
  size = 64,
}: {
  expression?: "normal" | "happy" | "sleepy";
  size?: number;
}) {
  return (
    <svg
      className="buddy"
      viewBox="0 0 64 64"
      width={size}
      height={size}
      role="img"
      aria-label={`相棒の${expression === "happy" ? "よろこび" : expression === "sleepy" ? "おやすみ" : "ふつう"}の表情`}
    >
      <ellipse
        cx="25"
        cy="12"
        rx="8"
        ry="4.5"
        transform="rotate(-30 25 12)"
        fill="var(--leaf)"
      />
      <ellipse
        cx="39"
        cy="12"
        rx="8"
        ry="4.5"
        transform="rotate(30 39 12)"
        fill="var(--leaf)"
      />
      <rect
        x="31"
        y="12"
        width="2.4"
        height="9"
        rx="1.2"
        fill="var(--leaf-deep)"
      />
      <ellipse
        cx="32"
        cy="40"
        rx="24"
        ry="21"
        fill="var(--buddy-body)"
        stroke="var(--sun-deep)"
        strokeWidth="2"
      />
      {expression === "happy" ? (
        <path
          d="M20 39q4-5 8 0M36 39q4-5 8 0"
          stroke="var(--on-color)"
          strokeWidth="2.6"
          fill="none"
          strokeLinecap="round"
        />
      ) : expression === "sleepy" ? (
        <path
          d="M20 39h8M36 39h8"
          stroke="var(--on-color)"
          strokeWidth="2.6"
          fill="none"
          strokeLinecap="round"
        />
      ) : (
        <>
          <ellipse cx="24" cy="38" rx="3" ry="4" fill="var(--on-color)" />
          <ellipse cx="40" cy="38" rx="3" ry="4" fill="var(--on-color)" />
        </>
      )}
      <ellipse cx="17" cy="46" rx="4" ry="2.5" fill="var(--buddy-cheek)" />
      <ellipse cx="47" cy="46" rx="4" ry="2.5" fill="var(--buddy-cheek)" />
      {expression === "happy" ? (
        <path d="M27 45q5 7 10 0z" fill="var(--on-color)" />
      ) : expression === "sleepy" ? (
        <path
          d="M29 47h6"
          stroke="var(--on-color)"
          strokeWidth="2.5"
          strokeLinecap="round"
        />
      ) : (
        <path
          d="M28 46q4 4 8 0"
          stroke="var(--on-color)"
          strokeWidth="2.5"
          fill="none"
          strokeLinecap="round"
        />
      )}
    </svg>
  );
}
