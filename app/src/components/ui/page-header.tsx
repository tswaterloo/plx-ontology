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
  level = 1,
}: {
  /** 2 when the page already has its h1 (a tab of a page). */
  level?: 1 | 2;
  eyebrow?: React.ReactNode;
  title: React.ReactNode;
  sub?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
  testId?: string;
}) {
  return (
    <header className={cn("flex flex-wrap items-end justify-between gap-x-6 gap-y-2", className)} data-testid={testId}>
      <div className="min-w-0 space-y-1">
        {eyebrow && <p className="text-meta text-muted">{eyebrow}</p>}
        {React.createElement(level === 2 ? "h2" : "h1", { className: "break-words text-[24px] leading-[30px] font-semibold tracking-[-0.025em]" }, title)}
        {sub && <p className="max-w-[80ch] text-[13px] leading-5 text-muted">{sub}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2 max-sm:w-full max-sm:flex-wrap">{actions}</div>}
    </header>
  );
}
