"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";
import { AssistWorkflowsView } from "@/components/autopilot/assist-workflows";
import { AuditView } from "@/components/autopilot/audit";
import { AutopilotView } from "@/components/autopilot/area";
import { AssistMemoryView } from "@/components/autopilot/memory-tab";
import { AssistSuggestionsView } from "@/components/autopilot/suggestions-tab";
import { AssistWorkbench } from "@/components/autopilot/workbench";
import { StoredAge } from "@/components/ui/stored-age";
import { FeedNote } from "@/components/feed-note";
import { SectionTabs } from "@/components/shell";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { PageIntro } from "@/components/ui/page-intro";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { getAutopilot, getRecurring, getTeamsConnection } from "@/lib/api";
import { NO_FILTERS } from "@/lib/autopilot";
import { noticeKind, plainError } from "@/lib/errors";
import { useStaleRefresh } from "@/lib/stale-refresh";
import type { AutopilotArea } from "@/lib/types";
import { useRead } from "@/lib/use-read";

// Assist: where a person sees and controls what the Workbench prepares for them. Ready-for-you
// queue (matters and AI-prepared drafts/jobs), proactive suggestions, recurring workflows,
// persistent memory, what Assist did, and switches. Every send is still confirmed by the person.
type Show = "prepared" | "suggestions" | "workflows" | "memory" | "log" | "switches";
const SHOWS: Show[] = ["prepared", "suggestions", "workflows", "memory", "log", "switches"];

export default function AssistPage() {
  return (
    <Suspense>
      <Assist />
    </Suspense>
  );
}

function Assist() {
  const params = useSearchParams();
  const raw = params.get("show");
  const normalized = raw === "routines" ? "workflows" : raw === "queue" ? "prepared" : raw;
  const show: Show = normalized && SHOWS.includes(normalized as Show) ? (normalized as Show) : "prepared";
  return (
    <div className="flex min-h-0 flex-1 flex-col bg-bg" data-own-scroll={show === "prepared" ? "" : undefined}>
      <Toolbar heading title="Assist" />
      <SectionTabs section="assist" />
      <PageIntro path="/assist" />
      <div
        className={
          show === "prepared"
            ? "flex min-h-0 w-full flex-1 flex-col overflow-hidden"
            : "flex min-h-0 w-full flex-1 flex-col overflow-y-auto px-4 py-4 lg:px-6 lg:py-5"
        }
        data-testid={`assist-${show}`}
      >
        {show === "prepared" ? (
          <AssistWorkbench linked={params.get("item")} />
        ) : show === "suggestions" ? (
          <AssistSuggestionsView />
        ) : show === "workflows" ? (
          <AssistWorkflowsView />
        ) : show === "memory" ? (
          <AssistMemoryView />
        ) : show === "log" ? (
          <LogTab />
        ) : (
          <SwitchesTab />
        )}
      </div>
    </div>
  );
}

/** What Assist did for the person: the audit for the owner; each person's own journal is the work log. */
function LogTab() {
  const { data: area, error, retry } = useRead<AutopilotArea>("/api/autopilot", (signal) => getAutopilot(signal));
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!area) return <Pending page label="Reading what Assist did" />;
  return area.owner ? (
    <AuditView area={area} filters={NO_FILTERS} />
  ) : (
    <p className="text-[13px] text-muted" data-testid="assist-log-own">
      What Assist did for you is in your <Link href="/work-log" className="underline underline-offset-2">work log</Link>, newest first, with the time it saved.
    </p>
  );
}

function SwitchesTab() {
  const { data: area, error, retry, at } = useRead<AutopilotArea>("/api/autopilot", (signal) => getAutopilot(signal));
  useStaleRefresh(area?.stale_since, retry);
  // the other two reads start with the main one; the panels that show them join these requests
  useEffect(() => {
    const c = new AbortController();
    getTeamsConnection(c.signal).catch(() => undefined);
    getRecurring(c.signal).catch(() => undefined);
    return () => c.abort();
  }, []);
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!area) return <Pending page label="Reading your switches" />;
  return (
    <>
      <StoredAge at={at} className="pb-1" />
      <FeedNote feed="autopilot" className="mb-2 text-[13px] text-risk" />
      <StaleNote since={area.stale_since} readAt={area.read_at} className="mb-2" />
      <AutopilotView area={area} onReload={retry} />
    </>
  );
}
