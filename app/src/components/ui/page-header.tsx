import * as React from "react";
import { cn } from "@/lib/cn";

/**
 * Every page starts the same way: a small eyebrow (what this is), the verdict as the
 * title, one muted line of why, and optional actions on the right.
 */
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
    <header className={cn("flex flex-wrap items-end justify-between gap-x-6 gap-y-3", className)} data-testid={testId}>
      <div className="min-w-0 space-y-1">
        {eyebrow && <p className="text-eyebrow uppercase text-muted">{eyebrow}</p>}
        <h1 className="text-display text-balance">{title}</h1>
        {sub && <p className="max-w-3xl text-row text-muted sm:text-body">{sub}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}
