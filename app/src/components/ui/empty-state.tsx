import type { LucideIcon } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/cn";

/** What is true, and what to do: one plain sentence, the evidence, at most one action. No illustration. */
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
    <div className={cn("py-4", className)} data-testid="empty-state">
      <p className="flex items-center gap-2 text-row font-medium text-fg">
        <Icon className="size-4 shrink-0 text-ok" aria-hidden />
        {title}
      </p>
      {line && <p className="mt-1 max-w-[68ch] text-row text-muted">{line}</p>}
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}
