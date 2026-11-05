import * as React from "react";
import { cn } from "@/lib/cn";

/**
 * Every page starts the same way, like a paper's masthead: a small dateline or kicker, the
 * verdict as the title in the serif, one muted line of why, actions on the right, and a
 * 2 px ink rule under it.
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
    <header className={cn("flex flex-wrap items-end justify-between gap-x-6 gap-y-3 border-b-2 border-rule pb-4", className)} data-testid={testId}>
      <div className="min-w-0 space-y-1.5">
        {eyebrow && <p className="text-eyebrow uppercase text-muted">{eyebrow}</p>}
        <h1 className="text-display text-balance">{title}</h1>
        {sub && <p className="max-w-[68ch] text-row text-muted sm:text-body">{sub}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}
