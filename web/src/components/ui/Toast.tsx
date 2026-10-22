export function Toast({
  message,
  actionLabel,
  onAction,
}: {
  message: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  if (!message) return null;
  return (
    <div className="game-toast" role="status">
      <span>{message}</span>
      {actionLabel && (
        <button type="button" onClick={onAction}>
          {actionLabel}
        </button>
      )}
    </div>
  );
}
