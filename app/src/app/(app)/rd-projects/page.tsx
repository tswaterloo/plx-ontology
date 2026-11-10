"use client";

import { useCallback, useEffect, useState } from "react";
import { RdBoardView } from "@/components/rd-board";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
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
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar
        heading
        title={
          <>
            R&amp;D projects <span className="ml-2 rounded bg-hover px-1 text-[11px] font-normal">Early</span>
          </>
        }
      />
      <div className="mx-auto w-full max-w-3xl px-6 py-6">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : board ? (
          <RdBoardView board={board} />
        ) : (
          <Pending label="Reading R&D and sample projects" />
        )}
      </div>
    </div>
  );
}
