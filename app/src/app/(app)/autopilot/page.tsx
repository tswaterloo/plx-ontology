"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { AutopilotView } from "@/components/autopilot/area";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { ThreadContext } from "@/components/work-log";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { getAutopilot } from "@/lib/api";
import { useStaleRefresh } from "@/lib/stale-refresh";
import { noticeKind, plainError } from "@/lib/errors";
import type { AutopilotArea } from "@/lib/types";

// The autopilots: today's prepared items by person, role, workflow and system, each with its
// evidence and drafted action; per workflow its scores and state; the owner's switches and
// the audit log. Accepting is a recorded dry run until a workflow is live.
export default function AutopilotPage() {
  return (
    <Suspense>
      <Autopilot />
    </Suspense>
  );
}

function Autopilot() {
  // "Continue" from the work log: the item's earlier work above, the item itself in view
  const item = useSearchParams().get("item");
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
  useStaleRefresh(area?.stale_since, retry);
  useEffect(() => {
    if (!area || !item) return;
    const el = [...document.querySelectorAll<HTMLElement>("li[data-key]")].find((li) => li.dataset.key?.endsWith(`/${item}`));
    el?.scrollIntoView({ block: "center" });
    el?.focus();
  }, [area, item]);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Autopilot" />
      <div className="w-full px-6 py-5">
        {item && <ThreadContext thread={`autopilot:${item}`} title="Your work on this item" />}
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : area ? (
          <>
            <StaleNote since={area.stale_since} readAt={area.read_at} className="mb-2" />
            <AutopilotView area={area} onReload={retry} />
          </>
        ) : (
          <Pending page label="Reading the autopilots" />
        )}
      </div>
    </div>
  );
}
