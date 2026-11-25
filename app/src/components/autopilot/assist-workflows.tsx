"use client";

// A person's own Assist workflows: created from plain language, mined routines or templates; triggered on a
// schedule, an event (new mail from X, new Teams message from X, PO goes late, MC question arrives, batch on
// QC hold) or on demand. Each run prepares its pieces through Assist's job pipeline; the person approves and
// confirms every change on its own card.

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { useProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { JobCard } from "@/components/autopilot/jobs";
import {
  createAssistWorkflow,
  getAssistWorkflows,
  parseAssistWorkflow,
  pauseAssistWorkflow,
  resumeAssistWorkflow,
  runAssistWorkflow,
  stopAssistWorkflow,
  type AssistJob,
  type AssistMinedRoutine,
  type AssistWorkflow,
  type AssistWorkflowParsed,
  type AssistWorkflows,
  type AssistWorkflowTemplate,
  type AssistWorkflowTriggerKind,
} from "@/lib/api";
import { ApiError, plainError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";

const LIVE = new Set(["queued", "running", "stopping"]);
const SELECT = "rounded-[3px] border border-border bg-bg px-2 py-1 text-[12px]";
const INPUT = "border border-border bg-bg px-2 py-1 text-[13px]";
const WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"] as const;

const SOURCE_LABELS: Record<AssistWorkflow["source_kind"], string> = {
  plain_language: "From plain language",
  routine: "From mined routine",
  template: "From template",
};

const STATUS_LABELS: Record<AssistWorkflow["status"], string> = {
  active: "Active",
  paused: "Paused",
  stopped: "Stopped",
};

function useAssistWorkflows() {
  const [data, setData] = useState<AssistWorkflows | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const alive = useRef(true);
  const load = useCallback(async () => {
    try {
      const got = await getAssistWorkflows();
      if (alive.current) {
        setData(got);
        setError(got.status === "not_run" ? got.reason : null);
        setErrorStatus(null);
        setErrorCode(null);
      }
    } catch (e) {
      if (alive.current) {
        setError(plainError(e));
        setErrorStatus(e instanceof ApiError ? e.status : null);
        setErrorCode(e instanceof ApiError ? e.code : null);
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
  const hasLiveRun = (data?.workflows ?? []).some((w) => w.runs.some((r) => r.job && LIVE.has(r.job.status)));
  useEffect(() => {
    if (!hasLiveRun) return;
    const t = setInterval(() => void load(), 4000);
    return () => clearInterval(t);
  }, [hasLiveRun, load]);
  const replaceWorkflow = useCallback((wf: AssistWorkflow) => {
    setData((prev) =>
      prev
        ? {
            ...prev,
            workflows: [wf, ...prev.workflows.filter((w) => w.workflow_id !== wf.workflow_id)],
          }
        : prev,
    );
  }, []);
  const replaceJob = useCallback((job: AssistJob) => {
    setData((prev) =>
      prev
        ? {
            ...prev,
            workflows: prev.workflows.map((w) => ({
              ...w,
              runs: w.runs.map((r) => (r.job_id === job.job_id ? { ...r, job } : r)),
            })),
          }
        : prev,
    );
  }, []);
  return { data, error, errorStatus, errorCode, reload: load, replaceWorkflow, replaceJob };
}

type CreateMode = "plain" | "template" | "routine";

function CreateWorkflowPanel({
  templates,
  routines,
  onCreated,
}: {
  templates: AssistWorkflowTemplate[];
  routines: AssistMinedRoutine[];
  onCreated: (wf: AssistWorkflow) => void;
}) {
  const [mode, setMode] = useState<CreateMode>("plain");
  const [plain, setPlain] = useState("");
  const [preview, setPreview] = useState<AssistWorkflowParsed | null>(null);
  const [triggerKind, setTriggerKind] = useState<AssistWorkflowTriggerKind | "auto">("auto");
  const [cadence, setCadence] = useState("weekly");
  const [weekday, setWeekday] = useState("monday");
  const [hour, setHour] = useState(8);
  const [eventKind, setEventKind] = useState<"mail_from" | "teams_from" | "po_late" | "mc_question" | "qc_hold">("mail_from");
  const [sender, setSender] = useState("");
  const [subject, setSubject] = useState("");
  const [supplier, setSupplier] = useState("");
  const [product, setProduct] = useState("");
  const [tplParams, setTplParams] = useState<Record<string, { sender?: string; supplier?: string; product?: string }>>({});
  const [busy, setBusy] = useState(false);
  const [, setError, problem] = useProblem();

  const checkPreview = async () => {
    if (!plain.trim()) return;
    setError(null);
    try {
      setPreview(await parseAssistWorkflow(plain.trim()));
    } catch (e) {
      setError(plainError(e));
    }
  };

  const buildCustomTrigger = (): { trigger_kind?: AssistWorkflowTriggerKind; trigger?: Record<string, unknown> } => {
    if (triggerKind === "auto") return {};
    if (triggerKind === "manual") return { trigger_kind: "manual", trigger: {} };
    if (triggerKind === "schedule") {
      if (cadence === "hourly") return { trigger_kind: "schedule", trigger: { cadence: "hourly", every_hours: 1 } };
      if (cadence === "weekly") return { trigger_kind: "schedule", trigger: { cadence: "weekly", weekday, hour, minute: 0 } };
      if (cadence === "monthly") return { trigger_kind: "schedule", trigger: { cadence: "monthly", day: 1, hour, minute: 0 } };
      return { trigger_kind: "schedule", trigger: { cadence, hour, minute: 0 } };
    }
    if (eventKind === "mail_from" || eventKind === "teams_from") {
      return { trigger_kind: "event", trigger: { event: eventKind, sender: sender.trim(), ...(subject.trim() ? { subject: subject.trim() } : {}) } };
    }
    if (eventKind === "po_late") {
      return { trigger_kind: "event", trigger: { event: "po_late", ...(supplier.trim() ? { supplier: supplier.trim() } : {}) } };
    }
    if (eventKind === "qc_hold") {
      return { trigger_kind: "event", trigger: { event: "qc_hold", ...(product.trim() ? { product: product.trim() } : {}) } };
    }
    return { trigger_kind: "event", trigger: { event: eventKind } };
  };

  const createPlain = async () => {
    if (!plain.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const wf = await createAssistWorkflow({ plain_text: plain.trim(), ...buildCustomTrigger() });
      onCreated(wf);
      setPlain("");
      setPreview(null);
      setTriggerKind("auto");
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };

  const createFromTemplate = async (tpl: AssistWorkflowTemplate) => {
    setBusy(true);
    setError(null);
    try {
      const extra = tplParams[tpl.template_id] ?? {};
      const trigger: Record<string, unknown> = { ...tpl.trigger };
      if ((tpl.trigger.event === "mail_from" || tpl.trigger.event === "teams_from") && extra.sender) trigger.sender = extra.sender.trim();
      if (tpl.trigger.event === "po_late" && extra.supplier) trigger.supplier = extra.supplier.trim();
      if (tpl.trigger.event === "qc_hold" && extra.product) trigger.product = extra.product.trim();
      const wf = await createAssistWorkflow({ template_id: tpl.template_id, trigger });
      onCreated(wf);
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };

  const createFromRoutine = async (routine: AssistMinedRoutine) => {
    setBusy(true);
    setError(null);
    try {
      const wf = await createAssistWorkflow({ routine_id: routine.routine_id });
      onCreated(wf);
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="mb-8 max-w-[52rem] space-y-3 border-b border-border pb-6" aria-label="Create a workflow" data-testid="create-workflow">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-[14px] font-medium">Set up a workflow</h2>
        <div className="inline-flex rounded-[4px] border border-border text-[12px]" role="tablist" aria-label="How to create a workflow">
          {(
            [
              ["plain", "Plain language"],
              ["template", `Templates (${templates.length})`],
              ["routine", `Mined routines (${routines.length})`],
            ] as const
          ).map(([id, label]) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={mode === id}
              onClick={() => (setMode(id), setError(null))}
              className={`cursor-pointer px-3 py-1 ${mode === id ? "bg-hover font-medium text-fg" : "text-muted hover:text-fg"}`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {mode === "plain" && (
        <div className="space-y-2.5" data-testid="workflow-plain-form">
          <label htmlFor="workflow-plain-text" className="block text-[12px] text-muted">
            Describe when it should run and what Assist should prepare. Nothing sends or changes without your confirmation.
          </label>
          <textarea
            id="workflow-plain-text"
            rows={2}
            value={plain}
            disabled={busy}
            onChange={(e) => setPlain(e.target.value)}
            placeholder='For example: "every Monday 8 AM send me late POs by supplier", "when mail arrives from croda.com, draft a reply", or "when a PO goes late, draft a supplier chase"'
            className="w-full resize-y border border-border bg-bg px-2 py-1.5 text-[13px]"
          />
          <div className="flex flex-wrap items-center gap-2 text-[12px]">
            <label htmlFor="workflow-trigger-kind" className="text-muted">
              Trigger:
            </label>
            <select
              id="workflow-trigger-kind"
              className={SELECT}
              value={triggerKind}
              disabled={busy}
              onChange={(e) => setTriggerKind(e.target.value as AssistWorkflowTriggerKind | "auto")}
            >
              <option value="auto">Read from my wording</option>
              <option value="schedule">Schedule</option>
              <option value="event">Event</option>
              <option value="manual">On demand</option>
            </select>

            {triggerKind === "schedule" && (
              <>
                <select aria-label="Schedule cadence" className={SELECT} value={cadence} onChange={(e) => setCadence(e.target.value)}>
                  <option value="weekly">Weekly</option>
                  <option value="weekdays">Weekdays</option>
                  <option value="daily">Daily</option>
                  <option value="monthly">Monthly</option>
                  <option value="hourly">Hourly</option>
                </select>
                {cadence === "weekly" && (
                  <select aria-label="Schedule weekday" className={SELECT} value={weekday} onChange={(e) => setWeekday(e.target.value)}>
                    {WEEKDAYS.map((d) => (
                      <option key={d} value={d}>
                        {d[0].toUpperCase() + d.slice(1)}
                      </option>
                    ))}
                  </select>
                )}
                {cadence !== "hourly" && (
                  <select aria-label="Schedule hour" className={SELECT} value={hour} onChange={(e) => setHour(Number(e.target.value))}>
                    {Array.from({ length: 24 }, (_, h) => (
                      <option key={h} value={h}>
                        {h % 12 || 12} {h < 12 ? "AM" : "PM"} ET
                      </option>
                    ))}
                  </select>
                )}
              </>
            )}

            {triggerKind === "event" && (
              <>
                <select aria-label="Event kind" className={SELECT} value={eventKind} onChange={(e) => setEventKind(e.target.value as typeof eventKind)}>
                  <option value="mail_from">New mail from sender</option>
                  <option value="teams_from">New Teams message from sender</option>
                  <option value="po_late">PO goes late</option>
                  <option value="mc_question">Mission Control question arrives</option>
                  <option value="qc_hold">Batch goes on QC hold</option>
                </select>
                {(eventKind === "mail_from" || eventKind === "teams_from") && (
                  <>
                    <input aria-label="Sender address or domain" placeholder={eventKind === "teams_from" ? "Sender name" : "Sender or domain"} className={INPUT} value={sender} onChange={(e) => setSender(e.target.value)} />
                    <input aria-label="Subject contains" placeholder={eventKind === "teams_from" ? "Topic or words (optional)" : "Subject (optional)"} className={INPUT} value={subject} onChange={(e) => setSubject(e.target.value)} />
                  </>
                )}
                {eventKind === "po_late" && (
                  <input aria-label="Supplier filter" placeholder="Supplier (optional)" className={INPUT} value={supplier} onChange={(e) => setSupplier(e.target.value)} />
                )}
                {eventKind === "qc_hold" && (
                  <input aria-label="Product filter" placeholder="Product or batch (optional)" className={INPUT} value={product} onChange={(e) => setProduct(e.target.value)} />
                )}
              </>
            )}
          </div>

          {preview && triggerKind === "auto" && (
            <p className="text-[12px] text-muted" data-testid="workflow-parse-preview">
              Parsed trigger: <span className="font-medium text-fg">{preview.trigger_summary}</span> · Prepares: {preview.instruction}
            </p>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={busy || !plain.trim()} onClick={() => void createPlain()}>
              Save workflow
            </Button>
            {triggerKind === "auto" && (
              <Button size="sm" variant="outline" disabled={busy || !plain.trim()} onClick={() => void checkPreview()}>
                Preview trigger
              </Button>
            )}
          </div>
        </div>
      )}

      {mode === "template" && (
        <ul className="divide-y divide-border" data-testid="workflow-templates">
          {templates.map((tpl) => {
            const p = tplParams[tpl.template_id] ?? {};
            const needsSender = tpl.trigger.event === "mail_from" || tpl.trigger.event === "teams_from";
            const allowsSupplier = tpl.trigger.event === "po_late";
            const allowsProduct = tpl.trigger.event === "qc_hold";
            return (
              <li key={tpl.template_id} className="flex flex-wrap items-start justify-between gap-3 py-3" data-testid="workflow-template-row">
                <div className="min-w-0 flex-1 space-y-1">
                  <p className="text-[13px] font-medium">
                    {tpl.name} <span className="font-normal text-muted">· {tpl.trigger_summary}</span>
                  </p>
                  <p className="text-[12px] text-muted">{tpl.description}</p>
                  {needsSender && (
                    <input
                      aria-label={`Sender for ${tpl.name}`}
                      placeholder={tpl.trigger.event === "teams_from" ? "Sender name (required)" : "Sender email or domain (required)"}
                      value={p.sender ?? ""}
                      onChange={(e) => setTplParams((s) => ({ ...s, [tpl.template_id]: { ...p, sender: e.target.value } }))}
                      className={`${INPUT} mt-1 w-64`}
                    />
                  )}
                  {allowsSupplier && (
                    <input
                      aria-label={`Supplier for ${tpl.name}`}
                      placeholder="Supplier name (optional)"
                      value={p.supplier ?? ""}
                      onChange={(e) => setTplParams((s) => ({ ...s, [tpl.template_id]: { ...p, supplier: e.target.value } }))}
                      className={`${INPUT} mt-1 w-64`}
                    />
                  )}
                  {allowsProduct && (
                    <input
                      aria-label={`Product for ${tpl.name}`}
                      placeholder="Product or batch (optional)"
                      value={p.product ?? ""}
                      onChange={(e) => setTplParams((s) => ({ ...s, [tpl.template_id]: { ...p, product: e.target.value } }))}
                      className={`${INPUT} mt-1 w-64`}
                    />
                  )}
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy || (needsSender && !(p.sender ?? "").trim())}
                  onClick={() => void createFromTemplate(tpl)}
                >
                  Use template
                </Button>
              </li>
            );
          })}
        </ul>
      )}

      {mode === "routine" && (
        <div data-testid="workflow-routines-picker">
          {routines.length === 0 ? (
            <p className="text-[12px] text-muted">
              No mined routines are on your account yet. You can also check the{" "}
              <Link href="/assist?show=routines" className="underline underline-offset-2">
                Routines tab
              </Link>
              .
            </p>
          ) : (
            <ul className="divide-y divide-border">
              {routines.map((r) => (
                <li key={r.routine_id} className="flex flex-wrap items-start justify-between gap-3 py-3" data-testid="mined-routine-row">
                  <div className="min-w-0 flex-1 space-y-0.5">
                    <p className="text-[13px] font-medium">
                      {r.suggested_name} <span className="font-normal text-muted">· {r.suggested_trigger_summary}</span>
                    </p>
                    {r.description && <p className="text-[12px] text-muted">{r.description}</p>}
                    <p className="text-[12px] text-muted">
                      Observed {String(r.mined.acts ?? 0)} acts
                      {r.mined.days != null ? ` over ${String(r.mined.days)} days` : ""}
                      {r.mined.support != null ? ` · support ${Math.round(Number(r.mined.support) * 100)}%` : ""}
                    </p>
                  </div>
                  <Button size="sm" variant="outline" disabled={busy} onClick={() => void createFromRoutine(r)}>
                    Turn into workflow
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {problem}
    </section>
  );
}

function WorkflowCard({
  wf,
  onWorkflow,
  onJob,
}: {
  wf: AssistWorkflow;
  onWorkflow: (w: AssistWorkflow) => void;
  onJob: (j: AssistJob) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [openRun, setOpenRun] = useState<string | null>(wf.runs[0]?.job_id ?? null);
  const [, setError, problem] = useProblem();

  const act = async (fn: () => Promise<AssistWorkflow>) => {
    setBusy(true);
    setError(null);
    try {
      onWorkflow(await fn());
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };

  const runNow = async () => {
    setBusy(true);
    setError(null);
    try {
      const got = await runAssistWorkflow(wf.workflow_id);
      onWorkflow(got.workflow);
      setOpenRun(got.job.job_id);
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };

  const statusText =
    wf.status === "paused" && wf.pause_reason === "paused_after_failures"
      ? "Paused after repeated failures"
      : STATUS_LABELS[wf.status];

  return (
    <article className="max-w-[52rem] space-y-2.5 border-b border-border pb-5" data-testid="assist-workflow" aria-label={wf.name}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-[14px] font-medium">{wf.name}</h3>
          <p className="text-[12px] text-muted" data-testid="workflow-meta">
            {statusText} · {wf.trigger_summary} · {SOURCE_LABELS[wf.source_kind]}
            {wf.next_run_at && wf.status === "active" ? ` · next ${formatDateTime(wf.next_run_at)}` : ""}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {wf.status !== "stopped" && (
            <Button size="sm" disabled={busy} onClick={() => void runNow()}>
              Run now
            </Button>
          )}
          {wf.status === "active" && (
            <Button size="sm" variant="outline" disabled={busy} onClick={() => void act(() => pauseAssistWorkflow(wf.workflow_id))}>
              Pause
            </Button>
          )}
          {wf.status === "paused" && (
            <Button size="sm" variant="outline" disabled={busy} onClick={() => void act(() => resumeAssistWorkflow(wf.workflow_id))}>
              Resume
            </Button>
          )}
          {wf.status !== "stopped" && (
            <Button size="sm" variant="outline" disabled={busy} onClick={() => void act(() => stopAssistWorkflow(wf.workflow_id))}>
              Stop
            </Button>
          )}
        </div>
      </div>

      <p className="text-[13px]">{wf.instruction}</p>

      {wf.backoff_until && wf.status === "active" && (
        <p className="text-[12px] text-risk" data-testid="workflow-backoff">
          Waiting after {wf.consecutive_failures} failed {wf.consecutive_failures === 1 ? "run" : "runs"} so it does not spin; next automatic try after {formatDateTime(wf.backoff_until)}.
        </p>
      )}
      {wf.last_error && (
        <p className="text-[12px] text-risk" data-testid="workflow-error">
          Last failure: {wf.last_error}
        </p>
      )}

      {wf.runs.length > 0 && (
        <div className="space-y-2 pt-1" data-testid="workflow-runs">
          <p className="text-[12px] text-muted">
            {wf.runs.length} {wf.runs.length === 1 ? "run" : "runs"}
          </p>
          <ul className="divide-y divide-border border-t border-border">
            {wf.runs.map((r) => {
              const expanded = openRun === r.job_id;
              return (
                <li key={r.job_id} className="py-2">
                  <div className="flex flex-wrap items-center justify-between gap-2 text-[12px]">
                    <span>
                      <span className="font-medium">{r.trigger_note || (r.kind === "run_now" ? "Run on demand" : "Triggered")}</span>
                      <span className="text-muted"> · {formatDateTime(r.triggered_at)}</span>
                      {r.job && (
                        <span className="text-muted">
                          {" "}
                          · {r.job.status}
                          {r.job.planned > 0 ? ` (${r.job.pieces.length} of ${r.job.planned} pieces)` : ""}
                        </span>
                      )}
                    </span>
                    {r.job && (
                      <button
                        type="button"
                        onClick={() => setOpenRun(expanded ? null : r.job_id)}
                        className="cursor-pointer text-muted underline underline-offset-2 hover:text-fg"
                      >
                        {expanded ? "Hide run" : "Review pieces"}
                      </button>
                    )}
                  </div>
                  {expanded && r.job && (
                    <div className="mt-3 pl-3 border-l-2 border-border">
                      <JobCard job={r.job} onJob={onJob} />
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {problem}
    </article>
  );
}

/** The Workflows tab: create from plain language, templates or mined routines; manage & review runs. */
export function AssistWorkflowsView() {
  const { data, error, errorStatus, errorCode, replaceWorkflow, replaceJob } = useAssistWorkflows();
  if (!data && !error) return <Pending page label="Reading your workflows" />;
  if (!data && error && errorStatus === 403) {
    return (
      <div data-testid="assist-workflows-view">
        <p
          className="max-w-[44rem] rounded-md border border-border bg-surface px-4 py-3 text-[13px] text-muted"
          role={errorCode === "canary_route_refused" ? "status" : "alert"}
          data-testid="assist-workflows-restricted"
        >
          {error}
        </p>
      </div>
    );
  }
  return (
    <div data-testid="assist-workflows-view">
      {error && <p className="mb-4 text-[13px] text-risk">{error}</p>}
      <CreateWorkflowPanel
        templates={data?.templates ?? []}
        routines={data?.routines ?? []}
        onCreated={replaceWorkflow}
      />
      {(data?.workflows ?? []).length === 0 ? (
        <EmptyState
          title="No workflow set up yet."
          line='Describe one in plain language above, pick a template, or turn one of your mined routines into a workflow.'
        />
      ) : (
        <div className="space-y-6">
          {(data?.workflows ?? []).map((w) => (
            <WorkflowCard key={w.workflow_id} wf={w} onWorkflow={replaceWorkflow} onJob={replaceJob} />
          ))}
        </div>
      )}
    </div>
  );
}

/** Shown on the Routines tab so a person can turn any mined routine directly into a workflow. */
export function MinedRoutinesSection() {
  const { data, error, errorStatus } = useAssistWorkflows();
  const [created, setCreated] = useState<Record<string, string>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [, setErr, problem] = useProblem();

  const make = async (routine: AssistMinedRoutine) => {
    setBusyId(routine.routine_id);
    setErr(null);
    try {
      const wf = await createAssistWorkflow({ routine_id: routine.routine_id });
      setCreated((s) => ({ ...s, [routine.routine_id]: wf.workflow_id }));
    } catch (e) {
      setErr(plainError(e));
    } finally {
      setBusyId(null);
    }
  };

  if (!data && !error) return null;
  if (!data && errorStatus === 403) return null;
  const routines = data?.routines ?? [];
  return (
    <section className="mb-8 max-w-[52rem]" data-testid="mined-routines-section">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="text-[13px] font-medium">Mined routines from your past work</h2>
        <Link href="/assist?show=workflows" className="text-[12px] text-muted underline underline-offset-2 hover:text-fg">
          Open Workflows
        </Link>
      </div>
      {error && <p className="mb-2 text-[12px] text-risk">{error}</p>}
      {routines.length === 0 ? (
        <p className="text-[12px] text-muted">No mined routines are on your account yet.</p>
      ) : (
        <ul className="divide-y divide-border">
          {routines.map((r) => (
            <li key={r.routine_id} className="flex flex-wrap items-center justify-between gap-3 py-2.5 text-[13px]" data-testid="routines-tab-mined-row">
              <div className="min-w-0 flex-1">
                <p className="font-medium">
                  {r.suggested_name} <span className="font-normal text-muted">· {r.suggested_trigger_summary}</span>
                </p>
                {r.description && <p className="text-[12px] text-muted">{r.description}</p>}
                <p className="text-[12px] text-muted">
                  Observed {String(r.mined.acts ?? 0)} acts
                  {r.mined.days != null ? ` over ${String(r.mined.days)} days` : ""}
                </p>
              </div>
              {created[r.routine_id] ? (
                <Link href="/assist?show=workflows" className="text-[12px] text-ok underline underline-offset-2">
                  Workflow created. Open it
                </Link>
              ) : (
                <Button size="sm" variant="outline" disabled={busyId === r.routine_id} onClick={() => void make(r)}>
                  Turn into workflow
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {problem}
    </section>
  );
}
