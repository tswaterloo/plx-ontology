"use client";

import { Suspense, useEffect } from "react";
import { AutopilotView } from "@/components/autopilot/area";
import { RecurringView } from "@/components/autopilot/recurring";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { SectionTabs } from "@/components/shell";
import { PageIntro } from "@/components/ui/page-intro";
import { FeedNote } from "@/components/feed-note";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { getAutopilot, getRecurring, getTeamsConnection } from "@/lib/api";
import { useRead } from "@/lib/use-read";
import { useStaleRefresh } from "@/lib/stale-refresh";
import { noticeKind, plainError } from "@/lib/errors";
import type { AutopilotArea } from "@/lib/types";

// Home's Switches tab: what is prepared for you and what may go by itself. The items are on Home.
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
  // the page's other two reads start with the main one (they were asked for once the area was
  // drawn); the panels that show them join these requests
  useEffect(() => {
    const c = new AbortController();
    getTeamsConnection(c.signal).catch(() => undefined);
    getRecurring(c.signal).catch(() => undefined);
    return () => c.abort();
  }, []);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Switches" />
      <SectionTabs section="home" />
      <PageIntro path="/autopilot" />
      <div className="w-full px-6 py-5">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : area ? (
          <>
            <FeedNote feed="autopilot" className="mb-2 text-[13px] text-risk" />
            <StaleNote since={area.stale_since} readAt={area.read_at} className="mb-2" />
            <AutopilotView area={area} onReload={retry} />
            <RecurringView />
          </>
        ) : (
          <Pending page label="Reading your switches" />
        )}
      </div>
    </div>
  );
}
