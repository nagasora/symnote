import { useEffect, useRef, type RefObject } from "react";

const focusableSelector = [
  "button:not([disabled])",
  "a[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export function useModalFocus(
  open: boolean,
  onClose: () => void,
): RefObject<HTMLElement | null> {
  const containerRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!open || !containerRef.current) return;
    const container = containerRef.current;
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const siblings = container.parentElement
      ? Array.from(container.parentElement.children).filter(
          (node) => node !== container,
        )
      : [];
    const previousInert = siblings.map((node) => ({
      node: node as HTMLElement,
      inert: (node as HTMLElement).inert,
    }));
    previousInert.forEach(({ node }) => {
      node.inert = true;
    });
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const focusFirst = () => {
      if (container.inert) return;
      const preferred = container.querySelector<HTMLElement>(
        "[autofocus], textarea, input:not([type='hidden']), select",
      );
      const first =
        preferred ?? container.querySelector<HTMLElement>(focusableSelector);
      (first ?? container).focus();
    };
    const frame = requestAnimationFrame(focusFirst);
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || container.inert) return;
      if (event.key === "Escape") {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        container.querySelectorAll<HTMLElement>(focusableSelector),
      );
      if (!focusable.length) {
        event.preventDefault();
        container.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("keydown", handleKeyDown);
      previousInert.forEach(({ node, inert }) => {
        node.inert = inert;
      });
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, [open]);

  return containerRef;
}
