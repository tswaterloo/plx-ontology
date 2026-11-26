"use client";

// An autopilot job: the system changes an event calls for, each proposed by the model and
// prepared only when the person reviews it (read from its system, before and after), then
// confirmed in two clicks on the prepared change itself. Nothing here writes.

import { ExternalLink, Wrench } from "lucide-react";
import { Fragment, useState } from "react";
import { Button } from "@/components/ui/button";
import { useProblem, useProblemSlot } from "@/components/ui/notice";
import { PreparedChange } from "@/components/prepared-change";
import { TodoSteps } from "@/components/autopilot/todo";
import { cleanHeadline, formatSender, noteParts, saidOnce } from "@/components/autopilot/lines";
import { prepareAutopilotJob, type PreparedItem, type ProposedJob } from "@/lib/api";
import { ItemActions, type OptimisticCallbacks } from "@/components/autopilot/item-actions";
import { StatusIndicator } from "@/components/autopilot/jobs";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { countedWords, formatDateTime } from "@/lib/format";
import { operationLabel } from "@/lib/autopilot";

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

function Proposed({ itemId, job, detail }: { itemId: string; job: ProposedJob; detail?: boolean }) {
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
  const entries = Object.entries(job.change);

  if (detail) {
    return (
      <li className="space-y-3 border-l-2 border-border-strong py-1.5 pl-4" data-testid="proposed-job">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2 text-[13.5px] text-fg">
            <Wrench className="size-3.5 shrink-0 text-muted" aria-hidden />
            <span className="font-medium">{SYSTEMS[job.system] ?? job.system}</span>
            <span className="text-faint" aria-hidden>·</span>
            <span className="text-muted">{operationLabel(job.operation)}</span>
            {job.state && (
              <>
                <span className="text-faint" aria-hidden>·</span>
                <span className="text-[12px] text-muted">{job.state}</span>
              </>
            )}
          </div>
          {!pid && (
            <button
              type="button"
              disabled={busy}
              onClick={() => void review()}
              className="inline-flex h-8 shrink-0 items-center rounded-md bg-accent px-3 text-[13px] font-medium text-accent-fg transition-all hover:opacity-90 active:scale-[0.98] disabled:opacity-50 cursor-pointer"
            >
              {busy ? "Preparing..." : "Review this change"}
            </button>
          )}
        </div>
        {job.reason && <p className="text-[13.5px] leading-relaxed text-muted">{job.reason}</p>}
        {entries.length > 0 && (
          <dl className="grid grid-cols-1 gap-1.5 border-t border-border/80 pt-2.5 text-[13px] sm:grid-cols-2">
            {entries.map(([k, v]) => (
              <div key={k} className="flex items-baseline gap-2">
                <dt className="text-[12px] text-muted">{k}:</dt>
                <dd className="min-w-0 break-words text-fg">{typeof v === "string" ? v : JSON.stringify(v)}</dd>
              </div>
            ))}
          </dl>
        )}
        {pid && <PreparedChange id={pid} />}
        {problem}
      </li>
    );
  }

  return (
    <li className="space-y-1.5 py-2.5" data-testid="proposed-job">
      <p className="text-[13.5px] text-fg">
        <span className="font-medium">{SYSTEMS[job.system] ?? job.system}</span> · {operationLabel(job.operation)}
        {job.state ? <span className="text-muted"> · {job.state}</span> : null}
      </p>
      {job.reason && <p className="text-[13px] text-muted">{job.reason}</p>}
      {entries.length > 0 && <p className="text-[13px] text-fg/90">{fields(job.change)}</p>}
      {pid ? (
        <PreparedChange id={pid} />
      ) : (
        <Button size="sm" variant="outline" disabled={busy} onClick={() => void review()}>
          {busy ? "Preparing..." : "Review this change"}
        </Button>
      )}
      {problem}
    </li>
  );
}

export function JobCard({
  item,
  linked,
  detail,
  onGone,
  onOptimistic,
  onRestore,
  onFeedback,
  feedback,
}: {
  item: PreparedItem;
  linked?: boolean;
  detail?: boolean;
  onGone: (id: string) => void;
  feedback?: React.ReactNode;
} & OptimisticCallbacks) {
  const card = useProblemSlot();
  const parsedNote = noteParts(item.note ?? "");
  const [subjectLine, summary, message, noteText] = saidOnce([item.subject ?? "", item.summary ?? "", item.message ?? "", parsedNote.text]);
  const headline = summary || subjectLine;
  const subHeader = summary && subjectLine ? subjectLine : "";

  if (detail) {
    const senderInfo = formatSender(item.sender);
    const title = cleanHeadline(headline || item.subject || senderInfo.name || "Prepared change");
    const hasJobs = (item.jobs ?? []).length > 0;
    return (
      <li
        id={`prepared-${item.item_id}`}
        className={cn("w-full space-y-6", linked && "border-l-2 border-l-accent pl-4")}
        data-testid="prepared-job"
      >
        {card.provide(
          <>
            <header className="space-y-3 border-b border-border pb-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <h2 className="min-w-0 flex-1 text-[20px] font-semibold leading-[1.3] tracking-[-0.02em] text-fg">
                  {countedWords(title)}
                </h2>
                <div className="shrink-0">
                  <ItemActions
                    item={item}
                    onGone={onGone}
                    onOptimistic={onOptimistic}
                    onRestore={onRestore}
                    onFeedback={onFeedback}
                    menuAlign="right"
                  />
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12.5px] text-muted">
                <StatusIndicator
                  tone={hasJobs ? "ok" : "neutral"}
                  label={hasJobs ? "Ready" : "Waiting"}
                />
                {senderInfo.name && (
                  <span className="font-medium text-fg" title={senderInfo.email || item.sender || undefined}>
                    {senderInfo.name}
                  </span>
                )}
                {subHeader && cleanHeadline(subHeader) !== title && (
                  <span className="truncate text-muted">{subHeader}</span>
                )}
                {parsedNote.labels.map((l) => (
                  <span key={l} className="text-[12px] text-faint" data-testid="prepared-label">
                    {l}
                  </span>
                ))}
                <span className="num ml-auto text-[12px] text-faint">{formatDateTime(item.created_at)}</span>
              </div>
            </header>

            {(message || (item.facts ?? []).length > 0 || noteText || (item.links ?? []).length > 0) && (
              <section className="space-y-3">
                <h3 className="text-[12px] font-medium uppercase tracking-[0.04em] text-faint">What happened</h3>
                {message && (
                  <p className="whitespace-pre-wrap text-[14.5px] leading-[1.65] text-fg">
                    {message}
                  </p>
                )}
                {(item.facts ?? []).length > 0 && (
                  <ul className="list-disc space-y-1 pl-5 text-[14px] leading-[1.6] text-fg/90" aria-label="Why">
                    {item.facts.map((f, n) => (
                      <li key={n}>{f}</li>
                    ))}
                  </ul>
                )}
                {noteText && (
                  <p className="text-[13.5px] leading-relaxed text-muted" data-testid="prepared-note">
                    {noteText}
                  </p>
                )}
                {(item.links ?? []).length > 0 && (
                  <div className="flex flex-wrap gap-4 pt-1 text-[13.5px]">
                    {(item.links ?? []).map((l) => (
                      <a
                        key={l.href}
                        href={l.href}
                        {...(l.href.startsWith("/") ? {} : { target: "_blank", rel: "noopener noreferrer" })}
                        className="inline-flex items-center gap-1 font-medium text-accent underline-offset-2 hover:underline"
                      >
                        <span>{l.label}</span>
                        {!l.href.startsWith("/") && <ExternalLink className="size-3.5" aria-hidden />}
                      </a>
                    ))}
                  </div>
                )}
              </section>
            )}

            {hasJobs && (
              <section className="space-y-3 border-t border-border pt-5">
                <h3 className="text-[12.5px] font-medium text-muted">Changes to review</h3>
                <ul className="space-y-3" aria-label="Changes to review">
                  {(item.jobs ?? []).map((j) => (
                    <Proposed key={j.index} itemId={item.item_id} job={j} detail />
                  ))}
                </ul>
              </section>
            )}

            <TodoSteps todos={item.todos ?? []} />

            {feedback && (
              <div className="border-t border-border pt-4" data-testid="prepared-feedback-row">
                {feedback}
              </div>
            )}
          </>,
        )}
        {card.banner}
      </li>
    );
  }

  return (
    <li
      id={`prepared-${item.item_id}`}
      className={linked ? "w-full space-y-3 border-l-2 border-l-accent pl-3 py-1" : "w-full space-y-3 py-1"}
      data-testid="prepared-job"
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        {item.sender && <span className="text-[14px] font-semibold text-fg">{item.sender}</span>}
        {subHeader && (
          <>
            <span className="text-muted/60" aria-hidden>·</span>
            <span className="max-w-full truncate text-[13px] text-muted">
              {subHeader}
            </span>
          </>
        )}
        {parsedNote.labels.map((l) => (
          <Fragment key={l}>
            <span className="text-muted/60" aria-hidden>·</span>
            <p className="text-[12px] leading-4 text-muted" data-testid="prepared-label">
              {l}
            </p>
          </Fragment>
        ))}
        <span className="num ml-auto text-[12px] text-muted">{formatDateTime(item.created_at)}</span>
      </div>
      {headline && <p className="text-[14.5px] font-medium leading-[1.6] text-fg">{countedWords(headline)}</p>}
      {message && <p className="whitespace-pre-wrap border-l-2 border-border-strong pl-3 text-[13.5px] leading-relaxed text-muted">{message}</p>}
      {(item.facts ?? []).length > 0 && (
        <ul className="list-disc space-y-0.5 pl-4 text-[13.5px] leading-relaxed text-muted" aria-label="Why">
          {item.facts.map((f, n) => (
            <li key={n}>{f}</li>
          ))}
        </ul>
      )}
      {noteText && <p className="text-[12.5px] text-muted" data-testid="prepared-note">{noteText}</p>}
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
            <ul className="divide-y divide-border/70 border-t border-border/70 pt-2" aria-label="Changes to review">
              {(item.jobs ?? []).map((j) => (
                <Proposed key={j.index} itemId={item.item_id} job={j} />
              ))}
            </ul>
          )}
          <TodoSteps todos={item.todos ?? []} />
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/70 pt-3">
            <div className="min-w-0">
              <ItemActions item={item} onGone={onGone} onOptimistic={onOptimistic} onRestore={onRestore} onFeedback={onFeedback} />
            </div>
            {feedback && <div className="shrink-0" data-testid="prepared-feedback-row">{feedback}</div>}
          </div>
        </>,
      )}
      {card.banner}
    </li>
  );
}
