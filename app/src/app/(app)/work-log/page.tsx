"use client";

import { RefreshCw, Search, X } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useAuth } from "@/components/auth";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar, ToolbarButton } from "@/components/ui/toolbar";
import { PageIntro } from "@/components/ui/page-intro";
import { SectionTabs } from "@/components/shell";
import { AutopilotScorecardPanel } from "@/components/autopilot-scorecard";
import { TimeSavedPanel } from "@/components/time-saved";
import { KIND_LABEL, WorkLogRow } from "@/components/work-log";
import { getWorkLog, workLogPath, type WorkLogFilters } from "@/lib/api";
import { useRead } from "@/lib/use-read";
import { StoredAge } from "@/components/ui/stored-age";
import { cn } from "@/lib/cn";
import { useViewingAs } from "@/components/view-as";
import { plainError } from "@/lib/errors";
import type { WorkLogEvent, WorkLogKind, WorkLogPage } from "@/lib/types";

const TZ = "America/Toronto";
const KINDS = Object.keys(KIND_LABEL) as WorkLogKind[];
const dayKey = (iso: string) => new Date(iso).toLocaleDateString("en-CA", { timeZone: TZ });

function dayLabel(iso: string, now = new Date()): string {
  const k = dayKey(iso);
  if (k === dayKey(now.toISOString())) return "Today";
  if (k === dayKey(new Date(now.getTime() - 86_400_000).toISOString())) return "Yesterday";
  return new Date(iso).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: TZ });
}

function useLog(filters: WorkLogFilters) {
  const { data: page, error: failed, retry: load, at } = useRead<WorkLogPage>(workLogPath(filters), (s) => getWorkLog(filters, s));
  return { page, error: failed ? plainError(failed) : null, load, at };
}

/** Everything you did or received in the Workbench, newest first: one log, searchable, and each line continues where you left it. */
function WorkLog() {
  const params = useSearchParams();
  const router = useRouter();
  const record = params.get("record") ?? undefined;
  const [q, setQ] = useState(params.get("q") ?? "");
  const [query, setQuery] = useState(q);
  const [kind, setKind] = useState<WorkLogKind | undefined>(undefined);
  const [everyone, setEveryone] = useState(false);
  // the owner may read everyone's log: /api/me says so (the server still decides each read),
  // so no other reader's page asks for everyone's and is refused; /api/me stays the owner's
  // while they view as someone, and that person's page offers no Everyone either
  const { state } = useAuth();
  const viewing = useViewingAs();
  const owner = state.status === "signed-in" && state.me.owner === true && !viewing;
  const main = useLog({ q: query || undefined, kind, record, everyone, limit: 200 });
  const waiting = useLog({ waiting: true, limit: 20 });
  const pinned = useLog({ pinned: true, limit: 20 });

  const refresh = useCallback(() => {
    main.load();
    waiting.load();
    pinned.load();
  }, [main, waiting, pinned]);

  const days = useMemo(() => {
    const out: { key: string; label: string; rows: WorkLogEvent[] }[] = [];
    for (const e of main.page?.events ?? []) {
      const k = dayKey(e.at);
      const last = out[out.length - 1];
      if (last?.key === k) last.rows.push(e);
      else out.push({ key: k, label: dayLabel(e.at), rows: [e] });
    }
    return out;
  }, [main.page]);

  const open = (waiting.page?.events ?? []).filter((e) => e.loop && (e.loop.state === "waiting" || e.loop.state === "overdue"));
  const pins = useMemo(() => {
    const seen = new Set<string>();
    return (pinned.page?.events ?? []).filter((e) => !seen.has(e.thread) && seen.add(e.thread));
  }, [pinned.page]);
  const closedLog = main.page && main.page.total === null;
  // how many of each kind the unfiltered list holds (kept while a kind is picked, so the counts stay put)
  const [counts, setCounts] = useState<Partial<Record<WorkLogKind, number>> | null>(null);
  useEffect(() => {
    if (kind || !main.page) return;
    const c: Partial<Record<WorkLogKind, number>> = {};
    for (const e of main.page.events ?? []) c[e.kind] = (c[e.kind] ?? 0) + 1;
    setCounts(c);
  }, [kind, main.page]);

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-list">
      <Toolbar
        pin="Work log"
        heading
        title={
          <>
            Work log
            {main.page?.total ? <>{" "}<span className="ml-1 text-[13px] font-normal tracking-normal tabular-nums text-faint">{main.page.total}</span></> : null}
          </>
        }
        actions={
          <div className="flex items-center gap-1">
            <ToolbarButton onClick={refresh} title="Refresh work log" aria-label="Refresh work log">
              <RefreshCw className={cn("size-3.5", main.at !== null && "animate-spin")} strokeWidth={1.9} aria-hidden />
              <span className="max-sm:sr-only">Refresh</span>
            </ToolbarButton>
            {owner && (
              <ToolbarButton aria-pressed={everyone} onClick={() => setEveryone((v) => !v)} className={cn(everyone && "text-fg")}>
                {everyone ? "Everyone" : "Only mine"}
              </ToolbarButton>
            )}
          </div>
        }
        className="bg-list"
      />
      <SectionTabs section="work-log" />
      <PageIntro path="/work-log" />
      <StoredAge at={main.at} className="px-6 pb-1" />
      {/* a closed log has no counts and nothing to search: no figures that read as zero */}
      {!closedLog && <form
        className="flex flex-wrap items-center gap-2 px-6 pt-3"
        role="search"
        onSubmit={(ev) => {
          ev.preventDefault();
          setQuery(q.trim());
        }}
      >
        <label className="flex h-8 min-w-[240px] flex-1 items-center gap-2 rounded-lg bg-bg px-2.5 shadow-lift focus-within:shadow-[0_0_0_1px_var(--accent)]">
          <Search className="size-[15px] shrink-0 text-muted" aria-hidden />
          <input
            value={q}
            onChange={(ev) => setQ(ev.target.value)}
            onKeyDown={(ev) => {
              if (ev.key === "Escape" && (q || query)) {
                ev.preventDefault();
                setQ("");
                setQuery("");
              }
            }}
            placeholder="Search your work: a PO, a person, a question"
            className="w-full bg-transparent text-[13px] outline-none"
            aria-label="Search your work log"
          />
          {(q || query) && (
            <button
              type="button"
              onClick={() => {
                setQ("");
                setQuery("");
              }}
              aria-label="Clear search"
              className="inline-flex size-5 shrink-0 cursor-pointer items-center justify-center rounded text-muted transition-colors hover:bg-hover hover:text-fg"
            >
              <X className="size-3.5" aria-hidden />
            </button>
          )}
        </label>
        <div className="flex flex-wrap gap-1" role="group" aria-label="Show">
          {[undefined, ...KINDS].map((k) => (
            <button
              key={k ?? "all"}
              type="button"
              aria-pressed={kind === k}
              onClick={() => setKind(k)}
              className={cn("h-7 rounded-md px-2 text-[12px] transition-colors cursor-pointer", kind === k ? "bg-[color-mix(in_srgb,var(--accent)_14%,transparent)] font-medium text-fg" : "text-muted hover:bg-hover hover:text-fg")}
            >
              {k ? KIND_LABEL[k] : "All"}
              {k && counts ? <span className={cn("ml-1 tabular-nums", counts[k] ? "text-muted" : "text-faint")}>{counts[k] ?? 0}</span> : null}
            </button>
          ))}
        </div>
        {record && (
          <button type="button" onClick={() => router.push("/work-log")} className="h-7 rounded-md px-2 text-[12px] text-muted transition-colors hover:bg-hover hover:text-fg cursor-pointer">
            On one record only: show all
          </button>
        )}
      </form>}

      {closedLog ? (
        <p className="px-6 py-10 text-[13px] text-muted">Your work log can&apos;t be shown right now. Nothing is lost; try again in a few minutes.</p>
      ) : main.error ? (
        <div className="p-5">
          <LoadProblem message={main.error} onRetry={main.load} />
        </div>
      ) : !main.page ? (
        <Pending label="Reading your work log" className="px-6 pt-3" />
      ) : (
        <div className="pb-8" data-testid="work-log">
          {!record && !query && !kind && <TimeSavedPanel everyone={everyone} />}
          {!everyone && !record && !query && !kind && <AutopilotScorecardPanel />}
          {!everyone && !record && !query && !kind && open.length > 0 && (
            <section aria-label="Waiting for a reply" className="pt-3">
              <h2 className="px-6 pb-1.5 text-[12px] font-medium text-muted">
                Waiting for a reply <span className="font-normal tabular-nums text-faint">{open.length}</span>
              </h2>
              <ul className="px-3">
                {open.map((e) => (
                  <WorkLogRow key={e.seq} e={e} onChanged={refresh} />
                ))}
              </ul>
            </section>
          )}
          {!everyone && !record && !query && !kind && pins.length > 0 && (
            <section aria-label="Pinned" className="pt-3">
              <h2 className="px-6 pb-1.5 text-[12px] font-medium text-muted">Pinned</h2>
              <ul className="px-3">
                {pins.map((e) => (
                  <WorkLogRow key={e.seq} e={e} onChanged={refresh} />
                ))}
              </ul>
            </section>
          )}
          {days.length === 0 ? (
            <div className="flex flex-wrap items-center gap-3 px-6 py-10 text-[13px] text-muted">
              <p>{query || kind || record ? "Nothing in your work log matches." : "Nothing yet. What you ask, read, approve, send, note, save and watch shows up here."}</p>
              {(query || kind || record) && (
                <button
                  type="button"
                  onClick={() => {
                    setQ("");
                    setQuery("");
                    setKind(undefined);
                    if (record) router.push("/work-log");
                  }}
                  className="cursor-pointer rounded-md border border-border bg-bg px-2.5 py-1 text-[12px] font-medium text-fg transition-colors hover:border-border-strong hover:bg-hover"
                >
                  Clear filters
                </button>
              )}
            </div>
          ) : (
            days.map((d) => (
              <section key={d.key} aria-label={d.label}>
                <div className="sticky top-12 z-10 flex h-9 items-end bg-list px-6 pb-1.5">
                  <h2 className="text-[12px] font-medium text-muted">
                    {d.label} <span className="font-normal tabular-nums text-faint">{d.rows.length}</span>
                  </h2>
                </div>
                <ul className="px-3">
                  {d.rows.map((e) => (
                    <WorkLogRow key={e.seq} e={e} onChanged={refresh} showWho={everyone} />
                  ))}
                </ul>
              </section>
            ))
          )}
        </div>
      )}
    </div>
  );
}

export default function WorkLogPage() {
  return (
    <Suspense>
      <WorkLog />
    </Suspense>
  );
}
