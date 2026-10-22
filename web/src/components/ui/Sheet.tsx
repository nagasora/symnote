import { X } from "lucide-react";
import type { ReactNode } from "react";
import { useModalFocus } from "../../hooks/useModalFocus";
import { Button } from "./Button";

export function Sheet({
  open,
  title,
  onClose,
  children,
  className = "",
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  className?: string;
}) {
  const modalRef = useModalFocus(open, onClose);
  if (!open) return null;
  return (
    <div
      ref={modalRef as React.RefObject<HTMLDivElement>}
      className="sheet-layer"
      role="presentation"
      tabIndex={-1}
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <section
        className={`game-sheet ${className}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <span className="sheet-grabber" aria-hidden="true" />
        <header>
          <h2>{title}</h2>
          <Button
            variant="quiet"
            size="sm"
            aria-label="閉じる"
            onClick={onClose}
          >
            <X size={20} />
          </Button>
        </header>
        {children}
      </section>
    </div>
  );
}
