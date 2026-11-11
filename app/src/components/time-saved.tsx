"use client";

import { useEffect, useState } from "react";
import { getTimeSaved } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { TimeSaved, TimeSavedPerson } from "@/lib/types";

/** "2.4 h", "35 min": minutes as a reader takes them in. */
export function savedText(min: number): string {
  if (min < 60) return `${min} min`;
  const h = min / 60;
  return `${h < 9.95 ? h.toFixed(1) : Math.round(h)} h`;
}

const ITEM_WORD: Record<string, [string, string]> = {
  mail_sent: ["message sent from a draft", "messages sent from a draft"],
  teams_sent: ["Teams message", "Teams messages"],
  record: ["lookup or entry done for you", "lookups and entries done for you"],
};

function itemsText(items: Record<string, number> | undefined): string {
  return Object.entries(items ?? {})
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `${n} ${(ITEM_WORD[k] ?? [k, k])[n === 1 ? 0 : 1]}`)
    .join(" · ");
}

const weekLabel = (iso: string) =>
  new Date(`${iso}T12:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });

/** Four thin weekly bars, the current week last and in the accent. */
function Weeks({ p, weeks, max }: { p: TimeSavedPerson; weeks: string[]; max: number }) {
  return (
    <span className="flex h-5 items-end gap-[3px]" aria-hidden>
      {p.weeks.map((w, i) => (
        <span
          key={w.start}
          title={`Week of ${weekLabel(weeks[i] ?? w.start)}: ${savedText(w.minutes)}`}
          className={cn("w-2", i === p.weeks.length - 1 ? "bg-accent" : "bg-border-strong")}
          style={{ height: `${Math.max(2, Math.round((w.minutes / Math.max(max, 1)) * 20))}px` }}
        />
      ))}
    </span>
  );
}

/**
 * Time saved per week from the work log (O-03): the reader's own figure, or for the owner
 * with "Everyone" one row per person. Always an estimate; the method is one click away.
 */
export function TimeSavedPanel({ everyone }: { everyone: boolean }) {
  const [data, setData] = useState<TimeSaved | null>(null);
  useEffect(() => {
    const c = new AbortController();
    setData(null);
    getTimeSaved(everyone, c.signal).then(setData, () => undefined);
    return () => c.abort();
  }, [everyone]);
  if (!data || !data.available) return null;
  const weeks = data.weeks ?? [];
  const people = data.people ?? [];
  const total = (p: TimeSavedPerson) => p.weeks.reduce((n, w) => n + w.minutes, 0);
  const max = Math.max(0, ...people.flatMap((p) => p.weeks.map((w) => w.minutes)));
  const method = (
    <details className="mt-1 text-[12px] text-muted">
      <summary className="cursor-pointer select-none hover:text-fg">How this is counted</summary>
      <p className="mt-1 max-w-3xl leading-[18px]" data-testid="time-saved-method">{data.method}</p>
    </details>
  );
  if (!data.all_readers) {
    const me = people[0];
    const now = me?.weeks[me.weeks.length - 1];
    const last = me?.weeks[me.weeks.length - 2];
    return (
      <section aria-label="Time saved" className="mx-6 mt-3 border-b border-border pb-3" data-testid="time-saved">
        <div className="flex flex-wrap items-end gap-x-6 gap-y-1">
          <div>
            <p className="text-[12px] text-muted">Time saved this week (estimate)</p>
            <p className="text-[20px] font-semibold tabular-nums">{savedText(now?.minutes ?? 0)}</p>
          </div>
          <p className="pb-1 text-[12px] tabular-nums text-muted">
            last week {savedText(last?.minutes ?? 0)} · {weeks.length} weeks {savedText(me ? total(me) : 0)}
          </p>
          {me && <span className="pb-1.5"><Weeks p={me} weeks={weeks} max={max} /></span>}
          <p className="min-w-0 flex-1 pb-1 text-[12px] text-muted">{now && itemsText(now.items) ? itemsText(now.items) : "Nothing the Workbench did for you counts this week yet."}</p>
        </div>
        {method}
      </section>
    );
  }
  return (
    <section aria-label="Time saved by person" className="mx-6 mt-3 border-b border-border pb-3" data-testid="time-saved">
      <h2 className="text-[12px] font-medium text-muted">Time saved per person (estimate)</h2>
      {people.length === 0 ? (
        <p className="mt-1 text-[13px] text-muted">Nobody&apos;s work log counts any yet.</p>
      ) : (
        <table className="mt-1 w-full text-left text-[13px]">
          <thead className="text-[12px] text-muted">
            <tr className="border-b border-border">
              <th className="py-1 pr-3 font-normal">Person</th>
              <th className="py-1 pr-3 text-right font-normal">This week</th>
              <th className="py-1 pr-3 text-right font-normal">Last week</th>
              <th className="py-1 pr-3 text-right font-normal">{weeks.length} weeks</th>
              <th className="py-1 pr-3 font-normal max-sm:hidden">Trend</th>
              <th className="py-1 font-normal max-md:hidden">This week&apos;s work</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {people.map((p, i) => {
              const now = p.weeks[p.weeks.length - 1];
              const last = p.weeks[p.weeks.length - 2];
              return (
                <tr key={`${p.name}-${i}`}>
                  <td className="py-1.5 pr-3">{p.name ?? "Someone"}</td>
                  <td className="py-1.5 pr-3 text-right font-medium tabular-nums">{savedText(now?.minutes ?? 0)}</td>
                  <td className="py-1.5 pr-3 text-right tabular-nums text-muted">{savedText(last?.minutes ?? 0)}</td>
                  <td className="py-1.5 pr-3 text-right tabular-nums text-muted">{savedText(total(p))}</td>
                  <td className="py-1.5 pr-3 max-sm:hidden"><Weeks p={p} weeks={weeks} max={max} /></td>
                  <td className="py-1.5 text-[12px] text-muted max-md:hidden">{now ? itemsText(now.items) : ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {method}
    </section>
  );
}
