import type { LucideIcon } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/cn";

/** What is true and what to do: one sentence, at most one action. */
export function EmptyState({
  title,
  line,
  action,
  className,
}: {
  icon?: LucideIcon;
  title: string;
  line?: string;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("py-3", className)} data-testid="empty-state">
      <p className="text-row text-fg">{title}</p>
      {line && <p className="mt-0.5 max-w-[80ch] text-meta text-muted">{line}</p>}
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}
