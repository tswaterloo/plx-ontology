"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { SourceChips } from "@/components/source-chip";
import { PageHeader } from "@/components/ui/page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadProblem } from "@/components/ui/notice";
import { getUpcomingMeetings } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import { formatDateTime, safeHref } from "@/lib/format";
import type { UpcomingMeetings } from "@/lib/types";

export default function PreparePage() {
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
      <PageHeader eyebrow="Meeting prep" title="Prepare" sub="Your meetings in the next two weeks. Open one for its brief: orders, late items, threads, promises and quality, from the records you may read." />
      {!list ? (
        <Skeleton className="h-40 w-full" />
      ) : list.meetings.length === 0 ? (
        <p className="text-[13px] text-muted">No upcoming meeting you may read. A customer or supplier page has a Prepare button too.</p>
      ) : (
        <ul className="divide-y divide-border" data-testid="upcoming-meetings">
          {list.meetings.map((e) => (
            <li key={e.object.object_id} className="flex flex-wrap items-baseline justify-between gap-3 py-2 text-[13px]">
              <Link href={safeHref(e.brief_url) ?? "/prepare"} className="font-medium hover:underline">
                {e.object.label}
              </Link>
              <span className="flex items-center gap-3 text-muted">
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
