"use client";

import { useCallback, useEffect, useState } from "react";
import { RdBoardView } from "@/components/rd-board";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadProblem } from "@/components/ui/notice";
import { getRdBoard } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import type { RdBoard } from "@/lib/types";

// R&D and sample projects per customer: status, last activity, owner and who each waits on.
// Built only from the threads and records you may open; updates are drafts.
export default function RdProjectsPage() {
  const [board, setBoard] = useState<RdBoard | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    setBoard(null);
    getRdBoard(ctl.signal).then(setBoard, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [round]);
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!board) return <Skeleton className="h-64 w-full" />;
  return <RdBoardView board={board} />;
}
