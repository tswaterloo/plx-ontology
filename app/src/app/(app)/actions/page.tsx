"use client";

import { Check, Clock3, ListChecks, X, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { SystemTile } from "@/components/ui/glyphs";
import { LoadProblem } from "@/components/ui/notice";
import { StatusWord } from "@/components/ui/status-badge";
import { Toolbar } from "@/components/ui/toolbar";
import { actionChip } from "@/lib/actions";
import { getActionsLog } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDateTime, plural } from "@/lib/format";
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

/** Tasks drawn as the app draws its places: a white glyph on a grey tile. */
const TASK: Record<string, LucideIcon> = { create_follow_up: ListChecks, mark_done: Check, snooze: Clock3, dismiss: X };

function KindTile({ kind }: { kind: string }) {
  if (kind === "teams_message") return <SystemTile system="Teams" size={32} />;
  const Icon = TASK[kind];
  if (!Icon) return <SystemTile system="Email" size={32} />;
  return (
    <span className="flex size-8 shrink-0 items-center justify-center rounded-[9px] bg-[linear-gradient(180deg,#90939b,#72757e)] text-white shadow-[inset_0_-1px_0_rgb(0_0_0/0.12)]" aria-hidden>
      <Icon className="size-[17px]" strokeWidth={2.2} />
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

const timeOf = (iso: string) => new Date(iso).toLocaleTimeString("en-CA", { hour: "numeric", minute: "2-digit", timeZone: TZ });

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
      className="grid grid-cols-[32px_minmax(0,1fr)_auto] items-start gap-x-3 lg:items-center rounded-[10px] px-3 py-2.5 hover:bg-hover lg:grid-cols-[32px_minmax(0,1fr)_minmax(0,240px)_184px_80px]"
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
    <div className="flex flex-1 flex-col items-center justify-center px-6 py-16 text-center">
      <div className="w-full max-w-[340px] space-y-1.5" aria-hidden>
        {["w-[90%]", "w-[62%]", "w-[40%]"].map((w, i) => (
          <div key={i} className={cn("flex items-center gap-3 rounded-[10px] bg-bg px-3 py-2.5 shadow-lift", i === 1 && "opacity-60", i === 2 && "opacity-30")}>
            <span className="size-8 shrink-0 rounded-[9px] bg-surface-2" />
            <span className="flex-1 space-y-1.5">
              <span className={cn("block h-2.5 rounded-full bg-surface-2", w)} />
              <span className="block h-2 w-1/2 rounded-full bg-[color-mix(in_srgb,var(--fg)_5%,transparent)]" />
            </span>
          </div>
        ))}
      </div>
      <p className="mt-6 text-[13px] font-medium">No actions yet</p>
      <p className="mt-1 max-w-[300px] text-[12px] leading-[18px] text-muted">Emails you send and tasks you log from Today are kept here.</p>
      <Link
        href="/"
        className="mt-4 inline-flex h-8 items-center rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-3 text-[13px] font-medium hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)]"
      >
        Open Today
      </Link>
    </div>
  );
}

/** Your actions journal: every action you confirmed, newest first (append-only on the server). */
export default function ActionsPage() {
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
        title={
          <>
            Action log
            {rows && rows.length > 0 && <span className="ml-2 text-[13px] font-normal tracking-normal tabular-nums text-faint">{rows.length}</span>}
          </>
        }
        className="bg-[color-mix(in_srgb,var(--list)_90%,transparent)]"
      />
      {error ? (
        <div className="p-5">
          <LoadProblem message={error} onRetry={load} />
        </div>
      ) : !rows ? (
        <ul className="space-y-1 px-2 pt-3 sm:px-3" aria-busy="true" aria-label="Loading the log">
          {Array.from({ length: 7 }).map((_, i) => (
            <li key={i} className="flex items-center gap-3 px-3 py-2.5">
              <span className="skeleton size-8 rounded-[9px]" />
              <span className="flex-1 space-y-2">
                <span className={cn("skeleton block h-3", ["w-1/2", "w-2/5", "w-1/3"][i % 3])} />
                <span className={cn("skeleton block h-2.5", ["w-1/3", "w-1/4"][i % 2])} />
              </span>
            </li>
          ))}
        </ul>
      ) : rows.length === 0 ? (
        <Empty />
      ) : (
        <div className="pb-8" data-testid="actions-log">
          <div className="hidden h-8 grid-cols-[32px_minmax(0,1fr)_minmax(0,240px)_184px_80px] items-end gap-x-3 px-6 pb-1 text-[11px] leading-4 text-faint lg:grid" aria-hidden>
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
                  {d.label} <span className="font-normal tabular-nums text-faint">{d.rows.length}</span>
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
