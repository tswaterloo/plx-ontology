"use client";

import { Check, Clock3, ListChecks, Mail, MessageCircle, X, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { StatusWord } from "@/components/ui/status-badge";
import { Toolbar } from "@/components/ui/toolbar";
import { actionChip } from "@/lib/actions";
import { getActionsLog } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { clockET, formatDateTime, plural } from "@/lib/format";
import type { ActionLogRow } from "@/lib/types";

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
  const title = r.subject || (
    <>
      {KIND[r.kind] ?? r.kind}
      {r.card_label && <span className="lg:hidden">: {r.card_label}</span>}
    </>
  );
  const to = (r.to ?? []).join(", ");
  const cc = (r.cc ?? []).join(", ");
  const line = mail ? [to ? `To ${to}` : "No recipient", cc ? `cc ${cc}` : ""].filter(Boolean).join(" · ") : r.text;
  const cites = (r.citations ?? []).length;
  return (
    <li
      className="grid grid-cols-[44px_minmax(0,1fr)_auto] items-start gap-x-3 lg:items-center rounded-[3px] px-3 py-2.5 hover:bg-hover lg:grid-cols-[44px_minmax(0,1fr)_minmax(0,240px)_184px_80px]"
      data-testid="actions-log-row"
      title={`${formatDateTime(r.at)}, by ${r.actor}. Fingerprint ${r.payload_hash.slice(0, 12)}`}
    >
      <KindTile kind={r.kind} />
      <div className="min-w-0">
        <div className="truncate text-[13px] leading-[18px] font-semibold">{title}</div>
        <div className="mt-0.5 truncate text-[12px] leading-4 text-muted">
          {line}
          {r.card_label && mail && <span className="lg:hidden"> · {r.card_label}</span>}
        </div>
      </div>
      <div className="hidden min-w-0 lg:block">
        <div className="truncate text-[13px] leading-[18px]">{r.card_label || "Follow-up"}</div>
        <div className="mt-0.5 truncate text-[12px] leading-4 text-muted">
          {r.actor}
          {cites > 0 && <span className="text-faint"> · {plural(cites, "cited fact")}</span>}
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
        Each message you send, task you set and item you put away from My work, a record page or Ask shows here, with whom it went to, the record it was about and whether it went out.
      </p>
      <Link href="/" className="mt-3 inline-block text-[13px] font-medium text-accent hover:underline">
        Open My work
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
    check: rows.filter((r) => r.status === "failed" || r.status === "unknown").slice(0, 5),
  };
}

function Summary({ rows }: { rows: ActionLogRow[] }) {
  const w = weekOf(rows);
  const cells: [string, number, string?][] = [
    ["Sent", w.sent],
    ["Not sent (sending off)", w.notSent],
    ["Tasks set or done", w.tasks],
    ["Put away", w.putAway],
    ["People reached", w.people],
    ["Records", w.records],
  ];
  return (
    <section className="border-b border-border px-6 py-3" aria-label="Last 7 days" data-testid="activity-summary">
      <h2 className="text-[12px] font-medium text-muted">Last 7 days</h2>
      <dl className="mt-1 grid grid-cols-3 gap-x-6 gap-y-1 sm:grid-cols-6">
        {cells.map(([label, n]) => (
          <div key={label}>
            <dt className="text-[11px] leading-4 text-muted">{label}</dt>
            <dd className="text-[18px] font-semibold tabular-nums">{n}</dd>
          </div>
        ))}
      </dl>
      {w.check.length > 0 && (
        <div className="mt-2" data-testid="activity-check">
          <h3 className="text-[12px] font-medium text-late">Needs a look</h3>
          <ul className="text-[12px]">
            {w.check.map((r) => (
              <li key={r.seq} className="truncate">
                <span className="font-medium">{KIND[r.kind] ?? r.kind}</span> on {r.card_label}:{" "}
                <span className="text-muted">{r.status === "failed" ? "it failed; send it again from the record" : "it may not have gone out; check the sent folder before sending again"}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

/** Your actions journal: every action you confirmed, newest first (append-only on the server). */
export default function ActivityPage() {
  const [rows, setRows] = useState<ActionLogRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    getActionsLog(c.signal)
      .then((r) => setRows(r.rows ?? []))
      .catch((e) => !c.signal.aborted && setError(plainError(e)));
    return () => c.abort();
  }, []);
  useEffect(() => load(), [load]);

  const days = useMemo(() => {
    const out: { key: string; label: string; rows: ActionLogRow[] }[] = [];
    for (const r of rows ?? []) {
      const k = dayKey(r.at);
      const last = out[out.length - 1];
      if (last?.key === k) last.rows.push(r);
      else out.push({ key: k, label: dayLabel(r.at), rows: [r] });
    }
    return out;
  }, [rows]);

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-list">
      <Toolbar
        heading
        title={
          <>
            Activity
            {rows && rows.length > 0 && <span className="ml-2 text-[13px] font-normal tracking-normal num text-faint"><span className="sr-only">, </span>{rows.length}</span>}
          </>
        }
        className="bg-list"
      />
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
          <div className="hidden h-8 grid-cols-[44px_minmax(0,1fr)_minmax(0,240px)_184px_80px] items-end gap-x-3 px-6 pb-1 text-[11px] leading-4 text-faint lg:grid" aria-hidden>
            <span />
            <span>Action</span>
            <span>Record</span>
            <span>Result</span>
            <span className="text-right">Time</span>
          </div>
          {days.map((d) => (
            <section key={d.key} aria-label={d.label}>
              <div className="sticky top-[52px] z-10 flex h-9 items-end bg-list px-6 pb-1.5">
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
          ))}
        </div>
      )}
    </div>
  );
}
