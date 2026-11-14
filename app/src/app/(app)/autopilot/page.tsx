"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";
import { AutopilotView } from "@/components/autopilot/area";
import { FormFill } from "@/components/autopilot/form-fill";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { ThreadContext } from "@/components/work-log";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { getAutopilot } from "@/lib/api";
import { useRead } from "@/lib/use-read";
import { useStaleRefresh } from "@/lib/stale-refresh";
import { noticeKind, plainError } from "@/lib/errors";
import type { AutopilotArea } from "@/lib/types";

// The autopilots: today's prepared items by person, role, workflow and system, each with its
// evidence and drafted action; per workflow its scores and state; your own switches (each
// person turns their own workflows on); the owner's stop switches and the audit log.
// Accepting is a recorded dry run until a workflow is on for you.
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
  const { data: area, error, retry } = useRead<AutopilotArea>("autopilot", (signal) => getAutopilot(signal));
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
        <div className="mt-8">
          <FormFill />
        </div>
      </div>
    </div>
  );
}
