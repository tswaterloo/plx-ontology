"use client";

import { Check, Clock3, ListChecks, Mail, MessageCircle, RefreshCw, Search, X, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { StatusWord } from "@/components/ui/status-badge";
import { Toolbar, ToolbarButton } from "@/components/ui/toolbar";
import { PageIntro } from "@/components/ui/page-intro";
import { SectionTabs } from "@/components/shell";
import { actionChip, realName } from "@/lib/actions";
import { cn } from "@/lib/cn";
import { getActionsLog } from "@/lib/api";
import { useRead } from "@/lib/use-read";
import { StoredAge } from "@/components/ui/stored-age";
import { plainError } from "@/lib/errors";
import { clockET, formatDateTime, plural } from "@/lib/format";
import type { ActionLogRow, ActionsLog } from "@/lib/types";

const TZ = "America/Toronto";

const KIND: Record<string, string> = {
  send_email: "Email",
  reply_in_thread: "Reply",
  forward: "Forward",
  teams_message: "Teams message",
  create_follow_up: "Follow-up",
  mark_done: "Marked done",
  snooze: "Snoozed",
  dismiss: "Dismissed",
  assign: "Assigned",
};

/** A task by a plain line glyph. */
const TASK: Record<string, LucideIcon> = { create_follow_up: ListChecks, mark_done: Check, snooze: Clock3, dismiss: X };

function KindTile({ kind }: { kind: string }) {
  const Icon = kind === "teams_message" ? MessageCircle : (TASK[kind] ?? Mail);
  // the source's own colour: Teams and Mail by their ink, the Workbench's own tasks in the accent
  const ink = kind === "teams_message" ? "var(--sys-teams-ink)" : TASK[kind] ? "var(--accent)" : "var(--sys-mail-ink)";
  return (
    <span className="flex size-8 shrink-0 items-center" style={{ color: ink }} aria-hidden>
      <Icon className="size-4" strokeWidth={1.8} />
    </span>
  );
}

const dayKey = (iso: string) => new Date(iso).toLocaleDateString("en-CA", { timeZone: TZ });

function dayLabel(iso: string, now = new Date()): string {
  const k = dayKey(iso);
  if (k === dayKey(now.toISOString())) return "Today";
  if (k === dayKey(new Date(now.getTime() - 86_400_000).toISOString())) return "Yesterday";
  const d = new Date(iso);
  const sameYear = d.toLocaleDateString("en-CA", { year: "numeric", timeZone: TZ }) === now.toLocaleDateString("en-CA", { year: "numeric", timeZone: TZ });
  return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", ...(sameYear ? {} : { year: "numeric" }), timeZone: TZ }).replace("Sept", "Sep");
}

const timeOf = (iso: string) => clockET(new Date(iso));

function Row({ r }: { r: ActionLogRow }) {
  const chip = actionChip(r, false);
  const mail = !!(r.to?.length || r.subject);
  const subject = realName(r.subject);
  const label = realName(r.card_label) ?? (r.card_id.startsWith("ask:") ? "From Ask" : null);
  const titleText = subject || (mail ? (r.to?.length ? `Email to ${r.to[0]}` : "Email with no subject") : `${KIND[r.kind] ?? r.kind}${label ? `: ${label}` : ""}`);
  const title = subject || (mail ? (r.to?.length ? `Email to ${r.to[0]}` : "Email with no subject") : null) || (
    <>
      {KIND[r.kind] ?? r.kind}
      {label && <span className="lg:hidden">: {label}</span>}
    </>
  );
  const to = (r.to ?? []).join(", ");
  const cc = (r.cc ?? []).join(", ");
  const line = mail ? [to ? `To ${to}` : "No recipient", cc ? `cc ${cc}` : ""].filter(Boolean).join(" · ") : r.text;
  const cites = (r.citations ?? []).length;
  return (
    <li
      className="grid grid-cols-[44px_minmax(0,1fr)_auto] items-start gap-x-3 rounded-md px-3 py-2.5 transition-colors hover:bg-hover lg:grid-cols-[44px_minmax(0,1fr)_minmax(0,240px)_184px_80px] lg:items-center"
      data-testid="actions-log-row"
      title={`${formatDateTime(r.at)}, by ${r.actor}`}
    >
      <KindTile kind={r.kind} />
      <div className="min-w-0">
        <div className="truncate text-[13px] leading-[18px] font-semibold" title={titleText}>{title}</div>
        <div className="mt-0.5 truncate text-[12px] leading-4 text-muted" title={line || undefined}>
          {line}
          {label && mail && <span className="lg:hidden"> · {label}</span>}
        </div>
      </div>
      <div className="hidden min-w-0 lg:block">
        <div className="truncate text-[13px] leading-[18px]" title={label || "Follow-up"}>{label || "Follow-up"}</div>
        <div className="mt-0.5 truncate text-[12px] leading-4 text-muted" title={r.actor}>
          {r.actor}
          {cites > 0 && <span className="text-faint"> · {plural(cites, "cited fact")}</span>}
          {r.replayed && <span className="text-faint"> · repeat of an earlier confirm</span>}
        </div>
      </div>
      <div className="hidden min-w-0 lg:block">{chip && <StatusWord shown={chip} title={chip.title ?? r.text} className="text-[12px]" />}</div>
      <span className="self-start pt-px text-right text-[12px] leading-4 tabular-nums text-muted lg:self-center lg:pt-0">{timeOf(r.at)}</span>
      {chip && (
        <div className="col-start-2 col-end-4 mt-1 lg:hidden">
          <StatusWord shown={chip} title={chip.title ?? r.text} className="text-[12px]" />
        </div>
      )}
    </li>
  );
}

function Empty() {
  return (
    <div className="max-w-xl px-6 py-8" data-testid="activity-empty">
      <p className="text-[13px] font-medium">Nothing sent, approved or logged yet.</p>
      <p className="mt-1 text-[13px] leading-5 text-muted">
        Each message you send, task you set and item you put away from Home, a record page or Ask shows here, with whom it went to, the record it was about and whether it went out.
      </p>
      <Link href="/" className="mt-3 inline-block text-[13px] font-medium text-accent hover:underline">
        Open Home
      </Link>
    </div>
  );
}

const WEEK_MS = 7 * 86_400_000;
const TASKS = new Set(["create_follow_up", "mark_done", "assign"]);
const OUTBOUND = new Set(["send_email", "reply_in_thread", "forward", "teams_message"]);

/** The last seven days in counts, and the actions whose outcome needs a look. */
function weekOf(rows: ActionLogRow[], now = Date.now()) {
  const week = rows.filter((r) => now - Date.parse(r.at) <= WEEK_MS);
  const out = week.filter((r) => OUTBOUND.has(r.kind));
  return {
    sent: out.filter((r) => r.status === "sent").length,
    notSent: out.filter((r) => r.status === "dry_run").length,
    tasks: week.filter((r) => TASKS.has(r.kind)).length,
    putAway: week.filter((r) => r.kind === "snooze" || r.kind === "dismiss").length,
    people: new Set(out.flatMap((r) => [...(r.to ?? []), ...(r.cc ?? [])].map((a) => a.toLowerCase()))).size,
    records: new Set(week.map((r) => r.card_label)).size,
    busiest: Object.entries(week.reduce<Record<string, number>>((m, r) => ((m[r.card_label || "Follow-up"] = (m[r.card_label || "Follow-up"] ?? 0) + 1), m), {}))
      .filter(([, n]) => n > 1)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 4),
  };
}

function Summary({ rows }: { rows: ActionLogRow[] }) {
  const w = weekOf(rows);
  const cells: [string, number, string?][] = [
    ["Sent", w.sent],
    ["Not sent", w.notSent],
    ["Tasks set or done", w.tasks],
    ["Put away", w.putAway],
    ["People reached", w.people],
    ["Records", w.records],
  ];
  if (cells.every(([, n]) => n === 0) && w.busiest.length === 0) return null;
  return (
    <section className="border-b border-border px-6 py-3" aria-label="Last 7 days" data-testid="activity-summary">
      <h2 className="text-[12px] font-medium text-muted">Last 7 days</h2>
      <dl className="mt-1 grid grid-cols-3 gap-x-6 gap-y-1 sm:grid-cols-6">
        {cells.filter(([, n]) => n > 0).map(([label, n]) => (
          <div key={label}>
            <dt className="text-[11px] leading-4 text-muted">{label}</dt>
            <dd className="text-[18px] font-semibold tabular-nums">{n}</dd>
          </div>
        ))}
      </dl>
      {w.busiest.length > 0 && (
        <p className="mt-2 text-[12px] text-muted" data-testid="activity-busiest">
          Most worked on:{" "}
          {w.busiest.map(([label, n], i) => (
            <span key={label}>
              {i > 0 && " · "}
              <span className="font-medium text-fg">{label}</span> {n}
            </span>
          ))}
        </p>
      )}
    </section>
  );
}

/** The journal's views: what went out, what did not, what needs a look, tasks, put away. */
const VIEWS: { key: string; label: string; test: (r: ActionLogRow) => boolean }[] = [
  { key: "all", label: "All", test: () => true },
  { key: "sent", label: "Sent", test: (r) => OUTBOUND.has(r.kind) && r.status === "sent" },
  { key: "not_sent", label: "Not sent", test: (r) => OUTBOUND.has(r.kind) && (r.status === "dry_run" || r.status === "pending") },
  { key: "check", label: "Needs a look", test: (r) => r.status === "failed" || r.status === "unknown" },
  { key: "tasks", label: "Tasks", test: (r) => TASKS.has(r.kind) },
  { key: "away", label: "Put away", test: (r) => r.kind === "snooze" || r.kind === "dismiss" },
];

/** Your actions journal: every action you confirmed, newest first (append-only on the server). */
export default function ActivityPage() {
  const { data: log, error: failed, retry: load, at } = useRead<ActionsLog>("/api/actions/log", (s) => getActionsLog(s));
  const rows: ActionLogRow[] | null = log ? (log.rows ?? []) : null;
  const error = failed ? plainError(failed) : null;

  const [view, setView] = useState("all");
  const [search, setSearch] = useState("");
  const q = search.trim().toLowerCase();
  const counts = useMemo(() => Object.fromEntries(VIEWS.map((v) => [v.key, (rows ?? []).filter(v.test).length])), [rows]);
  const days = useMemo(() => {
    const out: { key: string; label: string; rows: ActionLogRow[] }[] = [];
    const test = VIEWS.find((v) => v.key === view)?.test ?? (() => true);
    for (const r of (rows ?? []).filter(test)) {
      if (q) {
        const hay = [r.subject, r.card_label, r.actor, r.text, ...(r.to ?? []), ...(r.cc ?? []), KIND[r.kind] ?? r.kind].filter(Boolean).join(" ").toLowerCase();
        if (!hay.includes(q)) continue;
      }
      const k = dayKey(r.at);
      const last = out[out.length - 1];
      if (last?.key === k) last.rows.push(r);
      else out.push({ key: k, label: dayLabel(r.at), rows: [r] });
    }
    return out;
  }, [rows, view, q]);

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-list">
      <Toolbar
        pin="Messages and tasks"
        heading
        title={
          <>
            Messages and tasks
            {rows && rows.length > 0 && <>{" "}<span className="ml-1 text-[13px] font-normal tracking-normal num text-faint">{rows.length}</span></>}
          </>
        }
        actions={
          <ToolbarButton onClick={() => load()} title="Refresh activity" aria-label="Refresh activity">
            <RefreshCw className={cn("size-3.5", at !== null && "animate-spin")} strokeWidth={1.9} aria-hidden />
            <span className="max-sm:sr-only">Refresh</span>
          </ToolbarButton>
        }
        className="bg-list"
      />
      <SectionTabs section="work-log" />
      <PageIntro path="/activity" />
      <StoredAge at={at} className="px-6 pb-1" />
      {error ? (
        <div className="p-5">
          <LoadProblem message={error} onRetry={load} />
        </div>
      ) : !rows ? (
        <Pending page label="Reading your activity" testId="activity-pending" />
      ) : rows.length === 0 ? (
        <Empty />
      ) : (
        <div className="pb-8" data-testid="actions-log">
          <Summary rows={rows} />
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-border px-6">
            <div className="flex h-9 items-stretch gap-5 overflow-x-auto [scrollbar-width:none]" role="tablist" aria-label="Show">
              {VIEWS.filter((v) => v.key === "all" || counts[v.key] > 0).map((v) => (
                <button
                  key={v.key}
                  type="button"
                  role="tab"
                  aria-selected={view === v.key}
                  onClick={() => setView(v.key)}
                  className={cn(
                    "relative flex shrink-0 items-center gap-1.5 text-[13px] whitespace-nowrap transition-colors cursor-pointer",
                    view === v.key ? "font-medium text-fg after:absolute after:inset-x-0 after:bottom-0 after:h-[2px] after:bg-accent" : "text-muted hover:text-fg",
                  )}
                >
                  {v.label}
                  <span className="num text-[12px] font-normal text-faint">{counts[v.key]}</span>
                </button>
              ))}
            </div>
            <div className="relative my-1.5 w-full max-w-[240px]">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-faint" aria-hidden />
              <input
                type="search"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape" && search) {
                    e.preventDefault();
                    setSearch("");
                  }
                }}
                placeholder="Filter activity"
                aria-label="Filter activity"
                className="h-7 w-full rounded-md border border-border bg-bg pl-7.5 pr-7 text-[12px] text-fg outline-none transition-colors hover:border-border-strong focus-visible:border-accent placeholder:text-faint dark:bg-surface"
              />
              {search && (
                <button
                  type="button"
                  onClick={() => setSearch("")}
                  aria-label="Clear filter"
                  className="absolute right-1 top-1/2 inline-flex size-5 -translate-y-1/2 cursor-pointer items-center justify-center rounded text-muted hover:bg-hover hover:text-fg"
                >
                  <X className="size-3" aria-hidden />
                </button>
              )}
            </div>
          </div>
          <div className="hidden h-8 grid-cols-[44px_minmax(0,1fr)_minmax(0,240px)_184px_80px] items-end gap-x-3 px-6 pb-1 text-[11px] leading-4 text-faint lg:grid" aria-hidden>
            <span />
            <span>Action</span>
            <span>Record</span>
            <span>Result</span>
            <span className="text-right">Time</span>
          </div>
          {days.length === 0 ? (
            <div className="flex flex-wrap items-center gap-3 px-6 py-8 text-[13px] text-muted">
              <span>No activity matches &ldquo;{search.trim()}&rdquo;.</span>
              <button
                type="button"
                onClick={() => {
                  setSearch("");
                  setView("all");
                }}
                className="cursor-pointer rounded border border-border bg-bg px-2 py-0.5 text-[12px] font-medium text-fg hover:border-border-strong hover:bg-hover dark:bg-surface"
              >
                Clear filters
              </button>
            </div>
          ) : (
            days.map((d) => (
              <section key={d.key} aria-label={d.label}>
                <div className="sticky top-12 z-10 flex h-9 items-end bg-list px-6 pb-1.5">
                  <h2 className="text-[12px] font-medium text-muted">
                    {d.label} <span className="font-normal num text-faint">{d.rows.length}</span>
                  </h2>
                </div>
                <ul className="px-3">
                  {d.rows.map((r) => (
                    <Row key={r.seq} r={r} />
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
