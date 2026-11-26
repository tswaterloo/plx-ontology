"use client";

// A whole job handed to Assist: the composer, its progress, and the pieces it prepared, each one
// approved, edited or set aside by the person. A change in a system is reviewed on its own card
// and confirmed in two clicks there; approving a piece records the person's OK and sends or writes nothing.

import { AlertTriangle, ArrowUp, Ban, Check, CheckCircle2, ChevronDown, ChevronRight, CircleAlert, Clock3, Copy, FileText, Loader2, Mail, MessageSquare, Pencil, Sparkles, Wrench } from "lucide-react";
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

/** Honest status label and semantic tone: never calls a job Done when pieces failed or context was withheld. */
export function jobBadgeOf(job: AssistJob): JobBadgeInfo {
  if (job.status === "queued") return { label: "Waiting to start", tone: "neutral" };
  if (job.status === "running") return { label: "Working", tone: "new" };
  if (job.status === "stopping") return { label: "Stopping", tone: "neutral" };
  if (job.status === "stopped") return { label: "Stopped", tone: "neutral" };
  if (job.status === "failed") return { label: "Blocked", tone: "late" };

  const pieces = job.pieces ?? [];
  const hasProblemPieces = pieces.some((p) => p.status !== "prepared" && p.status !== "nothing");
  const hasWithheld = (job.unread ?? []).length > 0 || (job.unread_reads ?? []).length > 0;
  if (hasProblemPieces || hasWithheld) {
    return { label: "Needs context", tone: "risk" };
  }
  const hasPendingPieces = pieces.some((p) => p.status === "prepared" && !p.decision);
  if (hasPendingPieces) {
    return { label: "Ready", tone: "ok" };
  }
  return { label: "Done", tone: "ok" };
}

/** Quiet typographic status tone classes (zero background boxes, zero borders). */
export function badgeClass(tone: JobBadgeInfo["tone"]): string {
  if (tone === "ok") return "text-fg/80";
  if (tone === "risk") return "text-risk";
  if (tone === "late") return "text-late";
  if (tone === "new") return "text-new";
  return "text-muted";
}

function statusDotClass(tone: JobBadgeInfo["tone"]): string {
  if (tone === "ok") return "bg-dot-ok";
  if (tone === "risk") return "bg-dot-risk";
  if (tone === "late") return "bg-dot-late";
  if (tone === "new") return "bg-new";
  return "bg-dot-neutral";
}

/** Crisp 12px Lucide status icon matching each semantic tone. */
export function StatusToneIcon({ tone, className }: { tone: JobBadgeInfo["tone"]; className?: string }) {
  const cls = cn("size-3 shrink-0", className);
  if (tone === "ok") return <CheckCircle2 className={cls} strokeWidth={2} aria-hidden />;
  if (tone === "risk") return <AlertTriangle className={cls} strokeWidth={2} aria-hidden />;
  if (tone === "late") return <CircleAlert className={cls} strokeWidth={2} aria-hidden />;
  if (tone === "new") return <Loader2 className={cn(cls, "animate-spin")} strokeWidth={2} aria-hidden />;
  return <Clock3 className={cls} strokeWidth={2} aria-hidden />;
}

/** Quiet, box-free typographic status indicator: tiny 6px dot (or spinner) + calm 12px label. */
export function StatusIndicator({
  tone,
  label,
  suffix,
  className,
}: {
  tone: JobBadgeInfo["tone"];
  label: string;
  suffix?: string;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-[12px] font-medium leading-4 whitespace-nowrap",
        badgeClass(tone),
        className,
      )}
    >
      {tone === "new" ? (
        <Loader2 className="size-3 shrink-0 animate-spin text-new" strokeWidth={2} aria-hidden />
      ) : (
        <span className={cn("size-1.5 shrink-0 rounded-full", statusDotClass(tone))} aria-hidden />
      )}
      <span>
        {label}
        {suffix ?? ""}
      </span>
    </span>
  );
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
  autoFocus,
  compact,
  className,
}: {
  onGiven: (job: AssistJob) => void;
  switchesLink?: React.ReactNode;
  autoFocus?: boolean;
  compact?: boolean;
  className?: string;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [examplesOpen, setExamplesOpen] = useState(false);
  const [, setError, problem] = useProblem();
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (autoFocus) inputRef.current?.focus();
  }, [autoFocus]);

  useEffect(() => {
    if (!examplesOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) {
        setExamplesOpen(false);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [examplesOpen]);

  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 112)}px`;
  }, [text]);

  const give = async () => {
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    setExamplesOpen(false);
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
      className={cn(
        "w-full border-b border-border bg-bg-subtle/60 px-4 lg:px-5",
        compact ? "py-2" : "py-2.5",
        className,
      )}
      aria-label="Give Assist a job"
      data-testid="give-job"
    >
      <div ref={boxRef} className="relative mx-auto w-full max-w-[1360px] space-y-1.5">
        <div className="flex items-end gap-2 border-b border-border-strong pb-1.5 transition-colors focus-within:border-accent/70">
          <label htmlFor="give-job-text" className="sr-only">
            Give Assist a job
          </label>
          <textarea
            ref={inputRef}
            id="give-job-text"
            value={text}
            rows={1}
            disabled={busy}
            onFocus={() => {
              if (!text.trim()) setExamplesOpen(true);
            }}
            onChange={(e) => {
              setText(e.target.value);
              if (e.target.value.trim()) setExamplesOpen(false);
            }}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                setExamplesOpen(false);
                return;
              }
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void give();
              }
            }}
            placeholder="Describe a job for Assist (e.g. chase every late PO from DSM)..."
            className="min-h-[26px] max-h-[112px] flex-1 resize-none bg-transparent py-1 text-[13.5px] leading-[1.45] text-fg placeholder:text-faint focus:outline-none"
          />
          <button
            type="button"
            aria-expanded={examplesOpen}
            aria-label="Example jobs"
            onClick={() => setExamplesOpen((v) => !v)}
            className="inline-flex h-7 shrink-0 items-center gap-1 rounded-md px-2 text-[12px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg cursor-pointer"
          >
            <Sparkles className="size-3.5" aria-hidden />
            <span className="max-sm:hidden">Examples</span>
            <ChevronDown className={cn("size-3 transition-transform", examplesOpen && "rotate-180")} aria-hidden />
          </button>
          <button
            type="button"
            disabled={busy || !text.trim()}
            onClick={() => void give()}
            className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md bg-accent px-3 text-[12.5px] font-medium text-accent-fg transition-all hover:opacity-90 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40 cursor-pointer"
          >
            <span>{busy ? "Starting..." : "Start the job"}</span>
            {!busy && <ArrowUp className="size-3.5" aria-hidden />}
          </button>
        </div>

        {examplesOpen && !text.trim() && (
          <div
            role="region"
            aria-label="Example jobs"
            data-testid="give-job-examples"
            className="liquid-glass-surface absolute left-0 right-0 top-full z-30 mt-1.5 rounded-lg p-1.5"
          >
            <p className="px-2 pb-1 text-[11px] font-medium text-faint">
              Examples
            </p>
            <ul className="space-y-0.5">
              {EXAMPLE_JOBS.map((example) => (
                <li key={example}>
                  <button
                    type="button"
                    disabled={busy}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => {
                      setText(example);
                      setExamplesOpen(false);
                      inputRef.current?.focus();
                    }}
                    className="flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-[13px] text-fg transition-colors hover:bg-hover cursor-pointer"
                  >
                    <span>{example}</span>
                    <ArrowUp className="size-3.5 shrink-0 text-faint" aria-hidden />
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        {(!compact || switchesLink) && (
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-[11.5px] text-faint">
            <span>Nothing is sent or changed until you confirm it.</span>
            {switchesLink}
          </div>
        )}
        {problem}
      </div>
    </section>
  );
}

function ReadsNote({ piece }: { piece: AssistPiece }) {
  const toolReads = piece.tool_reads ?? [];
  const reads = piece.reads ?? [];
  const calls = toolReads.length;
  if (reads.length === 0 && calls === 0) return null;
  return (
    <details className="group text-[12.5px] text-muted" data-testid="piece-reads">
      <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 select-none hover:text-fg">
        <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" aria-hidden />
        Prepared from {reads.length} {reads.length === 1 ? "record" : "records"} opened and {calls} {calls === 1 ? "lookup" : "lookups"}
      </summary>
      <div className="mt-2 space-y-1.5 border-l border-border pl-3.5 py-0.5 leading-relaxed">
        {!piece.reads_traced && <p>The lookups of this piece could not be traced.</p>}
        {reads.length > 0 && <p className="break-all text-fg/85">{reads.join(", ")}</p>}
        {toolReads.length > 0 && (
          <ul className="list-disc space-y-0.5 pl-4">
            {toolReads.map((c, i) => (
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

function ProposalIcon({ system }: { system?: string }) {
  const cls = "size-4 shrink-0 text-muted";
  if (system === "mail") return <Mail className={cls} aria-hidden />;
  if (system === "teams") return <MessageSquare className={cls} aria-hidden />;
  if (system === "sharepoint") return <FileText className={cls} aria-hidden />;
  return <Wrench className={cls} aria-hidden />;
}

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
  const subject = typeof change.subject === "string" ? change.subject : "";
  const draftBody = body ?? (typeof change.body === "string" ? change.body : "");
  const extraFields = Object.entries(change).filter(([k]) => k !== "to" && k !== "cc" && k !== "subject" && k !== "body");

  return (
    <li
      className="space-y-3 border-l-2 border-border-strong py-1 pl-4"
      data-testid="piece-change"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2 text-[13px] text-fg">
          <ProposalIcon system={a.system} />
          <span className="font-medium">{SYSTEMS[a.system ?? ""] ?? a.system}</span>
          <span className="h-3 w-px bg-border" aria-hidden />
          <span className="text-muted">{operationLabel(a.operation ?? "")}</span>
        </div>
        {!pid && !overLimit && (
          <Button
            size="sm"
            variant="ghost"
            className="h-7 rounded-lg px-2.5 text-[12.5px] font-medium text-fg hover:bg-surface-2"
            disabled={busy}
            onClick={() => void review()}
          >
            {busy ? "Preparing..." : "Review this change"}
          </Button>
        )}
      </div>

      {a.reason && <p className="text-[13px] leading-relaxed text-muted">{a.reason}</p>}

      {(to || subject) && (
        <div className="space-y-1.5 border-y border-border/80 py-2.5 text-[13px]">
          {to && (
            <div className="flex items-baseline gap-3">
              <span className="w-14 shrink-0 text-[12px] font-medium text-muted">To</span>
              <span className="min-w-0 flex-1 break-words text-fg">{to}</span>
            </div>
          )}
          {subject && (
            <div className="flex items-baseline gap-3">
              <span className="w-14 shrink-0 text-[12px] font-medium text-muted">Subject</span>
              <span className="min-w-0 flex-1 font-medium text-fg">{subject}</span>
            </div>
          )}
        </div>
      )}

      {draftBody && (
        <p className="whitespace-pre-wrap text-[14.5px] leading-[1.65] text-fg">
          {draftBody}
        </p>
      )}

      {extraFields.length > 0 && !draftBody && (
        <dl className="grid grid-cols-1 gap-1.5 text-[13px] sm:grid-cols-2">
          {extraFields.map(([k, v]) => (
            <div key={k} className="flex items-baseline gap-2">
              <dt className="text-[12px] text-muted">{k}:</dt>
              <dd className="min-w-0 break-words text-fg">{typeof v === "string" ? v : JSON.stringify(v)}</dd>
            </div>
          ))}
        </dl>
      )}

      {pid ? (
        <PreparedChange id={pid} />
      ) : overLimit ? (
        <p className="text-[12.5px] text-muted">Past the {DESK_CHANGE_LIMIT}-change desk limit for one piece.</p>
      ) : null}
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
  const replyText =
    typeof piece.reply === "string"
      ? piece.reply
      : piece.reply && typeof piece.reply === "object" && "body" in piece.reply && typeof (piece.reply as { body?: unknown }).body === "string"
        ? (piece.reply as { body: string }).body
        : "";
  const [text, setText] = useState(piece.edited_text || replyText);
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
    const proposals = piece.proposals ?? [];
    if (decision === "edited") {
      const hasProposalEdits = Object.keys(out).length > 0;
      const hasText = proposals.length === 0 && text.trim() !== "";
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
      pieces: (job.pieces ?? []).map((p) =>
        p.n === piece.n
          ? {
              ...p,
              decision,
              ...(decision === "edited" && proposals.length === 0 ? { edited_text: text } : {}),
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
          ...(decision === "edited" ? { text: proposals.length === 0 ? text : "", edits: out } : {}),
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
    if (piece.edited_text || replyText) return piece.edited_text || replyText;
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
  const hasCopyable = Boolean(replyText || (piece.proposals ?? []).some((_, i) => bodyOf(i) !== ""));

  return (
    <li className="space-y-3.5 py-5 first:pt-2" data-testid="job-piece" id={`piece-${job.job_id}-${piece.n}`}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-1 items-center gap-2.5">
          <span className="text-[15px] font-semibold tracking-tight text-fg">{piece.title}</span>
          {settled && (
            <span
              className={cn(
                "inline-flex items-center gap-1.5 text-[12px] font-medium",
                piece.decision === "dismissed" ? "text-muted" : "text-ok",
              )}
            >
              {piece.decision === "dismissed" ? (
                <Ban className="size-3 shrink-0" aria-hidden />
              ) : (
                <CheckCircle2 className="size-3 shrink-0" aria-hidden />
              )}
              <span>{settled}</span>
            </span>
          )}
        </div>

        {piece.status === "prepared" && !settled && (
          <div className="flex flex-wrap items-center gap-1" data-testid="piece-actions">
            {editing ? (
              <>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void decide("edited")}
                  className="inline-flex h-8 items-center rounded-lg bg-fg px-3 text-[13px] font-medium text-bg transition-all hover:opacity-90 active:scale-[0.98] disabled:opacity-50 cursor-pointer"
                >
                  Save my edit
                </button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-8 rounded-lg px-2.5 text-[13px] text-muted hover:bg-surface-2 hover:text-fg"
                  disabled={busy}
                  onClick={() => setEditing(false)}
                >
                  Cancel
                </Button>
              </>
            ) : (
              <>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void decide("approved")}
                  className="inline-flex h-8 items-center rounded-lg bg-fg px-3 text-[13px] font-medium text-bg transition-all hover:opacity-90 active:scale-[0.98] disabled:opacity-50 cursor-pointer"
                >
                  Approve
                </button>
                {hasCopyable && (
                  <>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-8 rounded-lg px-2.5 text-[13px] text-muted hover:bg-surface-2 hover:text-fg"
                      disabled={busy}
                      onClick={() => setEditing(true)}
                    >
                      <Pencil className="size-3.5" aria-hidden />
                      <span>Edit</span>
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-8 rounded-lg px-2.5 text-[13px] text-muted hover:bg-surface-2 hover:text-fg"
                      disabled={busy}
                      onClick={() => void copyDraft()}
                    >
                      <Copy className="size-3.5" aria-hidden />
                      <span>Copy</span>
                    </Button>
                  </>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-8 rounded-lg px-2.5 text-[13px] text-muted hover:bg-surface-2 hover:text-fg"
                  disabled={busy}
                  onClick={() => void decide("dismissed")}
                >
                  Not needed
                </Button>
              </>
            )}
          </div>
        )}
        {piece.status !== "prepared" && !settled && (
          <div className="flex flex-wrap items-center gap-1" data-testid="piece-actions">
            <Button
              size="sm"
              variant="ghost"
              className="h-8 rounded-lg px-2.5 text-[13px] text-muted hover:bg-surface-2 hover:text-fg"
              disabled={busy}
              onClick={() => void decide("dismissed")}
            >
              Dismiss
            </Button>
          </div>
        )}
      </div>

      {piece.status === "prepared" ? (
        <>
          {piece.summary && (
            <p className="text-[14.5px] leading-[1.65] text-fg">{countedWords(piece.summary)}</p>
          )}
          {piece.warning && (
            <p className="border-l-2 border-l-risk/60 pl-3.5 py-0.5 text-[13px] leading-relaxed text-muted">
              {piece.warning}
            </p>
          )}
          {replyText && !editing && (
            <div className="border-l-2 border-border-strong pl-4 py-1">
              <p className="whitespace-pre-wrap text-[14.5px] leading-[1.65] text-fg">
                {piece.edited_text || replyText}
              </p>
            </div>
          )}
          {editing && (piece.proposals ?? []).length === 0 && (
            <textarea
              aria-label="Your wording"
              value={text}
              rows={6}
              onChange={(e) => setText(e.target.value)}
              className="w-full resize-none rounded-lg border border-border-strong bg-surface p-3.5 text-[14.5px] leading-[1.65] text-fg focus:border-fg/35 focus:outline-none"
            />
          )}
          {piece.ask?.question && (
            <div className="space-y-2 border-l-2 border-border-strong pl-4 py-1">
              <p className="text-[14px] font-medium leading-relaxed text-fg">{countedWords(piece.ask.question)}</p>
              {(piece.ask.options ?? []).length > 0 && (
                <ul className="list-disc space-y-1 pl-4 text-[13.5px] leading-relaxed text-muted">
                  {(piece.ask.options ?? []).map((o, i) => (
                    <li key={i}>
                      <span className="font-medium text-fg">{o.label}</span>
                      {o.reply ? `: ${o.reply}` : ""}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
          {(piece.proposals ?? []).length > 0 && (
            <div className="space-y-3">
              {(piece.changes_over_limit ?? Math.max(0, (piece.proposals ?? []).length - DESK_CHANGE_LIMIT)) > 0 && (
                <p className="text-[13px] leading-relaxed text-muted" data-testid="piece-changes-over-limit">
                  This piece holds {(piece.proposals ?? []).length} proposed changes. The desk prepares at most {DESK_CHANGE_LIMIT} per piece ({piece.changes_over_limit ?? (piece.proposals ?? []).length - DESK_CHANGE_LIMIT} more are shown below for reference).
                </p>
              )}
              <ul className="space-y-3" aria-label="Changes to review">
                {(piece.proposals ?? []).map((_, i) =>
                  editing && bodyOf(i) !== "" ? (
                    <li key={i} className="space-y-2.5 rounded-xl border border-border-strong bg-surface p-4">
                      {subjectOf(i) !== "" && (
                        <input
                          aria-label={`Subject ${i + 1}`}
                          value={subjectEdits[String(i)] ?? subjectOf(i)}
                          onChange={(e) => setSubjectEdits((s) => ({ ...s, [String(i)]: e.target.value }))}
                          className="h-9 w-full rounded-lg border border-border bg-bg px-3 text-[13.5px] font-medium text-fg focus:border-fg/35 focus:outline-none"
                        />
                      )}
                      <textarea
                        aria-label={`Message ${i + 1}`}
                        value={edits[String(i)] ?? bodyOf(i)}
                        rows={6}
                        onChange={(e) => setEdits((s) => ({ ...s, [String(i)]: e.target.value }))}
                        className="w-full resize-none rounded-lg border border-border bg-bg p-3 text-[14.5px] leading-[1.65] text-fg focus:border-fg/35 focus:outline-none"
                      />
                    </li>
                  ) : (
                    <Change key={i} job={job} piece={piece} index={i} />
                  ),
                )}
              </ul>
            </div>
          )}
          {(piece.held_back ?? []).length > 0 && (
            <ul className="space-y-1 border-l-2 border-l-risk/60 pl-3.5 py-0.5 text-[13px] leading-relaxed text-muted">
              {(piece.held_back ?? []).map((h, i) => (
                <li key={i}>A message to {(h.to ?? []).join(", ")} was held back: {h.why}. Write it yourself.</li>
              ))}
            </ul>
          )}
          <TodoSteps todos={(piece.todos ?? []).map((t) => ({ system: t.system ?? "", record: t.record ?? "", what: t.what ?? "" }))} />
          {piece.note && <p className="text-[13px] leading-relaxed text-muted">{piece.note}</p>}
        </>
      ) : piece.status === "nothing" ? (
        <p className="text-[14px] leading-relaxed text-muted">{piece.nothing ? `Nothing to do: ${piece.nothing}` : "Nothing to prepare for this piece."}</p>
      ) : (
        <p className="border-l-2 border-l-risk/60 pl-3.5 py-0.5 text-[14px] leading-relaxed text-muted">
          {piece.status === "stopped"
            ? "Not started: the job was stopped."
            : piece.status === "screened"
              ? "Not prepared: it names a person or subject that may not go to the model."
              : "Could not be prepared."}{" "}
          {piece.message}
        </p>
      )}
      {(piece.records_unread ?? []).length > 0 && (
        <p className="border-l-2 border-l-risk/60 pl-3.5 py-0.5 text-[13px] leading-relaxed text-muted">
          The plan named {(piece.records_unread ?? []).join(", ")}, but nothing read for this piece shows it. Check before relying on it.
        </p>
      )}
      {piece.status === "prepared" && <ReadsNote piece={piece} />}
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
    <section className="space-y-3 border-t border-border pt-5" data-testid="job-checklist">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-[12.5px] font-medium text-muted">Checklist</h4>
        <span className="num text-[12px] text-muted">
          {doneCount} of {items.length} checked
        </span>
      </div>
      <ul className="space-y-2.5">
        {items.map((c, i) => {
          const isDone = !!checked[i];
          return (
            <li key={i}>
              <label className="flex cursor-pointer items-start gap-2.5 text-[14px] leading-[1.6]">
                <button
                  type="button"
                  role="checkbox"
                  aria-checked={isDone}
                  onClick={() => setChecked((cur) => ({ ...cur, [i]: !cur[i] }))}
                  className={cn(
                    "mt-1 flex size-4 shrink-0 items-center justify-center rounded border transition-colors cursor-pointer",
                    isDone ? "border-fg bg-fg text-bg" : "border-border-strong bg-bg hover:border-fg/60",
                  )}
                >
                  {isDone && <Check className="size-3" strokeWidth={2.5} aria-hidden />}
                </button>
                <span className={cn("min-w-0 flex-1", isDone ? "text-muted line-through" : "text-fg")}>
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
  const pieces = job.pieces ?? [];
  const fallbacks = job.fallbacks ?? [];
  const unread = job.unread ?? [];
  const unreadReads = job.unread_reads ?? [];
  const done = pieces.length;
  const badge = jobBadgeOf(job);
  const hasCheckedDetails = Boolean(job.found || job.not_found || typeof job.usd === "number" || fallbacks.length > 0);
  const hasWarnings = unread.length > 0 || unreadReads.length > 0;

  return (
    <article
      className={cn("w-full space-y-6", className)}
      data-testid="assist-job"
      aria-label={job.text}
    >
      <header className="space-y-2 border-b border-border pb-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <h3 className="min-w-0 flex-1 text-[20px] font-semibold leading-[1.3] tracking-[-0.02em] text-fg">
            {job.text}
          </h3>
          {LIVE.has(job.status) && (
            <Button
              size="sm"
              variant="ghost"
              className="h-8 shrink-0 rounded-lg px-3 text-[13px] text-muted hover:bg-surface-2 hover:text-fg"
              disabled={busy || job.status === "stopping"}
              onClick={() => void stop()}
            >
              {job.status === "stopping" ? "Stopping…" : "Stop"}
            </Button>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[12.5px]" data-testid="job-status">
          <StatusIndicator
            tone={badge.tone}
            label={badge.label}
            suffix={isLive && job.status !== "stopping" ? ` (${formatJobElapsed(elapsedSec)})` : ""}
          />
          {job.planned > 0 && (
            <>
              <span className="h-3 w-px bg-border" aria-hidden />
              <span className="num text-muted">
                {done} of {job.planned} pieces ready
              </span>
            </>
          )}
          {job.progress && LIVE.has(job.status) && (
            <>
              <span className="h-3 w-px bg-border" aria-hidden />
              <span className="text-muted">{job.progress}</span>
            </>
          )}
          <span className="num ml-auto text-[12px] text-muted">{formatDateTime(job.requested_at)}</span>
        </div>
      </header>

      {job.status === "failed" && (
        <p className="border-l-2 border-l-late pl-3.5 py-0.5 text-[14px] leading-relaxed text-fg">
          {job.message || "The job could not be done."} Nothing was sent or changed.
        </p>
      )}

      {job.summary && (
        <section className="space-y-2">
          <h4 className="text-[12.5px] font-medium text-muted">Summary</h4>
          <p className="text-[14.5px] leading-[1.65] text-fg">{countedWords(job.summary)}</p>
        </section>
      )}

      {job.nothing && <p className="text-[14px] leading-relaxed text-muted">Nothing to do: {job.nothing}</p>}

      {hasWarnings && (
        <div className="space-y-1.5 border-l-2 border-l-risk/60 pl-3.5 py-0.5 text-[13px] leading-relaxed text-muted">
          {unread.length > 0 && (
            <p>Sources that could not be read just now: {unread.join("; ")}.</p>
          )}
          {unreadReads.length > 0 && (
            <p>
              {unreadReads.length} {unreadReads.length === 1 ? "record" : "records"} read for the plan could not be used ({Array.from(new Set(unreadReads.map((r) => r.why ?? "withheld"))).join(", ")}); the plan stands on the others.
            </p>
          )}
        </div>
      )}

      {pieces.length > 0 && (
        <section className="border-t border-border pt-5">
          <h4 className="mb-2 text-[12.5px] font-medium text-muted">Prepared actions</h4>
          <ul className="divide-y divide-border" aria-label="Pieces of the job">
            {pieces.map((p, idx) => (
              <PieceCard key={p.n ?? idx} job={job} piece={p} onJob={onJob} onFeedback={onFeedback} />
            ))}
          </ul>
        </section>
      )}

      <InteractiveChecklist items={job.checklist} jobId={job.job_id} />

      {hasCheckedDetails && (
        <details className="group border-t border-border pt-4 text-[12.5px] text-muted" data-testid="job-checked-details">
          <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 font-medium text-muted hover:text-fg">
            <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" aria-hidden />
            Sources and checks
          </summary>
          <div className="mt-2.5 space-y-1.5 pl-5 leading-relaxed">
            {job.found && <p><span className="font-medium text-fg">Looked at:</span> {job.found}</p>}
            {job.not_found && <p><span className="font-medium text-fg">Not found or not readable:</span> {job.not_found}</p>}
            {job.fallbacks.map((f, i) => (
              <p key={i}>{f}</p>
            ))}
            {typeof job.usd === "number" && (
              <p className="num text-muted">${job.usd.toFixed(2)} of model use</p>
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
    <div data-testid="assist-jobs" className="space-y-5">
      <GiveJob onGiven={replace} />
      {error && !forbidden && <p className="text-[13.5px] text-muted">{error}</p>}
      {(jobs ?? []).length > 0 && (
        <div className="divide-y divide-border space-y-6">
          {(jobs ?? []).map((j) => (
            <JobCard key={j.job_id} job={j} onJob={replace} className="pt-5 first:pt-0" />
          ))}
        </div>
      )}
    </div>
  );
}
