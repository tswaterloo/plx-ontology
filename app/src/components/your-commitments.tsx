"use client";

// The reader's own commitments on Today: what they told the Workbench they would do ("remind
// me to call Northwind Friday", or written on the Memory screen), overdue first, each with
// its due day as their words said it, and Done. Read from their own memory only; nothing
// shows while the owner views as someone.

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useViewingAs } from "@/components/view-as";
import { doneMemory, getMemory } from "@/lib/api";
import type { Pending } from "@/lib/desktop";
import type { MemoryItem } from "@/lib/types";

const TZ = "America/Toronto";

/** The thing to do, without the "remind me to" it was said with. */
export function taskOf(text: string): string {
  const t = text
    .trim()
    .replace(/^(?:please\s+)?(?:remind me(?:\s+to)?|don'?t let me forget(?:\s+to)?|i(?:'ll| will| need to| have to| must))\s+/i, "")
    .replace(/[.!]+$/, "");
  return t ? t[0].toUpperCase() + t.slice(1) : text;
}

/** Today's day in US Eastern, as YYYY-MM-DD. */
export function todayET(now: Date = new Date()): string {
  return now.toLocaleDateString("en-CA", { timeZone: TZ });
}

/** "Fri 20 Nov" for a YYYY-MM-DD day. */
function dayName(day: string): string {
  return new Date(`${day}T12:00:00Z`).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" }).replace(",", "").replace("Sept", "Sep");
}

/** "Overdue since Fri 13 Nov", "Due today", "Due tomorrow", "Due Fri 20 Nov", or "" (no day said). */
export function dueLine(due: string | null | undefined, today: string): string {
  if (!due) return "";
  const days = Math.round((Date.parse(due) - Date.parse(today)) / 86_400_000);
  if (days < 0) return `Overdue since ${dayName(due)}`;
  if (days === 0) return "Due today";
  if (days === 1) return "Due tomorrow";
  return `Due ${dayName(due)}`;
}

/** Overdue and due first (soonest first), then those with no day (newest first). */
export function byDue(items: MemoryItem[]): MemoryItem[] {
  return [...items].sort((a, b) => {
    if (a.due && b.due) return a.due.localeCompare(b.due);
    if (a.due || b.due) return a.due ? -1 : 1;
    return b.at.localeCompare(a.at);
  });
}

const REMINDED = "plx.reminded";

/** The reader's commitments due today or overdue not yet reminded of today (US Eastern), as
 * notices; marks them reminded in `store` (this browser or app: one reminder a day each). */
export function dueReminders(items: MemoryItem[], store: Pick<Storage, "getItem" | "setItem"> | null, now: Date = new Date()): Pending[] {
  const today = todayET(now);
  let held: { day?: string; ids?: number[] } = {};
  try {
    held = JSON.parse(store?.getItem(REMINDED) ?? "{}") ?? {};
  } catch {
    held = {};
  }
  const done = new Set(held.day === today ? (held.ids ?? []) : []);
  const due = byDue(items.filter((i) => i.kind === "commitment" && i.due && i.due <= today && !done.has(i.id)));
  if (due.length === 0) return [];
  try {
    store?.setItem(REMINDED, JSON.stringify({ day: today, ids: [...done, ...due.map((i) => i.id)] }));
  } catch {
    /* storage full or off: reminded again next time, never lost */
  }
  return due.map((i) => ({ id: `due:${i.id}:${today}`, title: i.due! < today ? "Overdue: you said you would" : "Due today: you said you would", body: taskOf(i.text), href: "/" }));
}

/** Done, for one commitment of the reader's (the row then leaves). */
export function DoneButton({ id, onDone }: { id: number; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  return (
    <button
      type="button"
      className="h-7 shrink-0 rounded-md px-2 text-[12px] font-medium hover:bg-hover cursor-pointer disabled:opacity-50"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        setFailed(false);
        try {
          await doneMemory(id);
          onDone();
        } catch {
          setFailed(true);
        } finally {
          setBusy(false);
        }
      }}
      data-testid="commitment-done"
    >
      {failed ? "Try again" : "Done"}
    </button>
  );
}

/** Today's "You said you would" list; nothing at all when there is none. */
export function YourCommitments({ most = 5 }: { most?: number }) {
  const viewing = useViewingAs();
  const [items, setItems] = useState<MemoryItem[] | null>(null);
  const load = useCallback(() => {
    const c = new AbortController();
    Promise.resolve()
      .then(() => getMemory(c.signal))
      .then((m) => setItems(m.available ? m.items.filter((i) => i.kind === "commitment") : []))
      .catch(() => !c.signal.aborted && setItems([]));   // Today never waits on memory
    return () => c.abort();
  }, []);
  useEffect(() => (viewing ? undefined : load()), [load, viewing]);
  if (viewing || !items || items.length === 0) return null;
  const today = todayET();
  const rows = byDue(items);
  const late = rows.filter((i) => i.due && i.due < today).length;
  return (
    <section className="border-b border-border px-5 py-3" aria-labelledby="your-commitments" data-testid="your-commitments">
      <h2 id="your-commitments" className="text-[13px] font-semibold">
        You said you would
        <span className="ml-1.5 font-normal text-muted">
          {rows.length}
          {late ? `, ${late} overdue` : ""}
        </span>
      </h2>
      <ul className="mt-1">
        {rows.slice(0, most).map((i) => {
          const due = dueLine(i.due, today);
          return (
            <li key={i.id} className="flex items-baseline gap-3 py-1" data-testid="commitment-row">
              <p className="min-w-0 flex-1 text-[13px] leading-5">
                {taskOf(i.text)}
                {due && <span className={i.due && i.due < today ? "ml-2 text-[12px] font-medium text-late" : "ml-2 text-[12px] text-muted"}>{due}</span>}
              </p>
              <DoneButton id={i.id} onDone={load} />
            </li>
          );
        })}
      </ul>
      {rows.length > most && (
        <Link href="/memory" className="text-[12px] text-muted hover:underline">
          {rows.length - most} more in Memory
        </Link>
      )}
    </section>
  );
}
