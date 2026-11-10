"use client";

import { useCallback, useEffect, useState } from "react";
import { AutopilotView } from "@/components/autopilot/area";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { LoadProblem } from "@/components/ui/notice";
import { getAutopilot } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import type { AutopilotArea } from "@/lib/types";

// The autopilots: today's prepared items by person, role, workflow and system, each with its
// evidence and drafted action; per workflow its scores and state; the owner's switches and
// the audit log. Accepting is a recorded dry run until a workflow is live.
export default function AutopilotPage() {
  const [area, setArea] = useState<AutopilotArea | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    getAutopilot(ctl.signal).then(setArea, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [round]);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Autopilot" />
      <div className="mx-auto w-full max-w-5xl px-4 py-5 sm:px-6">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : area ? (
          <AutopilotView area={area} onReload={retry} />
        ) : (
          <Pending label="Reading the autopilots" />
        )}
      </div>
    </div>
  );
}
