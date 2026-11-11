"use client";

import { useCallback, useEffect, useState } from "react";
import { MyWorkList } from "@/components/my-work";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { getMyWork } from "@/lib/api";
import { useStaleRefresh } from "@/lib/stale-refresh";
import { noticeKind, plainError } from "@/lib/errors";
import type { MyWork } from "@/lib/types";

// The work the autopilots prepared for you from new mail, tasks, approvals, QC results,
// meetings and record changes. A message goes out only through its preview and your
// confirm; an entry is a dry-run write intent until writes are granted.
export default function MyWorkPage() {
  const [work, setWork] = useState<MyWork | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    getMyWork(ctl.signal).then(setWork, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [round]);
  useStaleRefresh(work?.stale_since, retry);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Assist" />
      <div className="w-full px-6 py-5">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : work ? (
          <>
            <StaleNote since={work.stale_since} readAt={work.read_at} className="mb-2" />
            <MyWorkList work={work} />
          </>
        ) : (
          <Pending page label="Reading the work prepared for you" />
        )}
      </div>
    </div>
  );
}
