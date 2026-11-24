"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { AuditView } from "@/components/autopilot/audit";
import { AutopilotView } from "@/components/autopilot/area";
import { PreparedView } from "@/components/autopilot/prepared";
import { RecurringView } from "@/components/autopilot/recurring";
import { StoredAge } from "@/components/ui/stored-age";
import { FeedNote } from "@/components/feed-note";
import { SectionTabs } from "@/components/shell";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { PageIntro } from "@/components/ui/page-intro";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { getAutopilot, getAutopilotDrafts, getRecurring, getTeamsConnection, type DraftList } from "@/lib/api";
import { NO_FILTERS } from "@/lib/autopilot";
import { noticeKind, plainError } from "@/lib/errors";
import { useStaleRefresh } from "@/lib/stale-refresh";
import type { AutopilotArea } from "@/lib/types";
import { useRead } from "@/lib/use-read";

// Assist: where a person sees and controls what the Workbench prepares for them. Prepared work
// (each item with its plan) and the drafts waiting for their OK, their routines, what Assist did
// for them, and the switches. A send is still confirmed by the named sender in the app.
type Show = "prepared" | "routines" | "log" | "switches";
const SHOWS: Show[] = ["prepared", "routines", "log", "switches"];

export default function AssistPage() {
  return (
    <Suspense>
      <Assist />
    </Suspense>
  );
}

function Assist() {
  const params = useSearchParams();
  const asked = params.get("show") as Show | null;
  const show: Show = asked && SHOWS.includes(asked) ? asked : "prepared";
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Assist" />
      <SectionTabs section="assist" />
      <PageIntro path="/assist" />
      <div className="w-full px-6 py-5" data-testid={`assist-${show}`}>
        {show === "prepared" ? <PreparedTab linked={params.get("item")} /> : show === "routines" ? <RecurringView /> : show === "log" ? <LogTab /> : <SwitchesTab />}
      </div>
    </div>
  );
}

/** What is prepared for this person and what waits on their OK: the one list Home reads too. */
function PreparedTab({ linked }: { linked: string | null }) {
  const [all, setAll] = useState(false);
  const { data, error, retry } = useRead<DraftList>(all ? "assist-drafts-all" : "assist-drafts", (signal) => getAutopilotDrafts(signal, all));
  useStaleRefresh(data?.stale_since, retry);
  const router = useRouter();
  const [gone, setGone] = useState<Set<string>>(new Set());
  const onGone = useCallback((id: string) => setGone((s) => new Set(s).add(id)), []);
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!data) return <Pending page label="Reading what is prepared for you" />;
  const items = data.items.filter((i) => !gone.has(i.item_id));
  return (
    <>
      <FeedNote feed="autopilot" className="mb-2 text-[13px] text-risk" />
      <StaleNote since={data.stale_since} className="mb-2" />
      {data.status === "off" && <p className="mb-3 text-[13px] text-muted" data-testid="assist-off">{data.reason ?? "Assist is not switched on for you. Turn it on under Switches."}</p>}
      <PreparedView items={items} linked={linked} onGone={onGone} onChanged={retry} empty="Nothing is prepared for you right now." />
      {(data.older ?? 0) > 0 && !all && (
        <button type="button" onClick={() => setAll(true)} className="mt-3 cursor-pointer text-[13px] text-muted underline underline-offset-2 hover:text-fg" data-testid="assist-older">
          Show the {data.older} older {data.older === 1 ? "item" : "items"}
        </button>
      )}
      <p className="mt-6 text-[12px] text-muted">Nothing goes out without the sender&apos;s confirmation here. <button type="button" className="underline underline-offset-2 cursor-pointer" onClick={() => router.push("/assist?show=switches")}>Change what is prepared</button>.</p>
    </>
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
