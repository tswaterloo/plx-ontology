"use client";

import { useCallback, useEffect, useState } from "react";
import { WorkloadView } from "@/components/workload-view";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadProblem } from "@/components/ui/notice";
import { getWorkload } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import type { Workload } from "@/lib/types";

// Who is overloaded: counts per team for everyone; per person only your own counts and
// those of the teams the owner granted you. Counts only, never message content.
export default function WorkloadPage() {
  const [data, setData] = useState<Workload | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    setData(null);
    getWorkload(ctl.signal).then(setData, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [round]);
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!data) return <Skeleton className="h-64 w-full" />;
  return <WorkloadView data={data} />;
}
