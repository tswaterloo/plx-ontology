"use client";

// A person's own Assist workflows: created from plain language, mined routines or templates; triggered on a
// schedule, an event (new mail from X, new Teams message from X, PO goes late, MC question arrives, batch on
// QC hold) or on demand. Each run prepares its pieces through Assist's job pipeline; the person approves and
// confirms every change on its own card.

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, CircleAlert, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { StyledSelect } from "@/components/ui/controls";
import { EmptyState } from "@/components/ui/empty-state";
import { useProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { JobCard } from "@/components/autopilot/jobs";
import {
  createAssistWorkflow,
  deleteAssistWorkflow,
  getAssistWorkflows,
  parseAssistWorkflow,
  pauseAssistWorkflow,
  resumeAssistWorkflow,
  runAssistWorkflow,
  stopAssistWorkflow,
  updateAssistWorkflow,
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
const INPUT = "h-8 rounded-lg border border-border-strong bg-bg px-2.5 text-[13px] text-fg focus:border-accent focus:outline-none";
const WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"] as const;
type EventKind = "mail_from" | "teams_from" | "po_late" | "mc_question" | "qc_hold";

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

const JOB_STATUS_LABELS: Record<AssistJob["status"], string> = {
  queued: "Waiting to start",
  running: "Working",
  stopping: "Stopping",
  finished: "Ready for review",
  failed: "Could not be done",
  stopped: "Stopped",
};

function useAssistWorkflows() {
  const [data, setData] = useState<AssistWorkflows | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const alive = useRef(true);
  const dataRef = useRef<AssistWorkflows | null>(null);
  dataRef.current = data;
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
  const addWorkflow = useCallback((wf: AssistWorkflow) => {
    setData((prev) =>
      prev
        ? {
            ...prev,
            workflows: [wf, ...prev.workflows.filter((w) => w.workflow_id !== wf.workflow_id)],
          }
        : prev,
    );
  }, []);
  const replaceWorkflow = useCallback((wf: AssistWorkflow) => {
    setData((prev) => {
      if (!prev) return prev;
      const idx = prev.workflows.findIndex((w) => w.workflow_id === wf.workflow_id);
      if (idx === -1) return { ...prev, workflows: [wf, ...prev.workflows] };
      const next = [...prev.workflows];
      next[idx] = wf;
      return { ...prev, workflows: next };
    });
  }, []);
  const removeWorkflow = useCallback((id: string): { wf: AssistWorkflow; index: number } | null => {
    const cur = dataRef.current;
    if (!cur) return null;
    const index = cur.workflows.findIndex((w) => w.workflow_id === id);
    if (index < 0) return null;
    const wf = cur.workflows[index];
    setData((prev) => (prev ? { ...prev, workflows: prev.workflows.filter((w) => w.workflow_id !== id) } : prev));
    return { wf, index };
  }, []);
  const restoreWorkflow = useCallback((wf: AssistWorkflow, index: number) => {
    setData((prev) => {
      if (!prev || prev.workflows.some((w) => w.workflow_id === wf.workflow_id)) return prev;
      const next = [...prev.workflows];
      next.splice(Math.min(index, next.length), 0, wf);
      return { ...prev, workflows: next };
    });
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
  return { data, error, errorStatus, errorCode, reload: load, addWorkflow, replaceWorkflow, removeWorkflow, restoreWorkflow, replaceJob };
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
    <section className="mb-8 max-w-4xl space-y-3 border-b border-border pb-6" aria-label="Create a workflow" data-testid="create-workflow">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-[14px] font-medium">Set up a workflow</h2>
        <div className="inline-flex rounded-lg border border-border bg-surface p-0.5 text-[12px]" role="tablist" aria-label="How to create a workflow">
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
              className={`cursor-pointer rounded-md px-3 py-1 transition-colors ${mode === id ? "bg-bg font-medium text-fg shadow-sm" : "text-muted hover:text-fg"}`}
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
            rows={3}
            value={plain}
            disabled={busy}
            onChange={(e) => {
              setPlain(e.target.value);
              if (preview) setPreview(null);
            }}
            placeholder='For example: "every Monday 8 AM send me late POs by supplier", "when mail arrives from croda.com, draft a reply", or "when a PO goes late, draft a supplier chase"'
            className="w-full resize-none rounded-lg border border-border-strong bg-bg px-3 py-2 text-[13.5px] leading-5 text-fg placeholder:text-faint focus:border-accent focus:outline-none"
          />
          <div className="flex flex-wrap items-center gap-2 text-[12px]">
            <span className="text-muted">Trigger:</span>
            <StyledSelect
              ariaLabel="Trigger:"
              value={triggerKind}
              disabled={busy}
              onChange={(v) => setTriggerKind(v as AssistWorkflowTriggerKind | "auto")}
              options={[
                { value: "auto", label: "Read from my wording" },
                { value: "schedule", label: "Schedule" },
                { value: "event", label: "Event" },
                { value: "manual", label: "On demand" },
              ]}
            />

            {triggerKind === "schedule" && (
              <>
                <StyledSelect
                  ariaLabel="Schedule cadence"
                  value={cadence}
                  onChange={setCadence}
                  options={[
                    { value: "weekly", label: "Weekly" },
                    { value: "weekdays", label: "Weekdays" },
                    { value: "daily", label: "Daily" },
                    { value: "monthly", label: "Monthly" },
                    { value: "hourly", label: "Hourly" },
                  ]}
                />
                {cadence === "weekly" && (
                  <StyledSelect
                    ariaLabel="Schedule weekday"
                    value={weekday}
                    onChange={setWeekday}
                    options={WEEKDAYS.map((d) => ({ value: d, label: d[0].toUpperCase() + d.slice(1) }))}
                  />
                )}
                {cadence !== "hourly" && (
                  <StyledSelect
                    ariaLabel="Schedule hour"
                    value={String(hour)}
                    onChange={(v) => setHour(Number(v))}
                    options={Array.from({ length: 24 }, (_, h) => ({
                      value: String(h),
                      label: `${h % 12 || 12} ${h < 12 ? "AM" : "PM"} ET`,
                    }))}
                  />
                )}
              </>
            )}

            {triggerKind === "event" && (
              <>
                <StyledSelect
                  ariaLabel="Event kind"
                  value={eventKind}
                  onChange={(v) => setEventKind(v as typeof eventKind)}
                  options={[
                    { value: "mail_from", label: "New mail from sender" },
                    { value: "teams_from", label: "New Teams message from sender" },
                    { value: "po_late", label: "PO goes late" },
                    { value: "mc_question", label: "Mission Control question arrives" },
                    { value: "qc_hold", label: "Batch goes on QC hold" },
                  ]}
                />
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
              No mined routines are on your account yet.
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
  onDelete,
  onJob,
  onFeedback,
}: {
  wf: AssistWorkflow;
  onWorkflow: (w: AssistWorkflow) => void;
  onDelete: (w: AssistWorkflow) => void;
  onJob: (j: AssistJob) => void;
  onFeedback: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(wf.name);
  const [editInstruction, setEditInstruction] = useState(wf.instruction);
  const [editTriggerKind, setEditTriggerKind] = useState<AssistWorkflowTriggerKind>(wf.trigger_kind);
  const [editCadence, setEditCadence] = useState(String(wf.trigger.cadence ?? "weekly"));
  const [editWeekday, setEditWeekday] = useState(String(wf.trigger.weekday ?? "monday"));
  const [editDay, setEditDay] = useState(Number(wf.trigger.day ?? 1));
  const [editHour, setEditHour] = useState(Number(wf.trigger.hour ?? 8));
  const [editEventKind, setEditEventKind] = useState<EventKind>(
    (wf.trigger.event as EventKind | undefined) ?? "mail_from",
  );
  const [editSender, setEditSender] = useState(String(wf.trigger.sender ?? ""));
  const [editSubject, setEditSubject] = useState(String(wf.trigger.subject ?? ""));
  const [editSupplier, setEditSupplier] = useState(String(wf.trigger.supplier ?? ""));
  const [editProduct, setEditProduct] = useState(String(wf.trigger.product ?? ""));
  const [openRun, setOpenRun] = useState<string | null>(wf.runs[0]?.job_id ?? null);
  const [, setError, problem] = useProblem();

  const startEditing = () => {
    setEditName(wf.name);
    setEditInstruction(wf.instruction);
    setEditTriggerKind(wf.trigger_kind);
    setEditCadence(String(wf.trigger.cadence ?? "weekly"));
    setEditWeekday(String(wf.trigger.weekday ?? "monday"));
    setEditDay(Number(wf.trigger.day ?? 1));
    setEditHour(Number(wf.trigger.hour ?? 8));
    setEditEventKind((wf.trigger.event as EventKind | undefined) ?? "mail_from");
    setEditSender(String(wf.trigger.sender ?? ""));
    setEditSubject(String(wf.trigger.subject ?? ""));
    setEditSupplier(String(wf.trigger.supplier ?? ""));
    setEditProduct(String(wf.trigger.product ?? ""));
    setError(null);
    setEditing(true);
  };

  const buildEditedTrigger = (): Record<string, unknown> => {
    if (editTriggerKind === "manual") return {};
    if (editTriggerKind === "schedule") {
      if (editCadence === "hourly") return { cadence: "hourly", every_hours: Number(wf.trigger.every_hours ?? 1) || 1 };
      if (editCadence === "weekly") return { cadence: "weekly", weekday: editWeekday, hour: editHour, minute: 0 };
      if (editCadence === "monthly") return { cadence: "monthly", day: Math.min(28, Math.max(1, editDay || 1)), hour: editHour, minute: 0 };
      return { cadence: editCadence, hour: editHour, minute: 0 };
    }
    if (editEventKind === "mail_from" || editEventKind === "teams_from") {
      return {
        event: editEventKind,
        sender: editSender.trim(),
        ...(editSubject.trim() ? { subject: editSubject.trim() } : {}),
      };
    }
    if (editEventKind === "po_late") {
      return { event: "po_late", ...(editSupplier.trim() ? { supplier: editSupplier.trim() } : {}) };
    }
    if (editEventKind === "qc_hold") {
      return { event: "qc_hold", ...(editProduct.trim() ? { product: editProduct.trim() } : {}) };
    }
    return { event: editEventKind };
  };

  const saveEdit = async () => {
    if (!editInstruction.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await updateAssistWorkflow(wf.workflow_id, {
        name: editName.trim() || editInstruction.trim(),
        instruction: editInstruction.trim(),
        trigger_kind: editTriggerKind,
        trigger: buildEditedTrigger(),
      });
      onWorkflow(updated);
      setEditing(false);
      onFeedback("Workflow updated.");
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };

  const actOptimistic = async (
    optimistic: AssistWorkflow,
    toastMessage: string,
    fn: () => Promise<AssistWorkflow>,
  ) => {
    const prev = wf;
    setBusy(true);
    setError(null);
    onWorkflow(optimistic);
    onFeedback(toastMessage);
    try {
      onWorkflow(await fn());
    } catch (e) {
      onWorkflow(prev);
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
      onFeedback("Workflow run queued.");
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
          {wf.status !== "stopped" && !editing && (
            <Button size="sm" variant="outline" disabled={busy} onClick={startEditing}>
              Edit
            </Button>
          )}
          {wf.status === "active" && (
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() =>
                void actOptimistic(
                  { ...wf, status: "paused", pause_reason: "paused_by_person", next_run_at: null },
                  "Workflow paused.",
                  () => pauseAssistWorkflow(wf.workflow_id),
                )
              }
            >
              Pause
            </Button>
          )}
          {wf.status === "paused" && (
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() =>
                void actOptimistic(
                  { ...wf, status: "active", pause_reason: "", consecutive_failures: 0, backoff_until: null, last_error: "" },
                  "Workflow resumed.",
                  () => resumeAssistWorkflow(wf.workflow_id),
                )
              }
            >
              Resume
            </Button>
          )}
          {wf.status !== "stopped" && (
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() =>
                void actOptimistic(
                  { ...wf, status: "stopped", pause_reason: "stopped_by_person", next_run_at: null },
                  "Workflow stopped.",
                  () => stopAssistWorkflow(wf.workflow_id),
                )
              }
            >
              Stop
            </Button>
          )}
          <Button size="sm" variant="outline" disabled={busy} onClick={() => onDelete(wf)}>
            Delete
          </Button>
        </div>
      </div>

      {editing ? (
        <div className="space-y-2.5 rounded-lg border border-border bg-surface p-3" data-testid="workflow-edit-form">
          <div className="space-y-1">
            <label htmlFor={`wf-name-${wf.workflow_id}`} className="block text-[12px] text-muted">
              Name
            </label>
            <input
              id={`wf-name-${wf.workflow_id}`}
              aria-label="Workflow name"
              className={`${INPUT} w-full`}
              value={editName}
              disabled={busy}
              onChange={(e) => setEditName(e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <label htmlFor={`wf-instr-${wf.workflow_id}`} className="block text-[12px] text-muted">
              Instruction
            </label>
            <textarea
              id={`wf-instr-${wf.workflow_id}`}
              aria-label="Workflow instruction"
              rows={3}
              value={editInstruction}
              disabled={busy}
              onChange={(e) => setEditInstruction(e.target.value)}
              className="w-full resize-none rounded-lg border border-border-strong bg-bg px-3 py-2 text-[13px] leading-5 text-fg focus:border-accent focus:outline-none"
            />
          </div>
          <div className="flex flex-wrap items-center gap-2 text-[12px]">
            <span className="text-muted">Trigger:</span>
            <StyledSelect
              ariaLabel="Edit trigger"
              value={editTriggerKind}
              disabled={busy}
              onChange={(v) => setEditTriggerKind(v as AssistWorkflowTriggerKind)}
              options={[
                { value: "schedule", label: "Schedule" },
                { value: "event", label: "Event" },
                { value: "manual", label: "On demand" },
              ]}
            />
            {editTriggerKind === "schedule" && (
              <>
                <StyledSelect
                  ariaLabel="Edit schedule cadence"
                  value={editCadence}
                  onChange={setEditCadence}
                  options={[
                    { value: "weekly", label: "Weekly" },
                    { value: "weekdays", label: "Weekdays" },
                    { value: "daily", label: "Daily" },
                    { value: "monthly", label: "Monthly" },
                    { value: "hourly", label: "Hourly" },
                  ]}
                />
                {editCadence === "weekly" && (
                  <StyledSelect
                    ariaLabel="Edit schedule weekday"
                    value={editWeekday}
                    onChange={setEditWeekday}
                    options={WEEKDAYS.map((d) => ({ value: d, label: d[0].toUpperCase() + d.slice(1) }))}
                  />
                )}
                {editCadence === "monthly" && (
                  <StyledSelect
                    ariaLabel="Edit monthly day"
                    value={String(editDay)}
                    onChange={(v) => setEditDay(Number(v))}
                    options={Array.from({ length: 28 }, (_, i) => ({
                      value: String(i + 1),
                      label: `Day ${i + 1}`,
                    }))}
                  />
                )}
                {editCadence !== "hourly" && (
                  <StyledSelect
                    ariaLabel="Edit schedule hour"
                    value={String(editHour)}
                    onChange={(v) => setEditHour(Number(v))}
                    options={Array.from({ length: 24 }, (_, h) => ({
                      value: String(h),
                      label: `${h % 12 || 12} ${h < 12 ? "AM" : "PM"} ET`,
                    }))}
                  />
                )}
              </>
            )}
            {editTriggerKind === "event" && (
              <>
                <StyledSelect
                  ariaLabel="Edit event kind"
                  value={editEventKind}
                  onChange={(v) => setEditEventKind(v as EventKind)}
                  options={[
                    { value: "mail_from", label: "New mail from sender" },
                    { value: "teams_from", label: "New Teams message from sender" },
                    { value: "po_late", label: "PO goes late" },
                    { value: "mc_question", label: "Mission Control question arrives" },
                    { value: "qc_hold", label: "Batch goes on QC hold" },
                  ]}
                />
                {(editEventKind === "mail_from" || editEventKind === "teams_from") && (
                  <>
                    <input
                      aria-label="Edit sender filter"
                      placeholder={editEventKind === "teams_from" ? "Sender name" : "Sender or domain"}
                      className={INPUT}
                      value={editSender}
                      onChange={(e) => setEditSender(e.target.value)}
                    />
                    <input
                      aria-label="Edit subject filter"
                      placeholder={editEventKind === "teams_from" ? "Topic or words (optional)" : "Subject (optional)"}
                      className={INPUT}
                      value={editSubject}
                      onChange={(e) => setEditSubject(e.target.value)}
                    />
                  </>
                )}
                {editEventKind === "po_late" && (
                  <input
                    aria-label="Edit supplier filter"
                    placeholder="Supplier (optional)"
                    className={INPUT}
                    value={editSupplier}
                    onChange={(e) => setEditSupplier(e.target.value)}
                  />
                )}
                {editEventKind === "qc_hold" && (
                  <input
                    aria-label="Edit product filter"
                    placeholder="Product or batch (optional)"
                    className={INPUT}
                    value={editProduct}
                    onChange={(e) => setEditProduct(e.target.value)}
                  />
                )}
              </>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              disabled={
                busy ||
                !editInstruction.trim() ||
                (editTriggerKind === "event" &&
                  (editEventKind === "mail_from" || editEventKind === "teams_from") &&
                  !editSender.trim() &&
                  !editSubject.trim())
              }
              onClick={() => void saveEdit()}
            >
              Save changes
            </Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <p className="text-[13px]">{wf.instruction}</p>
      )}

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
                          · {JOB_STATUS_LABELS[r.job.status] ?? r.job.status}
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
                      <JobCard job={r.job} onJob={onJob} onFeedback={onFeedback} />
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
  const { data, error, errorStatus, errorCode, addWorkflow, replaceWorkflow, removeWorkflow, restoreWorkflow, replaceJob } = useAssistWorkflows();
  const [toast, setToast] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showToast = useCallback((message: string) => {
    setActionError(null);
    setToast(message);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 4000);
  }, []);

  useEffect(() => {
    return () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    };
  }, []);

  const handleCreated = useCallback(
    (wf: AssistWorkflow) => {
      addWorkflow(wf);
      showToast("Workflow saved.");
    },
    [addWorkflow, showToast],
  );

  const handleDelete = useCallback(
    async (wf: AssistWorkflow) => {
      const removed = removeWorkflow(wf.workflow_id);
      showToast("Workflow deleted.");
      try {
        await deleteAssistWorkflow(wf.workflow_id);
      } catch (e) {
        if (removed) restoreWorkflow(removed.wf, removed.index);
        if (toastTimer.current) clearTimeout(toastTimer.current);
        setToast(null);
        setActionError(plainError(e));
      }
    },
    [removeWorkflow, restoreWorkflow, showToast],
  );

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
      {toast && (
        <div
          role="status"
          data-testid="workflow-toast"
          className="mb-4 flex max-w-4xl items-center justify-between gap-2 rounded-lg border border-ok-line bg-ok-soft px-3.5 py-2 text-[13px] font-medium text-ok"
        >
          <span className="inline-flex items-center gap-2">
            <CheckCircle2 className="size-4 shrink-0" aria-hidden />
            {toast}
          </span>
          <button
            type="button"
            aria-label="Dismiss message"
            onClick={() => setToast(null)}
            className="cursor-pointer rounded p-0.5 text-ok/80 hover:text-ok"
          >
            <X className="size-3.5" aria-hidden />
          </button>
        </div>
      )}
      {actionError && (
        <div
          role="alert"
          data-testid="workflow-action-error"
          className="mb-4 flex max-w-4xl items-center justify-between gap-2 rounded-lg border border-late-line bg-late-soft px-3.5 py-2 text-[13px] text-late"
        >
          <span className="inline-flex items-center gap-2">
            <CircleAlert className="size-4 shrink-0" aria-hidden />
            {actionError}
          </span>
          <button
            type="button"
            aria-label="Dismiss error"
            onClick={() => setActionError(null)}
            className="cursor-pointer rounded p-0.5 text-late/80 hover:text-late"
          >
            <X className="size-3.5" aria-hidden />
          </button>
        </div>
      )}
      <CreateWorkflowPanel
        templates={data?.templates ?? []}
        routines={data?.routines ?? []}
        onCreated={handleCreated}
      />
      {(data?.workflows ?? []).length === 0 ? (
        <EmptyState
          title="No workflow set up yet."
          line='Describe one in plain language above, pick a template, or turn one of your mined routines into a workflow.'
        />
      ) : (
        <div className="space-y-6">
          {(data?.workflows ?? []).map((w) => (
            <WorkflowCard
              key={w.workflow_id}
              wf={w}
              onWorkflow={replaceWorkflow}
              onDelete={(wf) => void handleDelete(wf)}
              onJob={replaceJob}
              onFeedback={showToast}
            />
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
