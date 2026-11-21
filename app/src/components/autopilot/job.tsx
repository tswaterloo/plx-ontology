"use client";

// An autopilot job: the system changes an event calls for, each proposed by the model and
// prepared only when the person reviews it (read from its system, before and after), then
// confirmed in two clicks on the prepared change itself. Nothing here writes.

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { useProblem, useProblemSlot } from "@/components/ui/notice";
import { PreparedChange } from "@/components/prepared-change";
import { TodoSteps } from "@/components/autopilot/todo";
import { prepareAutopilotJob, type PreparedItem, type ProposedJob } from "@/lib/api";
import { ItemActions } from "@/components/autopilot/item-actions";
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

export { tomorrowMorning } from "@/components/autopilot/item-actions";

function Proposed({ itemId, job }: { itemId: string; job: ProposedJob }) {
  const [pid, setPid] = useState<string | null>(job.prepared_id ?? null);
  const [busy, setBusy] = useState(false);
  const [, setError, problem] = useProblem();
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
      {problem}
    </li>
  );
}

export function JobCard({ item, linked, onGone }: { item: PreparedItem; linked?: boolean; onGone: (id: string) => void }) {
  const card = useProblemSlot();
  return (
    <li id={`prepared-${item.item_id}`} className={linked ? "space-y-2 border-l-2 border-accent py-4 pl-3" : "space-y-2 py-4"} data-testid="prepared-job">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <span className="text-[13px] font-medium">{item.sender || "From the records"}</span>
        {item.subject && <span className="text-[12px] text-muted">{item.subject}</span>}
        <span className="num ml-auto text-[12px] text-muted">{formatDateTime(item.created_at)}</span>
      </div>
      {item.summary && <p className="max-w-[46rem] text-[13px]">{item.summary}</p>}
      {item.message && <p className="max-w-[46rem] whitespace-pre-wrap text-[12px] text-muted">{item.message}</p>}
      {(item.facts ?? []).length > 0 && (
        <ul className="max-w-[46rem] list-disc space-y-0.5 pl-4 text-[12px] text-muted" aria-label="Why">
          {item.facts.map((f, n) => (
            <li key={n}>{f}</li>
          ))}
        </ul>
      )}
      {item.note && <p className="text-[12px] text-muted">{item.note}</p>}
      {(item.links ?? []).length > 0 && (
        <div className="flex flex-wrap gap-3 text-[13px]">
          {(item.links ?? []).map((l) => (
            <a key={l.href} href={l.href} {...(l.href.startsWith("/") ? {} : { target: "_blank", rel: "noopener noreferrer" })} className="text-accent underline-offset-2 hover:underline">
              {l.label}
            </a>
          ))}
        </div>
      )}
      {card.provide(
        <>
          <ul className="max-w-[46rem] divide-y divide-border" aria-label="Changes to review">
            {(item.jobs ?? []).map((j) => (
              <Proposed key={j.index} itemId={item.item_id} job={j} />
            ))}
          </ul>
          <TodoSteps todos={item.todos ?? []} />
          <ItemActions item={item} onGone={onGone} />
        </>,
      )}
      {card.banner}
    </li>
  );
}
