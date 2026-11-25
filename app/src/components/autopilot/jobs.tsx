"use client";

// A whole job handed to Assist: the box, its progress, and the pieces it prepared, each one approved,
// edited or set aside by the person. A change in a system is reviewed on its own card and confirmed in
// two clicks there; approving a piece records the person's OK and sends or writes nothing.

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { useProblem } from "@/components/ui/notice";
import { PreparedChange } from "@/components/prepared-change";
import { TodoSteps } from "@/components/autopilot/todo";
import { decideAssistPiece, getAssistJobs, prepareAssistChange, requestAssistJob, stopAssistJob, type AssistJob, type AssistPiece } from "@/lib/api";
import { ApiError, plainError } from "@/lib/errors";
import { countedWords, formatDateTime } from "@/lib/format";

const SYSTEMS: Record<string, string> = { bc: "Business Central", portal: "Portal", mc: "Mission Control", planner: "Planner", calendar: "Calendar", sharepoint: "SharePoint", mail: "Mail", teams: "Teams" };
const LIVE = new Set(["queued", "running", "stopping"]);

const STATUS_WORDS: Record<AssistJob["status"], string> = {
  queued: "Waiting to start",
  running: "Working",
  stopping: "Stopping",
  stopped: "Stopped",
  finished: "Done",
  failed: "Could not be done",
};

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

export function GiveJob({ onGiven }: { onGiven: (job: AssistJob) => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [, setError, problem] = useProblem();
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
    <section className="mb-5 max-w-[50rem] rounded-lg border border-border bg-surface/60 p-3.5" aria-label="Give Assist a job" data-testid="give-job">
      <div className="flex flex-wrap items-baseline justify-between gap-2 pb-2">
        <label htmlFor="give-job-text" className="text-[13px] font-semibold text-fg">
          Give Assist a job
        </label>
        <span className="text-[12px] text-muted">Nothing is sent or changed until you confirm it.</span>
      </div>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <textarea
          id="give-job-text"
          value={text}
          rows={1}
          disabled={busy}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void give();
          }}
          placeholder="For example: chase every late PO from DSM"
          className="min-h-[36px] flex-1 resize-y rounded-md border border-border bg-bg px-3 py-1.5 text-[14px] leading-5 text-fg focus:border-accent focus:outline-none"
        />
        <Button size="sm" className="shrink-0" disabled={busy || !text.trim()} onClick={() => void give()}>
          Start the job
        </Button>
      </div>
      {problem}
    </section>
  );
}

function ReadsNote({ piece }: { piece: AssistPiece }) {
  const calls = piece.tool_reads.length;
  return (
    <details className="max-w-[46rem] text-[12px] text-muted" data-testid="piece-reads">
      <summary className="cursor-pointer">
        Prepared from {piece.reads.length} {piece.reads.length === 1 ? "record" : "records"} opened and {calls} {calls === 1 ? "lookup" : "lookups"}
      </summary>
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
    <li className="space-y-1 py-2" data-testid="piece-change">
      <p className="text-[13px]">
        <span className="font-medium">{SYSTEMS[a.system ?? ""] ?? a.system}</span> · {a.operation}
        {to ? <span className="text-muted"> · to {to}</span> : null}
      </p>
      {a.reason && <p className="text-[12px] text-muted">{a.reason}</p>}
      {typeof change.subject === "string" && <p className="text-[12px] font-medium">{change.subject}</p>}
      {(body ?? (typeof change.body === "string" ? change.body : "")) && <p className="max-w-[46rem] whitespace-pre-wrap text-[13px]">{body ?? String(change.body)}</p>}
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

export function PieceCard({ job, piece, onJob }: { job: AssistJob; piece: AssistPiece; onJob: (j: AssistJob) => void }) {
  const [editing, setEditing] = useState(false);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [text, setText] = useState(piece.edited_text || piece.reply);
  const [busy, setBusy] = useState(false);
  const [, setError, problem] = useProblem();
  const decide = async (decision: "approved" | "dismissed" | "edited") => {
    setBusy(true);
    setError(null);
    try {
      const out: Record<string, Record<string, string>> = {};
      for (const [i, body] of Object.entries(edits)) out[i] = { body };
      onJob(await decideAssistPiece(job.job_id, piece.n, { decision, ...(decision === "edited" ? { text: piece.proposals.length === 0 ? text : "", edits: out } : {}) }));
      setEditing(false);
    } catch (e) {
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
  return (
    <li className="space-y-2 py-4" data-testid="job-piece" id={`piece-${job.job_id}-${piece.n}`}>
      <div className="flex flex-wrap items-baseline gap-x-3">
        <span className="text-[13px] font-medium">{piece.title}</span>
        {settled && <span className="text-[12px] text-muted">{settled}</span>}
      </div>
      {piece.status === "prepared" ? (
        <>
          {piece.summary && <p className="max-w-[46rem] text-[13px]">{countedWords(piece.summary)}</p>}
          {piece.warning && <p className="max-w-[46rem] text-[12px] text-risk">{piece.warning}</p>}
          {piece.reply && !editing && <p className="max-w-[46rem] whitespace-pre-wrap text-[13px]">{piece.edited_text || piece.reply}</p>}
          {editing && piece.proposals.length === 0 && (
            <textarea aria-label="Your wording" value={text} rows={6} onChange={(e) => setText(e.target.value)} className="w-full max-w-[46rem] border border-border bg-bg px-2 py-1.5 text-[13px]" />
          )}
          {piece.ask?.question && (
            <div className="max-w-[46rem] space-y-1 border-l-2 border-border pl-3">
              <p className="text-[13px]">{countedWords(piece.ask.question)}</p>
              <ul className="list-disc space-y-0.5 pl-4 text-[12px] text-muted">
                {(piece.ask.options ?? []).map((o, i) => (
                  <li key={i}>
                    <span className="text-fg">{o.label}</span>
                    {o.reply ? ` – ${o.reply}` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {piece.proposals.length > 0 && (
            <>
              {(piece.changes_over_limit ?? Math.max(0, piece.proposals.length - DESK_CHANGE_LIMIT)) > 0 && (
                <p className="max-w-[46rem] text-[12px] text-risk" data-testid="piece-changes-over-limit">
                  This piece holds {piece.proposals.length} proposed changes. The desk prepares at most {DESK_CHANGE_LIMIT} per piece ({piece.changes_over_limit ?? piece.proposals.length - DESK_CHANGE_LIMIT} more are shown below for reference).
                </p>
              )}
              <ul className="max-w-[46rem] divide-y divide-border" aria-label="Changes to review">
                {piece.proposals.map((_, i) =>
                  editing && bodyOf(i) !== "" ? (
                    <li key={i} className="py-2">
                      <textarea aria-label={`Message ${i + 1}`} value={edits[String(i)] ?? bodyOf(i)} rows={6} onChange={(e) => setEdits((s) => ({ ...s, [String(i)]: e.target.value }))} className="w-full border border-border bg-bg px-2 py-1.5 text-[13px]" />
                    </li>
                  ) : (
                    <Change key={i} job={job} piece={piece} index={i} />
                  ),
                )}
              </ul>
            </>
          )}
          {piece.held_back.length > 0 && (
            <ul className="max-w-[46rem] list-disc pl-4 text-[12px] text-risk">
              {piece.held_back.map((h, i) => (
                <li key={i}>A message to {(h.to ?? []).join(", ")} was held back: {h.why}. Write it yourself.</li>
              ))}
            </ul>
          )}
          <TodoSteps todos={(piece.todos ?? []).map((t) => ({ system: t.system ?? "", record: t.record ?? "", what: t.what ?? "" }))} />
          {piece.note && <p className="text-[12px] text-muted">{piece.note}</p>}
        </>
      ) : piece.status === "nothing" ? (
        <p className="max-w-[46rem] text-[13px] text-muted">{piece.nothing ? `Nothing to do: ${piece.nothing}` : "Nothing to prepare for this piece."}</p>
      ) : (
        <p className="max-w-[46rem] text-[13px] text-risk">
          {piece.status === "stopped" ? "Not started: the job was stopped." : piece.status === "screened" ? "Not prepared: it names a person or subject that may not go to the model." : "Could not be prepared."} {piece.message}
        </p>
      )}
      {piece.records_unread.length > 0 && <p className="max-w-[46rem] text-[12px] text-risk">The plan named {piece.records_unread.join(", ")}, but nothing read for this piece shows it. Check before relying on it.</p>}
      {piece.status === "prepared" && <ReadsNote piece={piece} />}
      {piece.status === "prepared" && !settled && (
        <div className="flex flex-wrap gap-2 pt-1" data-testid="piece-actions">
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
                <Button size="sm" variant="outline" disabled={busy} onClick={() => setEditing(true)}>
                  Edit
                </Button>
              )}
              <Button size="sm" variant="outline" disabled={busy} onClick={() => void decide("dismissed")}>
                Not needed
              </Button>
            </>
          )}
        </div>
      )}
      {piece.status !== "prepared" && !settled && (
        <div className="flex flex-wrap gap-2 pt-1" data-testid="piece-actions">
          <Button size="sm" variant="outline" disabled={busy} onClick={() => void decide("dismissed")}>
            Dismiss
          </Button>
        </div>
      )}
      {problem}
    </li>
  );
}

export function JobCard({ job, onJob }: { job: AssistJob; onJob: (j: AssistJob) => void }) {
  const [busy, setBusy] = useState(false);
  const [, setError, problem] = useProblem();
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
  const hasCheckedDetails = Boolean(job.found || job.not_found || typeof job.usd === "number" || job.fallbacks.length > 0);
  return (
    <article className="max-w-[50rem] space-y-3 rounded-lg border border-border bg-surface/60 p-4" data-testid="assist-job" aria-label={job.text}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="text-[15px] font-semibold text-fg">{job.text}</h3>
        <span className="num ml-auto text-[12px] text-muted">{formatDateTime(job.requested_at)}</span>
      </div>
      <p className="text-[12px] text-muted" data-testid="job-status">
        {STATUS_WORDS[job.status]}
        {job.planned > 0 ? ` · ${done} of ${job.planned} pieces ready` : ""}
        {job.progress && LIVE.has(job.status) ? ` · ${job.progress}` : ""}
      </p>
      {LIVE.has(job.status) && (
        <Button size="sm" variant="outline" disabled={busy || job.status === "stopping"} onClick={() => void stop()}>
          {job.status === "stopping" ? "Stopping…" : "Stop"}
        </Button>
      )}
      {job.status === "failed" && <p className="text-[13px] text-risk">{job.message || "The job could not be done."} Nothing was sent or changed.</p>}
      {job.summary && <p className="text-[14px] leading-relaxed text-fg">{countedWords(job.summary)}</p>}
      {job.nothing && <p className="text-[13px] text-muted">Nothing to do: {job.nothing}</p>}
      {job.checklist.length > 0 && (
        <div className="rounded-md border border-border/70 bg-bg/70 p-3" data-testid="job-checklist">
          <p className="mb-1 text-[12px] font-medium text-muted">Checklist</p>
          <ul className="list-disc space-y-1 pl-4 text-[13px] leading-relaxed text-fg">
            {job.checklist.map((c, i) => (
              <li key={i}>
                {c.text}
                {c.piece ? <span className="text-muted"> (piece {c.piece})</span> : null}
              </li>
            ))}
          </ul>
        </div>
      )}
      {job.unread.length > 0 && <p className="text-[12px] text-risk">Sources that could not be read just now: {job.unread.join("; ")}.</p>}
      {job.unread_reads.length > 0 && (
        <p className="text-[12px] text-risk">
          {job.unread_reads.length} {job.unread_reads.length === 1 ? "record" : "records"} read for the plan could not be used ({Array.from(new Set(job.unread_reads.map((r) => r.why ?? "withheld"))).join(", ")}); the plan stands on the others.
        </p>
      )}
      {hasCheckedDetails && (
        <details className="rounded-md border border-border/70 bg-bg/60 px-3 py-2 text-[12px] text-muted" data-testid="job-checked-details">
          <summary className="cursor-pointer font-medium text-muted hover:text-fg">Show what Assist checked</summary>
          <div className="mt-2 space-y-1.5 leading-relaxed">
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
      {job.pieces.length > 0 && (
        <ul className="divide-y divide-border/70 rounded-md border border-border/70 bg-bg/70 px-3.5" aria-label="Pieces of the job">
          {job.pieces.map((p) => (
            <PieceCard key={p.n} job={job} piece={p} onJob={onJob} />
          ))}
        </ul>
      )}
      {problem}
    </article>
  );
}

/** The box and the person's jobs, newest first. */
export function JobsView() {
  const { jobs, error, forbidden, replace } = useAssistJobs();
  return (
    <div data-testid="assist-jobs">
      <GiveJob onGiven={replace} />
      {error && !forbidden && <p className="mb-3 text-[13px] text-risk">{error}</p>}
      {(jobs ?? []).length > 0 && (
        <div className="mb-8 space-y-6">
          {(jobs ?? []).map((j) => (
            <JobCard key={j.job_id} job={j} onJob={replace} />
          ))}
        </div>
      )}
    </div>
  );
}
