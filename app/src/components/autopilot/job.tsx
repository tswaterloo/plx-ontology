"use client";

// An autopilot job: the system changes an event calls for, each proposed by the model and
// prepared only when the person reviews it (read from its system, before and after), then
// confirmed in two clicks on the prepared change itself. Nothing here writes.

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { useProblem, useProblemSlot } from "@/components/ui/notice";
import { PreparedChange } from "@/components/prepared-change";
import { TodoSteps } from "@/components/autopilot/todo";
import { noteParts, saidOnce } from "@/components/autopilot/lines";
import { prepareAutopilotJob, type PreparedItem, type ProposedJob } from "@/lib/api";
import { ItemActions } from "@/components/autopilot/item-actions";
import { plainError } from "@/lib/errors";
import { countedWords, formatDateTime } from "@/lib/format";

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

export function JobCard({ item, linked, onGone, feedback }: { item: PreparedItem; linked?: boolean; onGone: (id: string) => void; feedback?: React.ReactNode }) {
  const card = useProblemSlot();
  const parsedNote = noteParts(item.note ?? "");
  const [subjectLine, summary, message, noteText] = saidOnce([item.subject ?? "", item.summary ?? "", item.message ?? "", parsedNote.text]);
  const headline = summary || subjectLine;
  const subHeader = summary && subjectLine ? subjectLine : "";
  return (
    <li
      id={`prepared-${item.item_id}`}
      className={linked ? "max-w-[50rem] space-y-3 rounded-lg border border-accent/70 bg-surface/60 p-4 ring-1 ring-accent/30" : "max-w-[50rem] space-y-3 rounded-lg border border-border bg-surface/60 p-4"}
      data-testid="prepared-job"
    >
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
        {item.sender && <span className="text-[14px] font-semibold text-fg">{item.sender}</span>}
        {subHeader && (
          <span className="inline-flex max-w-full items-center truncate rounded-md border border-border/70 bg-bg/80 px-2 py-0.5 text-[12px] text-muted">
            {subHeader}
          </span>
        )}
        {parsedNote.labels.map((l) => (
          <p key={l} className="inline-flex items-center rounded-md bg-surface-2 px-2 py-0.5 text-[11px] leading-4 text-muted" data-testid="prepared-label">
            {l}
          </p>
        ))}
        <span className="num ml-auto text-[12px] text-muted">{formatDateTime(item.created_at)}</span>
      </div>
      {headline && <p className="text-[14px] font-medium leading-relaxed text-fg">{countedWords(headline)}</p>}
      {message && <p className="whitespace-pre-wrap border-l-2 border-border-strong pl-3 text-[13px] leading-relaxed text-muted">{message}</p>}
      {(item.facts ?? []).length > 0 && (
        <ul className="list-disc space-y-0.5 pl-4 text-[13px] leading-relaxed text-muted" aria-label="Why">
          {item.facts.map((f, n) => (
            <li key={n}>{f}</li>
          ))}
        </ul>
      )}
      {noteText && <p className="text-[12px] text-muted" data-testid="prepared-note">{noteText}</p>}
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
          {(item.jobs ?? []).length > 0 && (
            <ul className="divide-y divide-border/70 rounded-md border border-border/70 bg-bg/80 px-3" aria-label="Changes to review">
              {(item.jobs ?? []).map((j) => (
                <Proposed key={j.index} itemId={item.item_id} job={j} />
              ))}
            </ul>
          )}
          <TodoSteps todos={item.todos ?? []} />
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/70 pt-3">
            <div className="min-w-0">
              <ItemActions item={item} onGone={onGone} />
            </div>
            {feedback && <div className="shrink-0" data-testid="prepared-feedback-row">{feedback}</div>}
          </div>
        </>,
      )}
      {card.banner}
    </li>
  );
}
