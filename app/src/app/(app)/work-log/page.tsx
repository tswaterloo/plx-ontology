"use client";

import { Search } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { LoadProblem } from "@/components/ui/notice";
import { SlowNote } from "@/components/ui/skeleton";
import { Toolbar, ToolbarButton } from "@/components/ui/toolbar";
import { KIND_LABEL, WorkLogRow } from "@/components/work-log";
import { getWorkLog, type WorkLogFilters } from "@/lib/api";
import { cn } from "@/lib/cn";
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
  const [page, setPage] = useState<WorkLogPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const key = JSON.stringify(filters);
  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    getWorkLog(JSON.parse(key) as WorkLogFilters, c.signal)
      .then(setPage)
      .catch((e) => !c.signal.aborted && setError(plainError(e)));
    return () => c.abort();
  }, [key]);
  useEffect(() => load(), [load]);
  return { page, error, load };
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
  const [owner, setOwner] = useState(false);
  const main = useLog({ q: query || undefined, kind, record, everyone, limit: 200 });
  const waiting = useLog({ waiting: true, limit: 20 });
  const pinned = useLog({ pinned: true, limit: 20 });

  // the owner may read everyone's log (the server decides; anyone else is refused)
  useEffect(() => {
    const c = new AbortController();
    getWorkLog({ everyone: true, limit: 1 }, c.signal)
      .then(() => setOwner(true))
      .catch(() => setOwner(false));
    return () => c.abort();
  }, []);

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

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-list">
      <Toolbar
        heading
        title={
          <>
            Work log
            {main.page?.total ? <span className="ml-2 text-[13px] font-normal tracking-normal tabular-nums text-faint"><span className="sr-only">, </span>{main.page.total}</span> : null}
          </>
        }
        actions={
          owner ? (
            <ToolbarButton aria-pressed={everyone} onClick={() => setEveryone((v) => !v)} className={cn(everyone && "text-fg")}>
              {everyone ? "Everyone" : "Only mine"}
            </ToolbarButton>
          ) : undefined
        }
        className="bg-list"
      />
      <form
        className="flex flex-wrap items-center gap-2 px-6 pt-3"
        role="search"
        onSubmit={(ev) => {
          ev.preventDefault();
          setQuery(q.trim());
        }}
      >
        <label className="flex h-8 min-w-[240px] flex-1 items-center gap-2 rounded-lg bg-bg px-2.5 shadow-lift">
          <Search className="size-[15px] text-muted" aria-hidden />
          <input value={q} onChange={(ev) => setQ(ev.target.value)} placeholder="Search your work: a PO, a person, a question" className="w-full bg-transparent text-[13px] outline-none" aria-label="Search your work log" />
        </label>
        <div className="flex flex-wrap gap-1" role="group" aria-label="Show">
          {[undefined, ...KINDS].map((k) => (
            <button
              key={k ?? "all"}
              type="button"
              aria-pressed={kind === k}
              onClick={() => setKind(k)}
              className={cn("h-7 rounded-md px-2 text-[12px] cursor-pointer", kind === k ? "bg-[color-mix(in_srgb,var(--accent)_14%,transparent)] font-medium text-fg" : "text-muted hover:bg-hover hover:text-fg")}
            >
              {k ? KIND_LABEL[k] : "All"}
            </button>
          ))}
        </div>
        {record && (
          <button type="button" onClick={() => router.push("/work-log")} className="h-7 rounded-md px-2 text-[12px] text-muted hover:bg-hover hover:text-fg cursor-pointer">
            On one record only: show all
          </button>
        )}
      </form>

      {closedLog ? (
        <p className="px-6 py-10 text-[13px] text-muted">Your work log can&apos;t be shown right now: the list of people kept out of the Workbench isn&apos;t loaded, so nothing is shown or kept until it is.</p>
      ) : main.error ? (
        <div className="p-5">
          <LoadProblem message={main.error} onRetry={main.load} />
        </div>
      ) : !main.page ? (
        <SlowNote className="px-6 pt-3" />
      ) : (
        <div className="pb-8" data-testid="work-log">
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
            <p className="px-6 py-10 text-[13px] text-muted">{query || kind || record ? "Nothing in your work log matches." : "Nothing yet. What you ask, read, approve, send, note, save and watch shows up here."}</p>
          ) : (
            days.map((d) => (
              <section key={d.key} aria-label={d.label}>
                <div className="sticky top-[52px] z-10 flex h-9 items-end bg-list px-6 pb-1.5">
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
