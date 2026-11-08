"use client";

import { useCallback, useEffect, useState } from "react";
import { BriefView, mondayBefore } from "@/components/brief-view";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadProblem } from "@/components/ui/notice";
import { getWeeklyReview } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import type { Brief } from "@/lib/types";

export default function WeeklyPage() {
  const [weeks, setWeeks] = useState(1);
  const [brief, setBrief] = useState<Brief | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    setBrief(null);
    getWeeklyReview(mondayBefore(new Date(), weeks), ctl.signal).then(setBrief, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [weeks, round]);
  const pick = (
    <div className="flex gap-2 text-[13px]">
      <button type="button" className="rounded-md px-2 py-1 hover:bg-hover" onClick={() => setWeeks((w) => w + 1)}>
        Earlier week
      </button>
      <button type="button" className="rounded-md px-2 py-1 hover:bg-hover disabled:opacity-40" disabled={weeks <= 0} onClick={() => setWeeks((w) => Math.max(0, w - 1))}>
        Later week
      </button>
    </div>
  );
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  return (
    <div className="space-y-3">
      {pick}
      {brief ? <BriefView brief={brief} eyebrow="Weekly review" /> : <Skeleton className="h-64 w-full" />}
    </div>
  );
}
