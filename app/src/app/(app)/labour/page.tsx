"use client";

import { useCallback, useEffect, useState } from "react";
import { LabourView } from "@/components/labour-view";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadProblem } from "@/components/ui/notice";
import { getLabour } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import type { LabourPage } from "@/lib/types";

export default function LabourRoute() {
  const [page, setPage] = useState<LabourPage | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    getLabour(ctl.signal).then(setPage, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [round]);
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  return page ? <LabourView page={page} /> : <Skeleton className="h-64 w-full" />;
}
