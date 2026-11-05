import * as React from "react";
import { cn } from "@/lib/cn";

/** A page's title, an optional kind above it ("Purchase order"), one muted line under it, actions on the right. */
export function PageHeader({
  eyebrow,
  title,
  sub,
  actions,
  className,
  testId,
}: {
  eyebrow?: React.ReactNode;
  title: React.ReactNode;
  sub?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
  testId?: string;
}) {
  return (
    <header className={cn("flex flex-wrap items-end justify-between gap-x-6 gap-y-2 border-b border-border pb-3", className)} data-testid={testId}>
      <div className="min-w-0 space-y-0.5">
        {eyebrow && <p className="text-meta text-muted">{eyebrow}</p>}
        <h1 className="text-display">{title}</h1>
        {sub && <p className="max-w-[80ch] text-row text-muted">{sub}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}
