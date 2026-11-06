"use client";

import { useCallback, useEffect, useState } from "react";
import { BriefingCard } from "@/components/briefing";
import { loadLate } from "@/components/shell";
import { ErrorNotice } from "@/components/ui/notice";
import { Toolbar } from "@/components/ui/toolbar";
import { getBriefing } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import type { Briefing, LatePo } from "@/lib/types";

function Skeleton() {
  const widths = ["w-[58%]", "w-[49%]", "w-[41%]"];
  return (
    <div className="grid flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_300px] xl:grid-cols-[minmax(0,1fr)_320px]" aria-busy="true" aria-label="Loading the briefing">
      <div className="bg-list">
        <div className="h-10 border-b border-border" />
        <ul className="space-y-1 px-2 pt-9">
          {Array.from({ length: 8 }).map((_, i) => (
            <li key={i} className="flex gap-3 px-3 py-2.5">
              <span className="skeleton size-8 rounded-[9px]" />
              <span className="flex-1 space-y-2">
                <span className={cn("skeleton block h-3", widths[i % 3])} />
                <span className={cn("skeleton block h-2.5", i % 2 ? "w-[30%]" : "w-[38%]")} />
              </span>
            </li>
          ))}
        </ul>
      </div>
      <div className="hidden border-l border-border bg-[color-mix(in_srgb,var(--surface)_70%,var(--bg))] px-6 pt-6 lg:block">
        <div className="skeleton h-6 w-32" />
        <div className="skeleton mt-4 h-3 w-48" />
      </div>
    </div>
  );
}

/** The full briefing. It never marks the briefing seen: Today does that once. */
export default function BriefingPage() {
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [late, setLate] = useState<LatePo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    getBriefing(c.signal)
      .then(setBriefing)
      .catch((e) => !c.signal.aborted && setError(plainError(e).replace(/^Server error\./, "Couldn't load the briefing.")));
    // suppliers for the rows' names; a failure only leaves them out
    loadLate().then((l) => !c.signal.aborted && setLate(l), () => undefined);
    return () => c.abort();
  }, []);
  useEffect(() => load(), [load]);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar title="Briefing" />
      {error ? (
        <div className="p-5">
          <ErrorNotice message={error} onRetry={load} />
        </div>
      ) : !briefing ? (
        <Skeleton />
      ) : (
        <BriefingCard briefing={briefing} latePos={late} />
      )}
    </div>
  );
}
