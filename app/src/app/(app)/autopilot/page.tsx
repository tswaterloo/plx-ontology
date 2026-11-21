"use client";

import { Suspense } from "react";
import { AutopilotView } from "@/components/autopilot/area";
import { RecurringView } from "@/components/autopilot/recurring";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { PageIntro } from "@/components/ui/page-intro";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { getAutopilot } from "@/lib/api";
import { useRead } from "@/lib/use-read";
import { useStaleRefresh } from "@/lib/stale-refresh";
import { noticeKind, plainError } from "@/lib/errors";
import type { AutopilotArea } from "@/lib/types";

// The autopilot: your five switches and what was prepared for you, to confirm, edit, copy or open in Outlook.
export default function AutopilotPage() {
  return (
    <Suspense>
      <Autopilot />
    </Suspense>
  );
}

function Autopilot() {
  const { data: area, error, retry } = useRead<AutopilotArea>("autopilot", (signal) => getAutopilot(signal));
  useStaleRefresh(area?.stale_since, retry);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Autopilot" />
      <PageIntro path="/autopilot" />
      <div className="w-full px-6 py-5">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : area ? (
          <>
            <StaleNote since={area.stale_since} readAt={area.read_at} className="mb-2" />
            <AutopilotView area={area} onReload={retry} />
            <RecurringView />
          </>
        ) : (
          <Pending page label="Reading the autopilots" />
        )}
      </div>
    </div>
  );
}
