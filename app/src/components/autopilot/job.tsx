"use client";

// An autopilot job: the system changes an event calls for, each proposed by the model and
// prepared only when the person reviews it (read from its system, before and after), then
// confirmed in two clicks on the prepared change itself. Nothing here writes.

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { ErrorNotice } from "@/components/ui/notice";
import { PreparedChange } from "@/components/prepared-change";
import { TodoSteps } from "@/components/autopilot/todo";
import { decideAutopilotItem, prepareAutopilotJob, setNeverSender, type PreparedItem, type ProposedJob } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";

const SYSTEMS: Record<string, string> = {
  bc: "Business Central",
  portal: "Portal",
  mc: "Mission Control",
  planner: "Planner",
  calendar: "Calendar",
  sharepoint: "SharePoint",
};

function fields(change: Record<string, unknown>): string {
  return Object.entries(change)
    .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ");
}

/** 8 AM tomorrow, the person's own time: when a "Later" item comes back. */
export function tomorrowMorning(now: Date = new Date()): Date {
  const d = new Date(now);
  d.setDate(d.getDate() + 1);
  d.setHours(8, 0, 0, 0);
  return d;
}

/** Later (snoozed to tomorrow 8 AM) or Not needed (rejected): either takes the item off the list.
 * Never from this sender (given a sender and onNever): nothing more from them until undone. */
export function SetAside({ itemId, disabled, onGone, sender, onNever }: { itemId: string; disabled?: boolean; onGone: (id: string) => void; sender?: string; onNever?: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (act: () => Promise<unknown>, then: () => void) => {
    setBusy(true);
    setError(null);
    try {
      await act();
      then();
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const drop = (decision: "reject" | "snooze") =>
    run(() => decideAutopilotItem(itemId, decision === "snooze" ? { decision, until: tomorrowMorning().toISOString() } : { decision }), () => onGone(itemId));
  return (
    <>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" disabled={busy || disabled} onClick={() => void drop("snooze")}>
          Later: tomorrow 8 AM
        </Button>
        <Button size="sm" variant="outline" disabled={busy || disabled} onClick={() => void drop("reject")}>
          Not needed
        </Button>
        {sender && onNever && (
          <Button size="sm" variant="outline" disabled={busy || disabled} onClick={() => void run(() => setNeverSender(sender, true), onNever)}>
            Never from this sender
          </Button>
        )}
      </div>
      {error && <ErrorNotice message={error} />}
    </>
  );
}

function Proposed({ itemId, job }: { itemId: string; job: ProposedJob }) {
  const [pid, setPid] = useState<string | null>(job.prepared_id ?? null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const review = async () => {
    setBusy(true);
    setError(null);
    try {
      setPid((await prepareAutopilotJob(itemId, job.index)).prepared_id);
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="space-y-1 py-2" data-testid="proposed-job">
      <p className="text-[13px]">
        <span className="font-medium">{SYSTEMS[job.system] ?? job.system}</span> · {job.operation}
        {job.state ? <span className="text-muted"> · {job.state}</span> : null}
      </p>
      {job.reason && <p className="text-[12px] text-muted">{job.reason}</p>}
      {Object.keys(job.change).length > 0 && <p className="text-[12px]">{fields(job.change)}</p>}
      {pid ? (
        <PreparedChange id={pid} />
      ) : (
        <Button size="sm" variant="outline" disabled={busy} onClick={() => void review()}>
          Review this change
        </Button>
      )}
      {error && <ErrorNotice message={error} />}
    </li>
  );
}

export function JobCard({ item, linked, onGone }: { item: PreparedItem; linked?: boolean; onGone: (id: string) => void }) {
  return (
    <li id={`prepared-${item.item_id}`} className={linked ? "space-y-2 border-l-2 border-accent py-4 pl-3" : "space-y-2 py-4"} data-testid="prepared-job">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <span className="text-[13px] font-medium">{item.sender || "From the records"}</span>
        {item.subject && <span className="text-[12px] text-muted">{item.subject}</span>}
        <span className="num ml-auto text-[12px] text-muted">{formatDateTime(item.created_at)}</span>
      </div>
      {item.summary && <p className="max-w-[46rem] text-[13px]">{item.summary}</p>}
      {item.message && <p className="max-w-[46rem] whitespace-pre-wrap text-[12px] text-muted">{item.message}</p>}
      <ul className="max-w-[46rem] divide-y divide-border" aria-label="Changes to review">
        {(item.jobs ?? []).map((j) => (
          <Proposed key={j.index} itemId={item.item_id} job={j} />
        ))}
      </ul>
      <TodoSteps todos={item.todos ?? []} />
      <SetAside itemId={item.item_id} onGone={onGone} />
    </li>
  );
}
