"use client";

import { useCallback, useEffect, useState } from "react";
import { MyWorkList } from "@/components/my-work";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadProblem } from "@/components/ui/notice";
import { getMyWork } from "@/lib/api";
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
    setWork(null);
    getMyWork(ctl.signal).then(setWork, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [round]);
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!work) return <Skeleton className="h-64 w-full" />;
  return <MyWorkList work={work} />;
}
