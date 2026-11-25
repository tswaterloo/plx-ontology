"use client";

// A whole job handed to Assist: the box, its progress, and the pieces it prepared, each one approved,
// edited or set aside by the person. A change in a system is reviewed on its own card and confirmed in
// two clicks there; approving a piece records the person's OK and sends or writes nothing.

import { Check, ChevronRight } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { useProblem } from "@/components/ui/notice";
import { PreparedChange } from "@/components/prepared-change";
import { TodoSteps } from "@/components/autopilot/todo";
import { decideAssistPiece, getAssistJobs, prepareAssistChange, requestAssistJob, stopAssistJob, type AssistJob, type AssistPiece } from "@/lib/api";
import { operationLabel } from "@/lib/autopilot";
import { cn } from "@/lib/cn";
import { ApiError, plainError } from "@/lib/errors";
import { countedWords, formatDateTime } from "@/lib/format";

const SYSTEMS: Record<string, string> = { bc: "Business Central", portal: "Portal", mc: "Mission Control", planner: "Planner", calendar: "Calendar", sharepoint: "SharePoint", mail: "Mail", teams: "Teams" };
const LIVE = new Set(["queued", "running", "stopping"]);

const EXAMPLE_JOBS = [
  "Chase every late PO from DSM",
  "Check shipped orders not yet invoiced",
  "Summarize open QC holds and affected orders",
  "Review overdue AP and AR accounts",
];

function formatJobElapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return rem ? `${m}m ${rem}s` : `${m}m`;
}

export type JobBadgeInfo = {
  label: string;
  tone: "ok" | "risk" | "late" | "new" | "neutral";
};

/** Honest status badge: never calls a job "Done" when pieces failed or context was withheld. */
export function jobBadgeOf(job: AssistJob): JobBadgeInfo {
  if (job.status === "queued") return { label: "Waiting to start", tone: "neutral" };
  if (job.status === "running") return { label: "Working", tone: "new" };
  if (job.status === "stopping") return { label: "Stopping", tone: "neutral" };
  if (job.status === "stopped") return { label: "Stopped", tone: "neutral" };
  if (job.status === "failed") return { label: "Could not be done", tone: "late" };

  const hasProblemPieces = job.pieces.some((p) => p.status !== "prepared" && p.status !== "nothing");
  const hasWithheld = job.unread.length > 0 || job.unread_reads.length > 0;
  if (hasProblemPieces || hasWithheld) {
    return { label: "Partial · Needs context", tone: "risk" };
  }
  const hasPendingPieces = job.pieces.some((p) => p.status === "prepared" && !p.decision);
  if (hasPendingPieces) {
    return { label: "Ready to review", tone: "new" };
  }
  return { label: "Completed", tone: "ok" };
}

/** Quiet inline text color for status labels; never filled pill boxes. */
export function badgeClass(tone: JobBadgeInfo["tone"]): string {
  if (tone === "ok") return "text-ok";
  if (tone === "risk") return "text-risk/90";
  if (tone === "late") return "text-late/90";
  if (tone === "new") return "text-accent";
  return "text-muted";
}

/** The jobs, read again every few seconds while one is working. */
export function useAssistJobs() {
  const [jobs, setJobs] = useState<AssistJob[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const alive = useRef(true);
  const load = useCallback(async () => {
    try {
      const got = await getAssistJobs();
      if (alive.current) {
        setJobs(got.jobs);
        setError(got.status === "not_run" ? got.reason : null);
        setForbidden(false);
      }
    } catch (e) {
      if (alive.current) {
        setError(plainError(e));
        setForbidden(e instanceof ApiError && e.status === 403);
      }
    }
  }, []);
  useEffect(() => {
    alive.current = true;
    void load();
    return () => {
      alive.current = false;
    };
  }, [load]);
  const working = (jobs ?? []).some((j) => LIVE.has(j.status));
  useEffect(() => {
    if (!working) return;
    const t = setInterval(() => void load(), 4000);
    return () => clearInterval(t);
  }, [working, load]);
  const replace = useCallback((job: AssistJob) => setJobs((all) => [job, ...(all ?? []).filter((j) => j.job_id !== job.job_id)].sort((a, b) => b.requested_at.localeCompare(a.requested_at))), []);
  return { jobs, error, forbidden, reload: load, replace };
}

export function GiveJob({
  onGiven,
  switchesLink,
  className,
}: {
  onGiven: (job: AssistJob) => void;
  switchesLink?: React.ReactNode;
  className?: string;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [, setError, problem] = useProblem();
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
  }, [text]);

  const give = async () => {
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    try {
      onGiven(await requestAssistJob(text.trim()));
      setText("");
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      className={cn("w-full border-b border-border bg-bg px-4 py-3 lg:px-5", className)}
      aria-label="Give Assist a job"
      data-testid="give-job"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 pb-2">
        <label htmlFor="give-job-text" className="text-[12.5px] font-medium text-fg">
          Give Assist a job
        </label>
        <div className="flex items-center gap-3 text-[12px] text-faint">
          <span>Nothing is sent or changed until you confirm it.</span>
          {switchesLink}
        </div>
      </div>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
        <textarea
          ref={inputRef}
          id="give-job-text"
          value={text}
          rows={1}
          disabled={busy}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              void give();
            }
          }}
          placeholder="For example: chase every late PO from DSM"
          className="min-h-[34px] max-h-[120px] flex-1 resize-none rounded-md border border-border bg-surface/50 px-3 py-1.5 text-[13.5px] leading-5 text-fg placeholder:text-faint focus:border-border-strong focus:bg-bg focus:outline-none"
        />
        <Button size="sm" variant="outline" className="shrink-0" disabled={busy || !text.trim()} onClick={() => void give()}>
          {busy ? "Starting..." : "Start the job"}
        </Button>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11.5px] text-faint" aria-label="Example jobs">
        <span>Try:</span>
        {EXAMPLE_JOBS.map((example, idx) => (
          <span key={example} className="inline-flex items-center gap-2">
            {idx > 0 && <span aria-hidden>·</span>}
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setText(example);
                inputRef.current?.focus();
              }}
              className="text-muted underline-offset-2 hover:text-fg hover:underline cursor-pointer"
            >
              {example}
            </button>
          </span>
        ))}
      </div>
      {problem}
    </section>
  );
}

function ReadsNote({ piece }: { piece: AssistPiece }) {
  const calls = piece.tool_reads.length;
  return (
    <details className="text-[12px] text-muted" data-testid="piece-reads">
      <summary className="cursor-pointer select-none hover:text-fg">
        Prepared from {piece.reads.length} {piece.reads.length === 1 ? "record" : "records"} opened and {calls} {calls === 1 ? "lookup" : "lookups"}
      </summary>
      <div className="mt-2 space-y-1 border-l-2 border-border pl-3 py-0.5">
        {!piece.reads_traced && <p>The lookups of this piece could not be traced.</p>}
        {piece.reads.length > 0 && <p className="break-all">{piece.reads.join(", ")}</p>}
        {piece.tool_reads.length > 0 && (
          <ul className="list-disc pl-4">
            {piece.tool_reads.map((c, i) => (
              <li key={i} className="break-all">
                {c.tool}
                {c.failed ? " (failed)" : ""} {typeof c.arguments === "string" ? c.arguments : JSON.stringify(c.arguments ?? {})}
              </li>
            ))}
          </ul>
        )}
      </div>
    </details>
  );
}

const DESK_CHANGE_LIMIT = 4;

function Change({ job, piece, index, body }: { job: AssistJob; piece: AssistPiece; index: number; body?: string }) {
  const p = piece.proposals[index];
  const a = p.arguments;
  const [pid, setPid] = useState<string | null>(piece.prepared_ids[String(index)] ?? null);
  const [busy, setBusy] = useState(false);
  const [, setError, problem] = useProblem();
  const overLimit = index >= DESK_CHANGE_LIMIT;
  const review = async () => {
    setBusy(true);
    setError(null);
    try {
      setPid((await prepareAssistChange(job.job_id, piece.n, index)).prepared_id);
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const change = a.change ?? {};
  const to = Array.isArray(change.to) ? (change.to as string[]).join(", ") : "";
  return (
    <li className="space-y-1.5 py-2.5" data-testid="piece-change">
      <p className="text-[13px] text-fg">
        <span className="font-medium">{SYSTEMS[a.system ?? ""] ?? a.system}</span> · {operationLabel(a.operation ?? "")}
        {to ? <span className="text-muted"> · to {to}</span> : null}
      </p>
      {a.reason && <p className="text-[12px] text-muted">{a.reason}</p>}
      {typeof change.subject === "string" && <p className="text-[12.5px] font-medium text-fg">{change.subject}</p>}
      {(body ?? (typeof change.body === "string" ? change.body : "")) && (
        <p className="whitespace-pre-wrap border-l-2 border-border-strong pl-3 py-0.5 text-[13px] leading-relaxed text-fg">
          {body ?? String(change.body)}
        </p>
      )}
      {pid ? (
        <PreparedChange id={pid} />
      ) : overLimit ? (
        <p className="text-[12px] text-muted">Past the {DESK_CHANGE_LIMIT}-change desk limit for one piece.</p>
      ) : (
        <Button size="sm" variant="outline" disabled={busy} onClick={() => void review()}>
          Review this change
        </Button>
      )}
      {problem}
    </li>
  );
}

export function PieceCard({
  job,
  piece,
  onJob,
  onFeedback,
}: {
  job: AssistJob;
  piece: AssistPiece;
  onJob: (j: AssistJob) => void;
  onFeedback?: (message: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [subjectEdits, setSubjectEdits] = useState<Record<string, string>>({});
  const [text, setText] = useState(piece.edited_text || piece.reply);
  const [busy, setBusy] = useState(false);
  const [, setError, problem] = useProblem();
  const decide = async (decision: "approved" | "dismissed" | "edited") => {
    const out: Record<string, Record<string, string>> = {};
    for (const [i, body] of Object.entries(edits)) {
      out[i] = { ...(out[i] ?? {}), body };
    }
    for (const [i, subject] of Object.entries(subjectEdits)) {
      out[i] = { ...(out[i] ?? {}), subject };
    }
    if (decision === "edited") {
      const hasProposalEdits = Object.keys(out).length > 0;
      const hasText = piece.proposals.length === 0 && text.trim() !== "";
      if (!hasProposalEdits && !hasText) {
        setEditing(false);
        return;
      }
    }
    setBusy(true);
    setError(null);
    const prevJob = job;
    const optimisticJob: AssistJob = {
      ...job,
      pieces: job.pieces.map((p) =>
        p.n === piece.n
          ? {
              ...p,
              decision,
              ...(decision === "edited" && piece.proposals.length === 0 ? { edited_text: text } : {}),
            }
          : p,
      ),
    };
    onJob(optimisticJob);
    setEditing(false);
    const label = decision === "approved" ? "Approved" : decision === "edited" ? "Saved edit" : "Dismissed";
    onFeedback?.(label);
    try {
      onJob(
        await decideAssistPiece(job.job_id, piece.n, {
          decision,
          ...(decision === "edited" ? { text: piece.proposals.length === 0 ? text : "", edits: out } : {}),
        }),
      );
    } catch (e) {
      onJob(prevJob);
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const settled = piece.decision === "approved" ? "Approved" : piece.decision === "edited" ? "Edited and approved" : piece.decision === "dismissed" ? "Not needed" : "";
  const bodyOf = (i: number) => {
    const b = piece.proposals[i].arguments.change?.body;
    return typeof b === "string" ? b : "";
  };
  const subjectOf = (i: number) => {
    const s = piece.proposals[i].arguments.change?.subject;
    return typeof s === "string" ? s : "";
  };
  const copyableText = () => {
    if (piece.edited_text || piece.reply) return piece.edited_text || piece.reply;
    const bodies = piece.proposals
      .map((_, i) => {
        const b = edits[String(i)] ?? bodyOf(i);
        const s = subjectEdits[String(i)] ?? subjectOf(i);
        if (!b) return "";
        return s ? `Subject: ${s}\n\n${b}` : b;
      })
      .filter(Boolean);
    return bodies.join("\n\n---\n\n");
  };
  const copyDraft = async () => {
    const content = copyableText();
    if (!content) return;
    setError(null);
    try {
      await navigator.clipboard.writeText(content);
      onFeedback?.("Copied draft to clipboard");
    } catch (e) {
      setError(plainError(e));
    }
  };
  return (
    <li className="space-y-2.5 py-3.5 first:pt-1" data-testid="job-piece" id={`piece-${job.job_id}-${piece.n}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-[14px] font-medium text-fg">{piece.title}</span>
        {settled && (
          <span className="text-[12px] font-medium text-faint">
            {settled}
          </span>
        )}
      </div>
      {piece.status === "prepared" ? (
        <>
          {piece.summary && <p className="text-[13.5px] leading-relaxed text-fg/90">{countedWords(piece.summary)}</p>}
          {piece.warning && (
            <p className="border-l-2 border-l-risk/60 pl-3 py-0.5 text-[12.5px] leading-5 text-muted">
              {piece.warning}
            </p>
          )}
          {piece.reply && !editing && (
            <p className="whitespace-pre-wrap border-l-2 border-border-strong pl-3 py-0.5 text-[13.5px] leading-relaxed text-fg">
              {piece.edited_text || piece.reply}
            </p>
          )}
          {editing && piece.proposals.length === 0 && (
            <textarea
              aria-label="Your wording"
              value={text}
              rows={6}
              onChange={(e) => setText(e.target.value)}
              className="w-full resize-none rounded-md border border-border-strong bg-bg px-3 py-2 text-[13.5px] leading-relaxed text-fg focus:border-accent focus:outline-none"
            />
          )}
          {piece.ask?.question && (
            <div className="space-y-1 border-l-2 border-border-strong pl-3 py-0.5">
              <p className="text-[13px] font-medium text-fg">{countedWords(piece.ask.question)}</p>
              <ul className="list-disc space-y-0.5 pl-4 text-[12.5px] text-muted">
                {(piece.ask.options ?? []).map((o, i) => (
                  <li key={i}>
                    <span className="text-fg">{o.label}</span>
                    {o.reply ? `: ${o.reply}` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {piece.proposals.length > 0 && (
            <>
              {(piece.changes_over_limit ?? Math.max(0, piece.proposals.length - DESK_CHANGE_LIMIT)) > 0 && (
                <p className="border-l-2 border-l-risk/60 pl-3 py-0.5 text-[12px] text-muted" data-testid="piece-changes-over-limit">
                  This piece holds {piece.proposals.length} proposed changes. The desk prepares at most {DESK_CHANGE_LIMIT} per piece ({piece.changes_over_limit ?? piece.proposals.length - DESK_CHANGE_LIMIT} more are shown below for reference).
                </p>
              )}
              <ul className="divide-y divide-border border-t border-border" aria-label="Changes to review">
                {piece.proposals.map((_, i) =>
                  editing && bodyOf(i) !== "" ? (
                    <li key={i} className="space-y-2 py-2.5">
                      {subjectOf(i) !== "" && (
                        <input
                          aria-label={`Subject ${i + 1}`}
                          value={subjectEdits[String(i)] ?? subjectOf(i)}
                          onChange={(e) => setSubjectEdits((s) => ({ ...s, [String(i)]: e.target.value }))}
                          className="h-8 w-full rounded-md border border-border-strong bg-bg px-3 text-[13px] font-medium text-fg focus:border-accent focus:outline-none"
                        />
                      )}
                      <textarea
                        aria-label={`Message ${i + 1}`}
                        value={edits[String(i)] ?? bodyOf(i)}
                        rows={6}
                        onChange={(e) => setEdits((s) => ({ ...s, [String(i)]: e.target.value }))}
                        className="w-full resize-none rounded-md border border-border-strong bg-bg px-3 py-2 text-[13px] leading-relaxed text-fg focus:border-accent focus:outline-none"
                      />
                    </li>
                  ) : (
                    <Change key={i} job={job} piece={piece} index={i} />
                  ),
                )}
              </ul>
            </>
          )}
          {piece.held_back.length > 0 && (
            <ul className="space-y-1 border-l-2 border-l-risk/60 pl-3 py-0.5 text-[12px] text-muted">
              {piece.held_back.map((h, i) => (
                <li key={i}>A message to {(h.to ?? []).join(", ")} was held back: {h.why}. Write it yourself.</li>
              ))}
            </ul>
          )}
          <TodoSteps todos={(piece.todos ?? []).map((t) => ({ system: t.system ?? "", record: t.record ?? "", what: t.what ?? "" }))} />
          {piece.note && <p className="text-[12px] text-muted">{piece.note}</p>}
        </>
      ) : piece.status === "nothing" ? (
        <p className="text-[13px] text-muted">{piece.nothing ? `Nothing to do: ${piece.nothing}` : "Nothing to prepare for this piece."}</p>
      ) : (
        <p className="border-l-2 border-l-risk/60 pl-3 py-0.5 text-[13px] leading-relaxed text-muted">
          {piece.status === "stopped"
            ? "Not started: the job was stopped."
            : piece.status === "screened"
              ? "Not prepared: it names a person or subject that may not go to the model."
              : "Could not be prepared."}{" "}
          {piece.message}
        </p>
      )}
      {piece.records_unread.length > 0 && (
        <p className="border-l-2 border-l-risk/60 pl-3 py-0.5 text-[12px] text-muted">
          The plan named {piece.records_unread.join(", ")}, but nothing read for this piece shows it. Check before relying on it.
        </p>
      )}
      {piece.status === "prepared" && <ReadsNote piece={piece} />}
      {piece.status === "prepared" && !settled && (
        <div className="flex flex-wrap items-center gap-2 pt-1" data-testid="piece-actions">
          {editing ? (
            <>
              <Button size="sm" disabled={busy} onClick={() => void decide("edited")}>
                Save my edit
              </Button>
              <Button size="sm" variant="outline" disabled={busy} onClick={() => setEditing(false)}>
                Cancel
              </Button>
            </>
          ) : (
            <>
              <Button size="sm" disabled={busy} onClick={() => void decide("approved")}>
                Approve
              </Button>
              {(piece.reply || piece.proposals.some((_, i) => bodyOf(i) !== "")) && (
                <>
                  <Button size="sm" variant="outline" disabled={busy} onClick={() => setEditing(true)}>
                    Edit
                  </Button>
                  <Button size="sm" variant="outline" disabled={busy} onClick={() => void copyDraft()}>
                    Copy
                  </Button>
                </>
              )}
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => void decide("dismissed")}>
                Not needed
              </Button>
            </>
          )}
        </div>
      )}
      {piece.status !== "prepared" && !settled && (
        <div className="flex flex-wrap items-center gap-2 pt-1" data-testid="piece-actions">
          <Button size="sm" variant="outline" disabled={busy} onClick={() => void decide("dismissed")}>
            Dismiss
          </Button>
        </div>
      )}
      {problem}
    </li>
  );
}

function InteractiveChecklist({ items, jobId }: { items: AssistJob["checklist"]; jobId: string }) {
  const [checked, setChecked] = useState<Record<number, boolean>>({});
  useEffect(() => {
    setChecked({});
  }, [jobId]);
  if (items.length === 0) return null;
  const doneCount = Object.values(checked).filter(Boolean).length;
  return (
    <section className="border-t border-border pt-4" data-testid="job-checklist">
      <div className="mb-2.5 flex items-center justify-between gap-2">
        <h4 className="text-[11.5px] font-medium uppercase tracking-[0.04em] text-faint">Checklist</h4>
        <span className="num text-[11.5px] text-faint">
          {doneCount} of {items.length} checked
        </span>
      </div>
      <ul className="space-y-2">
        {items.map((c, i) => {
          const isDone = !!checked[i];
          return (
            <li key={i}>
              <label className="flex cursor-pointer items-start gap-2.5 text-[13.5px] leading-relaxed">
                <button
                  type="button"
                  role="checkbox"
                  aria-checked={isDone}
                  onClick={() => setChecked((cur) => ({ ...cur, [i]: !cur[i] }))}
                  className={cn(
                    "mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-sm border transition-colors cursor-pointer",
                    isDone ? "border-accent bg-accent text-accent-fg" : "border-border-strong bg-bg hover:border-accent",
                  )}
                >
                  {isDone && <Check className="size-3" strokeWidth={2.5} aria-hidden />}
                </button>
                <span className={cn("min-w-0 flex-1", isDone ? "text-faint line-through" : "text-fg")}>
                  {c.text}
                </span>
              </label>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function JobCard({
  job,
  onJob,
  onFeedback,
  className,
}: {
  job: AssistJob;
  onJob: (j: AssistJob) => void;
  onFeedback?: (message: string) => void;
  className?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [, setError, problem] = useProblem();
  const isLive = LIVE.has(job.status);
  const [elapsedSec, setElapsedSec] = useState<number>(() => {
    const parsed = Date.parse(job.requested_at ?? "");
    return Number.isNaN(parsed) ? 0 : Math.max(0, Math.floor((Date.now() - parsed) / 1000));
  });
  useEffect(() => {
    if (!isLive) return;
    const parsed = Date.parse(job.requested_at ?? "");
    const start = Number.isNaN(parsed) ? Date.now() : parsed;
    setElapsedSec(Math.max(0, Math.floor((Date.now() - start) / 1000)));
    const tick = setInterval(() => setElapsedSec(Math.max(0, Math.floor((Date.now() - start) / 1000))), 1000);
    return () => clearInterval(tick);
  }, [isLive, job.requested_at]);
  const stop = async () => {
    setBusy(true);
    setError(null);
    try {
      onJob(await stopAssistJob(job.job_id));
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const done = job.pieces.length;
  const badge = jobBadgeOf(job);
  const hasCheckedDetails = Boolean(job.found || job.not_found || typeof job.usd === "number" || job.fallbacks.length > 0);
  const hasWarnings = job.unread.length > 0 || job.unread_reads.length > 0;

  return (
    <article
      className={cn("w-full space-y-5", className)}
      data-testid="assist-job"
      aria-label={job.text}
    >
      <header className="space-y-1.5 border-b border-border pb-4">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <h3 className="min-w-0 flex-1 text-[17px] font-semibold leading-6 tracking-[-0.015em] text-fg">{job.text}</h3>
          <span className="num shrink-0 text-[12px] text-faint">{formatDateTime(job.requested_at)}</span>
        </div>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[12.5px]" data-testid="job-status">
          <span className={cn("font-medium", badgeClass(badge.tone))}>
            {badge.label}
            {isLive && job.status !== "stopping" ? ` (${formatJobElapsed(elapsedSec)})` : ""}
          </span>
          {job.planned > 0 && (
            <>
              <span className="text-faint" aria-hidden>·</span>
              <span className="num text-muted">
                {done} of {job.planned} pieces ready
              </span>
            </>
          )}
          {job.progress && LIVE.has(job.status) && (
            <>
              <span className="text-faint" aria-hidden>·</span>
              <span className="text-muted">{job.progress}</span>
            </>
          )}
          {LIVE.has(job.status) && (
            <Button size="xs" variant="outline" className="ml-auto" disabled={busy || job.status === "stopping"} onClick={() => void stop()}>
              {job.status === "stopping" ? "Stopping…" : "Stop"}
            </Button>
          )}
        </div>
      </header>

      {job.status === "failed" && (
        <p className="border-l-2 border-l-late/75 pl-3 py-0.5 text-[13px] text-fg">
          {job.message || "The job could not be done."} Nothing was sent or changed.
        </p>
      )}

      {job.summary && (
        <section className="space-y-1.5">
          <h4 className="text-[11.5px] font-medium uppercase tracking-[0.04em] text-faint">Summary</h4>
          <p className="text-[14px] leading-relaxed text-fg">{countedWords(job.summary)}</p>
        </section>
      )}

      {job.nothing && <p className="text-[13px] text-muted">Nothing to do: {job.nothing}</p>}

      {hasWarnings && (
        <div className="space-y-1 border-l-2 border-l-risk/60 pl-3 py-0.5 text-[12.5px] leading-5 text-muted">
          {job.unread.length > 0 && (
            <p>Sources that could not be read just now: {job.unread.join("; ")}.</p>
          )}
          {job.unread_reads.length > 0 && (
            <p>
              {job.unread_reads.length} {job.unread_reads.length === 1 ? "record" : "records"} read for the plan could not be used ({Array.from(new Set(job.unread_reads.map((r) => r.why ?? "withheld"))).join(", ")}); the plan stands on the others.
            </p>
          )}
        </div>
      )}

      {job.pieces.length > 0 && (
        <section className="border-t border-border pt-4">
          <h4 className="mb-2 text-[11.5px] font-medium uppercase tracking-[0.04em] text-faint">Prepared actions</h4>
          <ul className="divide-y divide-border" aria-label="Pieces of the job">
            {job.pieces.map((p) => (
              <PieceCard key={p.n} job={job} piece={p} onJob={onJob} onFeedback={onFeedback} />
            ))}
          </ul>
        </section>
      )}

      <InteractiveChecklist items={job.checklist} jobId={job.job_id} />

      {hasCheckedDetails && (
        <details className="group border-t border-border pt-3 text-[12px] text-muted" data-testid="job-checked-details">
          <summary className="flex cursor-pointer list-none items-center gap-1.5 font-medium text-muted hover:text-fg">
            <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" aria-hidden />
            Sources and checks
          </summary>
          <div className="mt-2 space-y-1.5 pl-5 leading-relaxed">
            {job.found && <p><span className="font-medium text-fg">Looked at:</span> {job.found}</p>}
            {job.not_found && <p><span className="font-medium text-fg">Not found or not readable:</span> {job.not_found}</p>}
            {job.fallbacks.map((f, i) => (
              <p key={i}>{f}</p>
            ))}
            {typeof job.usd === "number" && (
              <p className="num text-faint">${job.usd.toFixed(2)} of model use</p>
            )}
          </div>
        </details>
      )}
      {problem}
    </article>
  );
}

/** The box and the person's jobs, newest first. */
export function JobsView() {
  const { jobs, error, forbidden, replace } = useAssistJobs();
  return (
    <div data-testid="assist-jobs" className="space-y-4">
      <GiveJob onGiven={replace} />
      {error && !forbidden && <p className="text-[13px] text-muted">{error}</p>}
      {(jobs ?? []).length > 0 && (
        <div className="divide-y divide-border space-y-6">
          {(jobs ?? []).map((j) => (
            <JobCard key={j.job_id} job={j} onJob={replace} className="pt-4 first:pt-0" />
          ))}
        </div>
      )}
    </div>
  );
}
