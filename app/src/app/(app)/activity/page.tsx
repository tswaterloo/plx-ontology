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

const TASK: Record<string, LucideIcon> = { create_follow_up: ListChecks, mark_done: Check, snooze: Clock3, dismiss: X };

function KindIcon({ kind }: { kind: string }) {
  const Icon = kind === "teams_message" ? MessageCircle : (TASK[kind] ?? Mail);
  const ink = kind === "teams_message" ? "var(--sys-teams-ink)" : TASK[kind] ? "var(--fg-muted)" : "var(--sys-mail-ink)";
  return (
    <span className="flex size-7 shrink-0 items-center justify-center" style={{ color: ink }} aria-hidden>
      <Icon className="size-4" strokeWidth={1.75} />
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
      className="grid grid-cols-[32px_minmax(0,1fr)_auto] items-start gap-x-3 px-4 py-3 transition-colors duration-150 hover:bg-row-on lg:grid-cols-[32px_minmax(0,1fr)_minmax(0,240px)_184px_80px] lg:items-center"
      data-testid="actions-log-row"
      title={`${formatDateTime(r.at)}, by ${r.actor}`}
    >
      <KindIcon kind={r.kind} />
      <div className="min-w-0">
        <div className="truncate text-[14px] leading-5 font-medium text-fg" title={titleText}>{title}</div>
        <div className="mt-0.5 truncate text-[12.5px] leading-[18px] text-muted" title={line || undefined}>
          {line}
          {label && mail && <span className="lg:hidden"> · {label}</span>}
        </div>
      </div>
      <div className="hidden min-w-0 lg:block">
        <div className="truncate text-[13.5px] leading-5 text-fg" title={label || "Follow-up"}>{label || "Follow-up"}</div>
        <div className="mt-0.5 truncate text-[12.5px] leading-[18px] text-muted" title={r.actor}>
          {r.actor}
          {cites > 0 && <span> · {plural(cites, "cited fact")}</span>}
          {r.replayed && <span> · repeat of an earlier confirm</span>}
        </div>
      </div>
      <div className="hidden min-w-0 lg:block">{chip && <StatusWord shown={chip} title={chip.title ?? r.text} className="text-[12.5px]" />}</div>
      <span className="self-start pt-0.5 text-right text-[12.5px] leading-4 tabular-nums text-muted lg:self-center lg:pt-0">{timeOf(r.at)}</span>
      {chip && (
        <div className="col-start-2 col-end-4 mt-1.5 lg:hidden">
          <StatusWord shown={chip} title={chip.title ?? r.text} className="text-[12.5px]" />
        </div>
      )}
    </li>
  );
}

function Empty() {
  return (
    <div className="mx-auto w-full max-w-[1360px] px-4 py-10 sm:px-6" data-testid="activity-empty">
      <div className="max-w-lg space-y-2">
        <p className="text-[14px] font-medium text-fg">Nothing sent, approved or logged yet.</p>
        <p className="text-[13px] leading-5 text-muted">
          Each message you send, task you set and item you put away from Home, a record page or Ask shows here, with whom it went to, the record it was about and whether it went out.
        </p>
        <Link
          href="/"
          className="mt-3 inline-flex h-8 items-center rounded-md bg-fg px-3 text-[12.5px] font-medium text-bg transition-[opacity,transform] duration-150 hover:opacity-90 active:scale-[0.98]"
        >
          Open Home
        </Link>
      </div>
    </div>
  );
}

const WEEK_MS = 7 * 86_400_000;
const TASKS = new Set(["create_follow_up", "mark_done", "assign"]);
const OUTBOUND = new Set(["send_email", "reply_in_thread", "forward", "teams_message"]);

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
  const cells: [string, number][] = [
    ["Sent", w.sent],
    ["Not sent", w.notSent],
    ["Tasks set or done", w.tasks],
    ["Put away", w.putAway],
    ["People reached", w.people],
    ["Records", w.records],
  ];
  const shown = cells.filter(([, n]) => n > 0);
  if (shown.length === 0 && w.busiest.length === 0) return null;
  return (
    <section className="space-y-3" aria-label="Last 7 days" data-testid="activity-summary">
      <h2 className="text-[12.5px] font-medium text-muted">Last 7 days</h2>
      {shown.length > 0 && (
        <dl className="grid grid-cols-2 divide-y divide-border border-y border-border sm:grid-cols-3 lg:grid-cols-6 lg:divide-x lg:divide-y-0">
          {shown.map(([label, n], idx) => (
            <div
              key={label}
              className={cn(
                "py-3.5 tabular-nums",
                idx === 0 ? "pr-4 lg:pr-5" : "lg:px-5",
                idx === shown.length - 1 && "lg:pr-0",
              )}
            >
              <dt className="truncate text-[12px] font-medium leading-4 text-muted">{label}</dt>
              <dd className="mt-1 text-[24px] leading-7 font-semibold tracking-[-0.02em] text-fg">{n}</dd>
            </div>
          ))}
        </dl>
      )}
      {w.busiest.length > 0 && (
        <p className="text-[12.5px] text-muted" data-testid="activity-busiest">
          Most worked on:{" "}
          {w.busiest.map(([label, n], i) => (
            <span key={label}>
              {i > 0 && " · "}
              <span className="font-medium text-fg">{label}</span>{" "}
              <span className="num text-muted">{n}</span>
            </span>
          ))}
        </p>
      )}
    </section>
  );
}

const VIEWS: { key: string; label: string; test: (r: ActionLogRow) => boolean }[] = [
  { key: "all", label: "All", test: () => true },
  { key: "sent", label: "Sent", test: (r) => OUTBOUND.has(r.kind) && r.status === "sent" },
  { key: "not_sent", label: "Not sent", test: (r) => OUTBOUND.has(r.kind) && (r.status === "dry_run" || r.status === "pending") },
  { key: "check", label: "Needs a look", test: (r) => r.status === "failed" || r.status === "unknown" },
  { key: "tasks", label: "Tasks", test: (r) => TASKS.has(r.kind) },
  { key: "away", label: "Put away", test: (r) => r.kind === "snooze" || r.kind === "dismiss" },
];

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
    <div className="flex min-h-0 flex-1 flex-col bg-bg">
      <Toolbar
        pin="Messages and tasks"
        heading
        title={
          <>
            Messages and tasks
            {rows && rows.length > 0 && (
              <>
                {" "}
                <span className="ml-1 text-[13px] font-normal tracking-normal num text-muted">
                  {rows.length}
                </span>
              </>
            )}
          </>
        }
        actions={
          <ToolbarButton onClick={() => load()} title="Refresh activity" aria-label="Refresh activity">
            <RefreshCw className={cn("size-3.5", at !== null && "animate-spin")} strokeWidth={1.9} aria-hidden />
            <span className="max-sm:sr-only">Refresh</span>
          </ToolbarButton>
        }
      />
      <SectionTabs section="work-log" />
      <PageIntro path="/activity" className="mx-auto w-full max-w-[1360px] px-4 sm:px-6 lg:px-6" />
      <StoredAge at={at} className="mx-auto w-full max-w-[1360px] px-4 pb-1 sm:px-6" />
      {error ? (
        <div className="mx-auto w-full max-w-[1360px] px-4 py-5 sm:px-6">
          <LoadProblem message={error} onRetry={load} />
        </div>
      ) : !rows ? (
        <Pending page label="Reading your activity" testId="activity-pending" />
      ) : rows.length === 0 ? (
        <Empty />
      ) : (
        <div className="mx-auto w-full max-w-[1360px] space-y-6 px-4 py-5 sm:px-6" data-testid="actions-log">
          <Summary rows={rows} />

          <div className="sticky top-12 z-10 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-border bg-bg/95 backdrop-blur-sm">
            <div className="flex h-10 items-stretch gap-5 overflow-x-auto [scrollbar-width:none]" role="tablist" aria-label="Show">
              {VIEWS.filter((v) => v.key === "all" || counts[v.key] > 0).map((v) => {
                const active = view === v.key;
                return (
                  <button
                    key={v.key}
                    type="button"
                    role="tab"
                    aria-selected={active}
                    onClick={() => setView(v.key)}
                    className={cn(
                      "relative flex shrink-0 items-center gap-1.5 text-[13px] whitespace-nowrap transition-colors duration-150 cursor-pointer",
                      active
                        ? "font-medium text-fg after:absolute after:inset-x-0 after:bottom-0 after:h-[2px] after:bg-fg"
                        : "text-muted hover:text-fg",
                    )}
                  >
                    {v.label}
                    <span className="num text-[12px] font-normal text-muted">{counts[v.key]}</span>
                  </button>
                );
              })}
            </div>
            <div className="relative my-1.5 w-full sm:w-60">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted" aria-hidden />
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
                className="h-8 w-full rounded-md border border-border bg-surface pl-8 pr-7 text-[12.5px] text-fg outline-none transition-colors hover:border-border-strong focus-visible:border-border-strong focus-visible:ring-2 focus-visible:ring-ring/40 placeholder:text-muted"
              />
              {search && (
                <button
                  type="button"
                  onClick={() => setSearch("")}
                  aria-label="Clear filter"
                  className="absolute right-1.5 top-1/2 inline-flex size-5 -translate-y-1/2 cursor-pointer items-center justify-center rounded text-muted hover:bg-hover hover:text-fg active:scale-[0.95]"
                >
                  <X className="size-3" aria-hidden />
                </button>
              )}
            </div>
          </div>

          {days.length === 0 ? (
            <div className="flex flex-wrap items-center gap-3 py-8 text-[13.5px] text-muted">
              <span>
                {search.trim()
                  ? <>No activity matches &ldquo;{search.trim()}&rdquo;.</>
                  : "No activity matches this filter."}
              </span>
              <button
                type="button"
                onClick={() => {
                  setSearch("");
                  setView("all");
                }}
                className="inline-flex h-7 cursor-pointer items-center rounded-md border border-border-strong bg-surface px-2.5 text-[12px] font-medium text-fg transition-colors hover:bg-surface-2 active:scale-[0.98]"
              >
                Clear filters
              </button>
            </div>
          ) : (
            <div className="space-y-5">
              <div className="hidden grid-cols-[32px_minmax(0,1fr)_minmax(0,240px)_184px_80px] items-center gap-x-3 px-4 text-[11.5px] font-medium text-muted lg:grid" aria-hidden>
                <span />
                <span>Action</span>
                <span>Record</span>
                <span>Result</span>
                <span className="text-right">Time</span>
              </div>
              {days.map((d) => (
                <section
                  key={d.key}
                  aria-label={d.label}
                  className="border-y border-border"
                >
                  <div className="flex items-center justify-between border-b border-border/70 bg-bg-subtle/60 px-4 py-2">
                    <h2 className="text-[12.5px] font-semibold text-fg">
                      {d.label}{" "}
                      <span className="ml-1 font-normal num text-muted">{d.rows.length}</span>
                    </h2>
                  </div>
                  <ul className="divide-y divide-border">
                    {d.rows.map((r) => (
                      <Row key={r.seq} r={r} />
                    ))}
                  </ul>
                </section>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
