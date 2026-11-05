"use client";

import { useCallback, useEffect, useState } from "react";
import { BriefingCard } from "@/components/briefing";
import { PageHeader } from "@/components/ui/page-header";
import { ErrorNotice } from "@/components/ui/notice";
import { SkeletonRows } from "@/components/ui/skeleton";
import { getBriefing, getHome } from "@/lib/api";
import { plainError } from "@/lib/errors";
import type { Briefing, LatePo } from "@/lib/types";

/** The full briefing. It never marks the briefing seen: Home does that once. */
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
    getHome(c.signal)
      .then((h) => setLate(h.needs_attention?.late_pos ?? []))
      .catch(() => undefined);
    return () => c.abort();
  }, []);
  useEffect(() => load(), [load]);
  return (
    <div className="mx-auto w-full max-w-[1200px] px-4 pt-6 pb-16 sm:px-8 sm:pt-8">
      <PageHeader
        className="mb-4"
        title="Briefing"
        sub="Everything open for your team, most urgent first."
      />
      {error ? (
        <ErrorNotice message={error} onRetry={load} />
      ) : !briefing ? (
        <SkeletonRows label="Loading the briefing" />
      ) : (
        <BriefingCard briefing={briefing} latePos={late} />
      )}
    </div>
  );
}
