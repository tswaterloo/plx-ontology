"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { BriefView, mondayBefore } from "@/components/brief-view";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { getWeeklyReview } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import { formatDate } from "@/lib/format";
import type { Brief } from "@/lib/types";

/** Meetings, Weekly review: the standing meeting, one week at a time. */
export function WeeklyReview() {
  const [weeks, setWeeks] = useState(1);
  const [brief, setBrief] = useState<Brief | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  const monday = mondayBefore(new Date(), weeks);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    setBrief(null);
    getWeeklyReview(monday, ctl.signal).then(setBrief, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [monday, round]);
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-1 text-[13px]" data-testid="week-picker">
        <button type="button" aria-label="Earlier week" className="flex size-7 items-center justify-center rounded-md hover:bg-hover cursor-pointer" onClick={() => setWeeks((w) => w + 1)}>
          <ChevronLeft className="size-4" aria-hidden />
        </button>
        <span className="min-w-[9rem] text-center font-medium tabular-nums">Week of {formatDate(monday, { short: true })}</span>
        <button type="button" aria-label="Later week" className="flex size-7 items-center justify-center rounded-md hover:bg-hover disabled:opacity-40 cursor-pointer" disabled={weeks <= 0} onClick={() => setWeeks((w) => Math.max(0, w - 1))}>
          <ChevronRight className="size-4" aria-hidden />
        </button>
      </div>
      {error ? (
        <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
      ) : brief ? (
        <BriefView brief={brief} level={2} />
      ) : (
        <Pending label="Gathering the week" />
      )}
    </div>
  );
}
