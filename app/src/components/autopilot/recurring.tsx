"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { changeRecurring, getRecurring, stopRecurring } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import type { RecurringInstruction, RecurringInstructions } from "@/lib/types";

const WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"] as const;
const SELECT = "rounded-[3px] border border-border bg-transparent px-1 py-0.5 text-[12px]";

/** 8 -> "8 AM", 13 -> "1 PM" (US Eastern, as the server runs them). */
export function hourWords(hour: number): string {
  return `${hour % 12 || 12} ${hour < 12 ? "AM" : "PM"}`;
}

function when(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString("en-CA", {
    timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });
}

/** Your recurring instructions: when each runs, its last run, and change, pause or stop. */
export function RecurringView() {
  const [data, setData] = useState<RecurringInstructions | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    getRecurring(ctl.signal).then(setData, (e) => !ctl.signal.aborted && setError(e));
    return () => ctl.abort();
  }, [round]);
  const reload = () => setRound((n) => n + 1);
  return (
    <section className="mt-6" data-testid="recurring-instructions">
      <h2 className="mb-2 text-[13px] font-medium">Your recurring instructions</h2>
      {error ? (
        <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={reload} />
      ) : !data ? (
        <Pending label="Reading your recurring instructions" />
      ) : (data.instructions ?? []).length === 0 ? (
        <EmptyState title="No recurring instruction kept." line={'Type one in Ask ("every Monday, chase the late POs") and keep it.'} />
      ) : (
        <ul className="divide-y divide-border">
          {(data.instructions ?? []).map((r) => (
            <Row key={r.id} row={r} onChanged={reload} />
          ))}
        </ul>
      )}
    </section>
  );
}

function Row({ row, onChanged }: { row: RecurringInstruction; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const act = (p: Promise<unknown>) => {
    setBusy(true);
    setError(null);
    p.then(onChanged, setError).finally(() => setBusy(false));
  };
  const paused = row.state === "paused";
  return (
    <li className="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-3 px-3 py-2 text-[13px] max-sm:grid-cols-1" data-testid="recurring-row">
      <span className="min-w-0">
        <span className="block truncate font-medium">{row.label}</span>
        <span className="block truncate text-[12px] text-muted">{row.text}</span>
        <span className="block text-[12px] text-muted">
          {row.words} at {hourWords(row.hour)}
          {paused ? " · paused" : row.next_run ? ` · next ${when(row.next_run)}` : ""}
          {row.last_run ? ` · last ran ${when(row.last_run)}${row.last_result ? `: ${row.last_result}` : ""}` : " · not run yet"}
        </span>
        {error ? <span role="alert" className="block text-[12px]">{plainError(error)}</span> : null}
      </span>
      <span className="flex flex-wrap items-center gap-2">
        <select aria-label={`Hour for ${row.label}`} className={SELECT} value={row.hour} disabled={busy}
          onChange={(e) => act(changeRecurring(row.id, { hour: Number(e.target.value) }))}>
          {Array.from({ length: 24 }, (_, h) => (
            <option key={h} value={h}>{hourWords(h)}</option>
          ))}
        </select>
        {row.kind === "schedule.weekly" && (
          <select aria-label={`Weekday for ${row.label}`} className={SELECT} value={String(row.params?.weekday ?? "monday")} disabled={busy}
            onChange={(e) => act(changeRecurring(row.id, { weekday: e.target.value as (typeof WEEKDAYS)[number] }))}>
            {WEEKDAYS.map((d) => (
              <option key={d} value={d}>{d[0].toUpperCase() + d.slice(1)}</option>
            ))}
          </select>
        )}
        <Button disabled={busy} onClick={() => act(changeRecurring(row.id, { state: paused ? "active" : "paused" }))}>{paused ? "Resume" : "Pause"}</Button>
        <Button disabled={busy} onClick={() => act(stopRecurring(row.id))}>Stop</Button>
      </span>
    </li>
  );
}
