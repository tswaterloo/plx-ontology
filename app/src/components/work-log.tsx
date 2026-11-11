"use client";

import { BellRing, Bookmark, Bot, Eye, FileText, Mail, MessageCircle, Pin, StickyNote, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { getWorkLog, getWorkThread, organiseWorkThread } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDateTime, objectHref } from "@/lib/format";
import type { WorkLogEvent, WorkLogKind } from "@/lib/types";

const TZ = "America/Toronto";

export const KIND_ICON: Record<WorkLogKind, LucideIcon> = {
  ask: MessageCircle,
  alert: BellRing,
  autopilot: Bot,
  action: Mail,
  write: FileText,
  watch: Eye,
  note: StickyNote,
  save: Bookmark,
};

export const KIND_LABEL: Record<WorkLogKind, string> = {
  ask: "Ask",
  alert: "Alerts",
  autopilot: "Autopilot",
  action: "Sent and done",
  write: "Approvals",
  watch: "Watching",
  note: "Notes",
  save: "Saved",
};

const VERB: Record<string, string> = {
  asked: "Asked",
  failed: "Asked, no answer",
  seen: "Read",
  dismissed: "Dismissed",
  snoozed: "Snoozed",
  reopened: "Put back",
  rated: "Rated",
  prepared: "Prepared",
  edited: "Edited",
  accepted: "Approved",
  rejected: "Rejected",
  sent: "Sent",
  replied: "Replied",
  forwarded: "Forwarded",
  messaged: "Messaged",
  follow_up: "Follow-up set",
  done: "Done",
  assigned: "Assigned",
  approved: "Approved",
  executed: "Ran",
  followed: "Watching",
  unfollowed: "Stopped watching",
  rule_added: "Alert rule added",
  rule_removed: "Alert rule removed",
  noted: "Note",
  saved: "Saved",
  unsaved: "Unsaved",
};

/** Where "Continue" takes you (the server's WorkLogThread.resume says the same). */
export function continueHref(thread: string): string {
  const [scheme, ...rest] = thread.split(":");
  const ref = rest.join(":");
  switch (scheme) {
    case "ask":
      return `/ask?c=${encodeURIComponent(ref)}`;
    case "autopilot":
      return `/autopilot?item=${encodeURIComponent(ref)}`;
    case "write":
      return "/my-work";
    case "record": {
      const at = ref.indexOf("/");
      const type = ref.slice(0, at);
      return at > 0 && !["note", "saved", "rule", "watch"].includes(type) ? objectHref(type, ref.slice(at + 1)) : "/work-log";
    }
    default:
      return "/";
  }
}

const timeOf = (iso: string) => new Date(iso).toLocaleTimeString("en-CA", { hour: "numeric", minute: "2-digit", timeZone: TZ });

/** A sent message's follow-through, in words. */
export function LoopLine({ e }: { e: WorkLogEvent }) {
  const loop = e.loop;
  if (!loop) return null;
  if (loop.state === "answered") return <span className="text-[color:var(--ok)]">They replied{loop.answered_at ? ` ${formatDateTime(loop.answered_at)}` : ""}</span>;
  if (loop.state === "closed") return <span className="text-muted">Closed</span>;
  if (loop.state === "overdue")
    return (
      <span className="text-[color:var(--late)]">
        {loop.nudge}
        {loop.follow_up && (
          <>
            {" "}
            <Link href={`/ask?q=${encodeURIComponent(loop.follow_up)}`} className="font-medium text-[color:var(--accent)] hover:underline">
              Draft a follow-up
            </Link>
          </>
        )}
      </span>
    );
  return <span className="text-muted">Waiting for a reply{loop.due ? ` until ${formatDateTime(loop.due)}` : ""}</span>;
}

export function WorkLogRow({ e, onChanged, showWho }: { e: WorkLogEvent; onChanged?: () => void; showWho?: boolean }) {
  const Icon = KIND_ICON[e.kind] ?? FileText;
  const people = e.people ?? [];
  const [busy, setBusy] = useState(false);
  const act = async (body: Parameters<typeof organiseWorkThread>[1]) => {
    setBusy(true);
    try {
      await organiseWorkThread(e.thread, body);
      onChanged?.();
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="grid grid-cols-[28px_minmax(0,1fr)_auto] items-start gap-x-3 rounded-[3px] px-3 py-2.5 hover:bg-hover" data-testid="work-log-row">
      <span className="flex size-7 items-center text-[color:var(--accent)]" aria-hidden>
        <Icon className="size-4" strokeWidth={1.8} />
      </span>
      <div className="min-w-0">
        <div className="truncate text-[13px] leading-[18px]">
          <span className="text-muted">{VERB[e.verb] ?? e.verb}: </span>
          <span className="font-semibold">{e.thread_name || e.title}</span>
          {e.pinned && <Pin className="ml-1.5 inline size-3 text-muted" aria-label="Pinned" />}
        </div>
        <div className="mt-0.5 flex flex-wrap gap-x-2 text-[12px] leading-4 text-muted">
          {showWho && e.reader_name && <span>{e.reader_name}</span>}
          {(e.records ?? []).slice(0, 3).map((r) =>
            r.type ? (
              <Link key={r.object_id} href={objectHref(r.type, r.object_id)} className="hover:underline">
                {r.label || r.object_id}
              </Link>
            ) : (
              <span key={r.object_id}>{r.label || r.object_id}</span>
            ),
          )}
          {people.length > 0 && <span>{people.slice(0, 2).join(", ")}{people.length > 2 ? ` +${people.length - 2}` : ""}</span>}
          {e.case_id && <span>Case {e.case_id}</span>}
          <LoopLine e={e} />
        </div>
      </div>
      <div className="flex items-center gap-2 text-[12px] leading-4 text-muted">
        <span className="tabular-nums">{timeOf(e.at)}</span>
        {e.mine && (
          <>
            <button type="button" disabled={busy} onClick={() => act({ pinned: !e.pinned })} className="cursor-pointer hover:text-fg disabled:opacity-60">
              {e.pinned ? "Unpin" : "Pin"}
            </button>
            {e.loop && (e.loop.state === "waiting" || e.loop.state === "overdue") && (
              <button type="button" disabled={busy} onClick={() => act({ closed: true })} className="cursor-pointer hover:text-fg disabled:opacity-60">
                Close
              </button>
            )}
            <Link href={continueHref(e.thread)} className="font-medium text-[color:var(--accent)] hover:underline">
              Continue
            </Link>
          </>
        )}
      </div>
    </li>
  );
}

/** The record page's section: your own work on this record. */
export function RecordWorkLog({ objectId }: { objectId: string }) {
  const [events, setEvents] = useState<WorkLogEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    const c = new AbortController();
    getWorkLog({ record: objectId, limit: 10 }, c.signal)
      .then((p) => setEvents(p.events))
      .catch((e) => !c.signal.aborted && setError(plainError(e)));
    return () => c.abort();
  }, [objectId]);
  useEffect(() => load(), [load]);
  if (error || !events || events.length === 0) return null;
  return (
    <section aria-label="Your work on this record" className="mt-6" data-testid="record-work-log">
      <div className="flex items-baseline justify-between px-3">
        <h2 className="text-[12px] font-medium text-muted">Your work on this record</h2>
        <Link href={`/work-log?record=${encodeURIComponent(objectId)}`} className={cn("text-[12px] text-muted hover:text-fg hover:underline")}>
          All of it
        </Link>
      </div>
      <ul className="mt-1">
        {events.map((e) => (
          <WorkLogRow key={e.seq} e={e} onChanged={load} />
        ))}
      </ul>
    </section>
  );
}

/** "Continue": what you already did on this thread and on its records, from your work log. */
export function ThreadContext({ thread, title }: { thread: string; title: string }) {
  const [t, setT] = useState<Awaited<ReturnType<typeof getWorkThread>> | null>(null);
  const load = useCallback(() => {
    const c = new AbortController();
    getWorkThread(thread, c.signal)
      .then(setT)
      .catch(() => setT(null));
    return () => c.abort();
  }, [thread]);
  useEffect(() => load(), [load]);
  if (!t) return null;
  const rows = [...t.events, ...(t.related ?? [])];
  return (
    <section aria-label={title} className="mb-5 rounded-[3px] bg-list py-2" data-testid="thread-context">
      <h2 className="px-4 pb-1 text-[12px] font-medium text-muted">{t.name ? `${title}: ${t.name}` : title}</h2>
      <ul>
        {rows.map((e) => (
          <WorkLogRow key={e.seq} e={e} onChanged={load} />
        ))}
      </ul>
    </section>
  );
}
