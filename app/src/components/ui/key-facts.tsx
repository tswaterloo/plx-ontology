import * as React from "react";
import { cn } from "@/lib/cn";

export interface KeyFact {
  label: string;
  value: React.ReactNode;
}

/** Up to six facts as a two-column list, then one source line (no chip per fact). */
export function KeyFacts({ facts, source, className }: { facts: KeyFact[]; source?: React.ReactNode; className?: string }) {
  const shown = facts.filter((f) => f.value !== null && f.value !== undefined && f.value !== "").slice(0, 6);
  if (shown.length === 0 && !source) return null;
  return (
    <div className={className} data-testid="key-facts">
      <dl className="grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-4 gap-y-2">
        {shown.map((f) => (
          <div key={f.label} className="contents">
            <dt className="text-meta text-muted">{f.label}</dt>
            <dd className={cn("min-w-0 break-words text-row font-medium")}>{f.value}</dd>
          </div>
        ))}
      </dl>
      {source && <div className="mt-3 text-meta text-muted">{source}</div>}
    </div>
  );
}
