"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { SourceChips } from "@/components/source-chip";
import { EmptyState } from "@/components/ui/empty-state";
import { Pending } from "@/components/ui/skeleton";
import { LoadProblem } from "@/components/ui/notice";
import { getUpcomingMeetings } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import { formatDateTime, safeHref } from "@/lib/format";
import type { UpcomingMeetings } from "@/lib/types";

/** Meetings, Upcoming: the next two weeks, each with its brief. */
export function Upcoming() {
  const [list, setList] = useState<UpcomingMeetings | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    getUpcomingMeetings(ctl.signal).then(setList, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [round]);
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  return (
    <div className="space-y-4">
      {!list ? (
        <Pending label="Reading your calendar" />
      ) : list.meetings.length === 0 ? (
        <EmptyState title="No meetings in the next two weeks." line="A customer or supplier page has a Prep button for any meeting." />
      ) : (
        <ul className="divide-y divide-border" data-testid="upcoming-meetings">
          {list.meetings.map((e) => (
            <li key={e.object.object_id} className="flex flex-wrap items-baseline justify-between gap-3 py-2 text-[13px]">
              <Link href={safeHref(e.brief_url) ?? "/meetings"} className="font-medium hover:underline">
                {e.object.label}
              </Link>
              <span className="flex items-center gap-3 text-muted">
                <Link href={safeHref(e.brief_url) ?? "/meetings"} className="rounded-md px-2 py-0.5 text-[12px] font-medium text-fg hover:bg-hover" data-testid="prep-link">
                  Prep
                </Link>
                {formatDateTime(e.start)}
                <SourceChips sources={e.sources} max={1} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
