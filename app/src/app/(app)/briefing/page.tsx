"use client";

import { useCallback, useEffect, useState } from "react";
import { BriefingCard } from "@/components/briefing";
import { Card, CardBody } from "@/components/ui/card";
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
      .catch((e) => !c.signal.aborted && setError(plainError(e).replace(/^Something went wrong on our side\./, "Your briefing didn't load.")));
    // suppliers for the rows' names; a failure only leaves them out
    getHome(c.signal)
      .then((h) => setLate(h.needs_attention?.late_pos ?? []))
      .catch(() => undefined);
    return () => c.abort();
  }, []);
  useEffect(() => load(), [load]);
  return (
    <div className="mx-auto w-full max-w-[1200px] px-4 pt-6 pb-16 sm:px-6 sm:pt-8 xl:px-8">
      <h1 className="text-title">Briefing</h1>
      <p className="mt-1 text-row text-muted">Ranked by customer orders affected, then money, then what is new, then days.</p>
      <Card className="mt-6">
        <CardBody className="pt-4 sm:pt-5">
          {error ? (
            <ErrorNotice message={error} onRetry={load} />
          ) : !briefing ? (
            <SkeletonRows rows={8} label="Loading your briefing" />
          ) : (
            <BriefingCard briefing={briefing} latePos={late} />
          )}
        </CardBody>
      </Card>
    </div>
  );
}
