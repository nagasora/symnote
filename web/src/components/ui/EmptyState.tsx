import type { ReactNode } from "react";
import { Card } from "./Card";

export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <Card className="empty-state">
      <strong>{title}</strong>
      {children && <p>{children}</p>}
      {action}
    </Card>
  );
}
