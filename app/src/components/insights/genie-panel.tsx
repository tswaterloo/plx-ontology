"use client";

import {
  ArrowUpRight,
  Bell,
  Clock,
  ExternalLink,
  FileSpreadsheet,
  FileText,
  Mail,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { getAllFeedback, getFeedback, setFeedbackStatus } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import type { FeedbackEntry, FeedbackStatus, InsightInfo, InsightList } from "@/lib/types";

export interface SavedAnswerPageSummary {
  page_id: string;
  title: string;
  summary?: string | null;
  question?: string | null;
  created_at?: string | null;
  tile_count?: number;
}

/**
 * Proactive Monitors & Saved Executive Briefs section (legacy export retained for compatibility).
 */
export function MonitorsAndBriefsPanel({
  list,
  monitorLine,
  onJumpToTile,
}: {
  list: InsightList;
  monitorLine: (item: InsightInfo) => string;
  onJumpToTile: (id: string) => void;
}) {
  const [pages, setPages] = useState<SavedAnswerPageSummary[] | null>(null);
  const [pagesError, setPagesError] = useState<string | null>(null);
  const [watchPrompt, setWatchPrompt] = useState("");

  const activeMonitors = list.items.filter((i) => i.saved && i.schedule);
  const pinnedCharts = list.items.filter((i) => i.saved);

  useEffect(() => {
    const ctl = new AbortController();
    fetch("/api/insights/pages", { signal: ctl.signal, credentials: "same-origin" })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = (await r.json()) as { pages?: SavedAnswerPageSummary[] };
        if (!ctl.signal.aborted) setPages(d.pages ?? []);
      })
      .catch((e) => {
        if (!ctl.signal.aborted) setPagesError(plainError(e));
      });
    return () => ctl.abort();
  }, []);

  const deletePage = async (pageId: string) => {
    try {
      const r = await fetch(`/api/insights/pages/${encodeURIComponent(pageId)}`, {
        method: "DELETE",
        credentials: "same-origin",
      });
      if (r.ok) {
        setPages((prev) => (prev ?? []).filter((p) => p.page_id !== pageId));
      }
    } catch {
      // ignore transient error
    }
  };

  const trimmedWatch = watchPrompt.trim();

  return (
    <div className="space-y-8" data-testid="monitors-tab-panel">
      <section className="space-y-4" aria-label="Proactive monitors">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-[15px] font-semibold tracking-[-0.015em] text-fg">
              Scheduled alerts &amp; watches
            </h2>
            <p className="text-[12.5px] text-muted">
              Pinned charts that run on a schedule as you and surface threshold crossings on Home.
            </p>
          </div>
          <span className="text-[12px] text-muted tabular-nums">
            {activeMonitors.length} active · {pinnedCharts.length} saved
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-bg p-2 focus-within:border-border-strong">
          <Bell className="ml-1 size-4 shrink-0 text-faint" strokeWidth={1.8} aria-hidden />
          <input
            type="text"
            value={watchPrompt}
            onChange={(e) => setWatchPrompt(e.target.value)}
            placeholder="Tell me when USD monthly revenue drops under 400k, or late POs exceed 15"
            aria-label="Create proactive monitor"
            className="min-w-[220px] flex-1 bg-transparent text-[13px] text-fg outline-none placeholder:text-faint"
          />
          {trimmedWatch.length > 2 ? (
            <Link
              prefetch={false}
              href={`/ask?q=${encodeURIComponent(`Tell me when ${trimmedWatch}`)}`}
              className="inline-flex h-7 items-center gap-1 rounded-md bg-fg px-3 text-[12px] font-medium text-bg hover:opacity-90"
            >
              Set up in Ask
              <ArrowUpRight className="size-3" strokeWidth={2} aria-hidden />
            </Link>
          ) : (
            <span className="inline-flex h-7 items-center rounded-md border border-border px-3 text-[12px] font-medium text-faint">
              Set up in Ask
            </span>
          )}
        </div>

        {activeMonitors.length === 0 ? (
          <p className="text-[13px] text-muted">
            No scheduled monitors active yet. Pin any chart from Ask, then click Schedule on the tile to run it hourly, daily, or on a numeric threshold.
          </p>
        ) : (
          <div className="overflow-x-auto border-t border-border/80">
            <table className="w-full border-collapse text-left text-[12.5px]">
              <thead className="border-b border-border text-[11.5px] text-muted">
                <tr>
                  <th className="py-2 pr-3 font-medium">Monitor</th>
                  <th className="px-3 py-2 font-medium">Rule</th>
                  <th className="px-3 py-2 font-medium">Last run</th>
                  <th className="py-2 pl-3 text-right font-medium">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/60">
                {activeMonitors.map((it) => (
                  <tr key={it.id} className="hover:bg-hover/40">
                    <td className="py-2.5 pr-3">
                      <button
                        type="button"
                        onClick={() => onJumpToTile(it.id)}
                        className="inline-flex items-center gap-2 font-medium text-fg hover:underline cursor-pointer"
                      >
                        <Clock className="size-3.5 shrink-0 text-faint" strokeWidth={1.8} aria-hidden />
                        <span>{it.title}</span>
                      </button>
                    </td>
                    <td className="px-3 py-2.5 text-muted">{monitorLine(it)}</td>
                    <td className="px-3 py-2.5 text-[12px] text-faint tabular-nums">
                      {it.last_run ? formatDateTime(it.last_run) : "Pending next run"}
                    </td>
                    <td className="py-2.5 pl-3 text-right">
                      <button
                        type="button"
                        onClick={() => onJumpToTile(it.id)}
                        className="inline-flex h-7 items-center rounded-md border border-border bg-bg px-2.5 text-[12px] font-medium text-fg hover:border-border-strong hover:bg-hover cursor-pointer"
                      >
                        Open tile
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="grid grid-cols-1 gap-8 border-t border-border pt-6 lg:grid-cols-12">
        <section className="space-y-3 lg:col-span-7" aria-label="Saved report pages">
          <div className="flex items-center justify-between gap-2">
            <div>
              <h3 className="text-[14px] font-semibold text-fg">Saved executive brief pages</h3>
              <p className="text-[12px] text-muted">Multi-chart Ask answers saved with governed SQL provenance.</p>
            </div>
            <Link
              prefetch={false}
              href="/ask"
              className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-bg px-2.5 text-[12px] font-medium text-fg hover:border-border-strong hover:bg-hover"
            >
              New brief in Ask
            </Link>
          </div>
          {pages === null && !pagesError ? (
            <p className="text-[12.5px] text-muted">Loading saved reports</p>
          ) : pagesError ? (
            <p className="text-[12.5px] text-muted">Saved report pages will appear here once created from Ask.</p>
          ) : pages && pages.length === 0 ? (
            <p className="text-[12.5px] text-muted">
              No saved report pages yet. Run a multi-step investigation in Ask and click Save as report to keep its live charts and citations here.
            </p>
          ) : (
            <ul className="divide-y divide-border/70 border-t border-border/70">
              {(pages ?? []).map((p) => (
                <li key={p.page_id} className="flex items-start justify-between gap-3 py-2.5">
                  <div className="min-w-0 flex-1 space-y-0.5">
                    <div className="flex items-center gap-2">
                      <FileText className="size-3.5 shrink-0 text-faint" strokeWidth={1.8} aria-hidden />
                      <span className="truncate text-[13px] font-medium text-fg">{p.title}</span>
                    </div>
                    {p.summary && <p className="pl-5 text-[12px] leading-4 text-muted">{p.summary}</p>}
                    <p className="pl-5 text-[11.5px] text-faint tabular-nums">
                      {p.tile_count ? `${p.tile_count} charts` : "Report"}
                      {p.created_at ? ` · ${formatDateTime(p.created_at)}` : ""}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => void deletePage(p.page_id)}
                    aria-label={`Delete report ${p.title}`}
                    className="inline-flex size-7 items-center justify-center rounded-md text-muted hover:bg-hover hover:text-late cursor-pointer"
                  >
                    <Trash2 className="size-3.5" strokeWidth={2} aria-hidden />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="space-y-3 lg:col-span-5" aria-label="Briefs and exports">
          <h3 className="text-[14px] font-semibold text-fg">Brief delivery &amp; exports</h3>
          <p className="text-[12.5px] leading-5 text-muted">
            Export live tables to Excel or generate cited executive decks and confirmation-gated email briefs.
          </p>
          <div className="space-y-2 pt-1">
            <Link
              prefetch={false}
              href="/artifacts"
              className="flex items-center justify-between gap-3 rounded-lg border border-border/80 bg-surface/50 px-3 py-2.5 text-[13px] text-fg transition-colors hover:border-border-strong hover:bg-hover"
            >
              <span className="flex items-center gap-2 font-medium">
                <FileSpreadsheet className="size-4 text-muted" strokeWidth={1.8} aria-hidden />
                Documents, Slides &amp; Excel Studio
              </span>
              <ExternalLink className="size-3.5 text-faint" aria-hidden />
            </Link>
            <Link
              prefetch={false}
              href="/ask?q=Prepare%20a%20weekly%20executive%20brief%20with%20revenue%2C%20open%20orders%2C%20and%20late%20POs"
              className="flex items-center justify-between gap-3 rounded-lg border border-border/80 bg-surface/50 px-3 py-2.5 text-[13px] text-fg transition-colors hover:border-border-strong hover:bg-hover"
            >
              <span className="flex items-center gap-2 font-medium">
                <Mail className="size-4 text-muted" strokeWidth={1.8} aria-hidden />
                Draft weekly email brief (confirm before send)
              </span>
              <ExternalLink className="size-3.5 text-faint" aria-hidden />
            </Link>
          </div>
        </section>
      </div>
    </div>
  );
}

/**
 * Flush hairline Feedback & Fix Proposals triage queue surfaced inside Data Gaps & Feedback.
 */
export function FeedbackTriagePanel({
  onWaitingCount,
}: {
  onWaitingCount?: (count: number) => void;
}) {
  const [entries, setEntries] = useState<FeedbackEntry[] | null>(null);
  const [allowed, setAllowed] = useState<boolean | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    (async () => {
      try {
        const first = await getFeedback({ kind: "answer", limit: 1 }, ctl.signal);
        if (!first.all_readers) {
          setAllowed(false);
          return;
        }
        setAllowed(true);
        const all = await getAllFeedback({ kind: "answer" }, ctl.signal);
        if (!ctl.signal.aborted) {
          setEntries(all.entries);
          const waiting = all.entries.filter((e) => e.rating === -1 && e.status === "new").length;
          onWaitingCount?.(waiting);
        }
      } catch (e) {
        if (!ctl.signal.aborted) setProblem(plainError(e));
      }
    })();
    return () => ctl.abort();
  }, [onWaitingCount]);

  const triage = async (entry: FeedbackEntry, status: FeedbackStatus) => {
    setBusyId(entry.id);
    setProblem(null);
    try {
      const got = await setFeedbackStatus(entry.id, status);
      setEntries((prev) => {
        const next = (prev ?? []).map((x) => (x.id === entry.id ? { ...x, status: got.status } : x));
        onWaitingCount?.(next.filter((e) => e.rating === -1 && e.status === "new").length);
        return next;
      });
    } catch (e) {
      setProblem(plainError(e));
    } finally {
      setBusyId(null);
    }
  };

  if (allowed === false) return null;

  const up = (entries ?? []).filter((e) => e.rating === 1).length;
  const down = (entries ?? []).filter((e) => e.rating === -1).length;
  const waiting = (entries ?? []).filter((e) => e.rating === -1 && e.status === "new");

  return (
    <section
      className="space-y-3 border-t border-border pt-6"
      aria-label="Answer feedback and fix proposals"
      data-testid="genie-feedback-triage"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h3 className="text-[14px] font-semibold tracking-[-0.01em] text-fg">Feedback triage</h3>
          <p className="text-[12.5px] text-muted">
            Flagged answers and review requests queued for measure, column, or prompt fixes.
          </p>
        </div>
        {entries && (
          <span className="text-[12px] text-muted tabular-nums">
            {up} helpful · {down} flagged · {waiting.length} waiting
          </span>
        )}
      </div>

      {problem && <p className="text-[12.5px] text-late" role="alert">{problem}</p>}
      {entries === null && !problem && <p className="text-[12.5px] text-muted">Reading answer feedback</p>}

      {entries && waiting.length === 0 && (
        <p className="py-2 text-[12.5px] text-muted">
          No flagged answers waiting for review right now.
        </p>
      )}

      {waiting.length > 0 && (
        <ul className="divide-y divide-border/70 border-y border-border/80">
          {waiting.slice(0, 12).map((e) => {
            const busy = busyId === e.id;
            return (
              <li key={e.id} className="space-y-1.5 py-3 text-[12.5px]">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <p className="font-medium text-fg">
                    {e.question || "Rated answer"}
                    {e.review_requested && (
                      <span className="ml-2 text-[11px] font-normal text-faint">
                        · Review requested
                      </span>
                    )}
                  </p>
                  <span className="text-[11.5px] text-faint tabular-nums">
                    {formatDateTime(e.at)}
                    {e.reader_name ? ` · ${e.reader_name}` : ""}
                  </span>
                </div>
                {e.note && <p className="text-muted">Note: {e.note}</p>}
                {e.expected && <p className="text-muted">Expected: {e.expected}</p>}
                <div className="flex flex-wrap items-center gap-2 pt-1">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void triage(e, "accepted")}
                    className={cn("h-6 rounded-md bg-fg px-2.5 text-[11.5px] font-medium text-bg hover:opacity-90 disabled:opacity-50 cursor-pointer")}
                  >
                    Accept fix
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void triage(e, "fixed")}
                    className="h-6 rounded-md border border-border bg-bg px-2.5 text-[11.5px] font-medium text-fg hover:border-border-strong hover:bg-hover disabled:opacity-50 cursor-pointer"
                  >
                    Already fixed
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void triage(e, "wont_fix")}
                    className="h-6 rounded-md border border-border bg-bg px-2.5 text-[11.5px] font-medium text-muted hover:border-border-strong hover:bg-hover hover:text-fg disabled:opacity-50 cursor-pointer"
                  >
                    Won&apos;t fix
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
