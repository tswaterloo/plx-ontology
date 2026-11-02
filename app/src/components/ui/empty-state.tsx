import type { LucideIcon } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/cn";

/** What is true, and what to do: icon, title, one line, an optional action. */
export function EmptyState({
  icon: Icon,
  title,
  line,
  action,
  className,
}: {
  icon: LucideIcon;
  title: string;
  line?: string;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center px-4 py-8 text-center", className)} data-testid="empty-state">
      <span className="flex size-10 items-center justify-center rounded-full bg-surface-2 text-muted">
        <Icon className="size-5" aria-hidden />
      </span>
      <p className="mt-3 text-section">{title}</p>
      {line && <p className="mt-1 max-w-md text-row text-muted">{line}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
