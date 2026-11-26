"use client";

// A person's own Assist workflows: created from plain language, mined routines or templates; triggered on a
// schedule, an event (new mail from X, new Teams message from X, PO goes late, MC question arrives, batch on
// QC hold) or on demand. Each run prepares its pieces through Assist's job pipeline; the person approves and
// confirms every change on its own card.

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Bot,
  CheckCircle2,
  CircleAlert,
  Clock3,
  Filter,
  Pause,
  Pencil,
  Play,
  Plus,
  ShieldCheck,
  Sparkles,
  Square,
  Trash2,
  X,
  Zap,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { StyledSelect } from "@/components/ui/controls";
import { DotGridCanvas, type DotGridEdge, type DotGridNode } from "@/components/ui/dot-grid-canvas";
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
import { cn } from "@/lib/cn";
import { ApiError, plainError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";

const LIVE = new Set(["queued", "running", "stopping"]);
const INPUT =
  "h-8 rounded-md border border-border-strong bg-bg px-2.5 text-[13px] text-fg placeholder:text-muted focus:border-fg/35 focus:outline-none";
const WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"] as const;
type EventKind = "mail_from" | "teams_from" | "po_late" | "mc_question" | "qc_hold";

const QUICK_PETRA_TEMPLATES: {
  id: string;
  label: string;
  triggerLabel: string;
  prompt: string;
  triggerKind: AssistWorkflowTriggerKind;
  trigger: Record<string, unknown>;
}[] = [
  {
    id: "quick-late-pos",
    label: "Weekly late PO sweep",
    triggerLabel: "Mondays 8 AM ET",
    prompt: "Every Monday at 8 AM, group late purchase orders by supplier and draft follow-up emails.",
    triggerKind: "schedule",
    trigger: { cadence: "weekly", weekday: "monday", hour: 8, minute: 0 },
  },
  {
    id: "quick-qc-holds",
    label: "QC hold impact brief",
    triggerLabel: "When batch hits QC hold",
    prompt: "When a batch goes on QC hold, list affected sales orders and prepare customer delay notes.",
    triggerKind: "event",
    trigger: { event: "qc_hold" },
  },
  {
    id: "quick-po-chase",
    label: "Late PO supplier chase",
    triggerLabel: "When PO goes late",
    prompt: "When a PO goes past due without a confirmed ETA, draft a concise supplier status request.",
    triggerKind: "event",
    trigger: { event: "po_late" },
  },
  {
    id: "quick-mc-triage",
    label: "Mission Control brief",
    triggerLabel: "When MC question arrives",
    prompt: "When a Mission Control question arrives, gather linked order records and draft a clear answer.",
    triggerKind: "event",
    trigger: { event: "mc_question" },
  },
];

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
  finished: "Ready",
  failed: "Blocked",
  stopped: "Stopped",
};

function WorkflowStatusBadge({ status, label }: { status: AssistWorkflow["status"]; label: string }) {
  const textTone = status === "active" ? "text-fg/85" : status === "paused" ? "text-risk" : "text-muted";
  const dotTone = status === "active" ? "bg-dot-ok" : status === "paused" ? "bg-dot-risk" : "bg-dot-neutral";
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-[12px] font-medium leading-4", textTone)}>
      <span className={cn("size-1.5 shrink-0 rounded-full", dotTone)} aria-hidden />
      <span>{label}</span>
    </span>
  );
}

export interface WorkflowPipelineSpec {
  id: string;
  name: string;
  status: AssistWorkflow["status"] | "draft";
  triggerKind: AssistWorkflowTriggerKind;
  triggerSummary: string;
  trigger?: Record<string, unknown>;
  instruction?: string;
  runsCount?: number;
}

function scopeSummaryOf(triggerKind: AssistWorkflowTriggerKind, trigger?: Record<string, unknown> | null): string {
  const trig = trigger && typeof trigger === "object" ? trigger : {};
  const parts: string[] = [];
  if (typeof trig.sender === "string" && trig.sender.trim()) parts.push(`Sender: ${trig.sender.trim()}`);
  if (typeof trig.subject === "string" && trig.subject.trim()) parts.push(`Subject: ${trig.subject.trim()}`);
  if (typeof trig.supplier === "string" && trig.supplier.trim()) parts.push(`Supplier: ${trig.supplier.trim()}`);
  if (typeof trig.product === "string" && trig.product.trim()) parts.push(`Product: ${trig.product.trim()}`);
  if (parts.length > 0) return parts.join(" · ");
  if (triggerKind === "event") {
    const ev = typeof trig.event === "string" ? trig.event : "";
    if (ev === "po_late") return "All open late purchase orders";
    if (ev === "qc_hold") return "All batches entering QC hold";
    if (ev === "mc_question") return "Incoming Mission Control questions";
    return "Matching incoming messages";
  }
  if (triggerKind === "schedule") return "Records matching instruction scope";
  return "Triggered manually on current records";
}

/** Pure: build interactive dot-grid canvas nodes and edges for a workflow pipeline. */
export function buildWorkflowPipelineGraph(spec: WorkflowPipelineSpec): { nodes: DotGridNode[]; edges: DotGridEdge[] } {
  const triggerKind = spec?.triggerKind ?? "manual";
  const trigger = spec?.trigger ?? {};
  const triggerIcon =
    triggerKind === "event" ? (
      <Zap className="size-3.5" aria-hidden />
    ) : triggerKind === "schedule" ? (
      <Clock3 className="size-3.5" aria-hidden />
    ) : (
      <Play className="size-3.5" aria-hidden />
    );
  const triggerBadge = triggerKind === "event" ? "Event" : triggerKind === "schedule" ? "Schedule" : "Manual";
  const scopeText = scopeSummaryOf(triggerKind, trigger);
  const instr = (spec?.instruction ?? "").trim();
  const actionSummary = instr.length > 96 ? `${instr.slice(0, 93)}...` : instr || "Prepare drafts and checks";

  const nodes: DotGridNode[] = [
    {
      id: "trigger",
      x: 24,
      y: 78,
      width: 224,
      height: 112,
      kindLabel: "Trigger",
      title: spec?.triggerSummary || "On demand",
      description:
        triggerKind === "event"
          ? "Listens for live operational events."
          : triggerKind === "schedule"
            ? "Runs on the configured ET schedule."
            : "Runs whenever you press Run now.",
      badge: triggerBadge,
      tone: "accent",
      icon: triggerIcon,
    },
    {
      id: "scope",
      x: 344,
      y: 78,
      width: 224,
      height: 112,
      kindLabel: "Scope & Filters",
      title: scopeText,
      description: "Reads only records your account is permitted to open.",
      badge: "Filtered",
      tone: "neutral",
      icon: <Filter className="size-3.5" aria-hidden />,
    },
    {
      id: "action",
      x: 664,
      y: 78,
      width: 236,
      height: 112,
      kindLabel: "Assist Action",
      title: "Draft and check",
      description: actionSummary,
      badge: spec.runsCount ? `${spec.runsCount} runs` : "Drafts",
      tone: spec.status === "paused" ? "risk" : "ok",
      icon: <Bot className="size-3.5" aria-hidden />,
    },
    {
      id: "approval",
      x: 996,
      y: 78,
      width: 224,
      height: 112,
      kindLabel: "Approval Gate",
      title: "Human confirmation",
      description: "Every email or system write waits for your review and confirm click.",
      badge: "Required",
      tone: "risk",
      icon: <ShieldCheck className="size-3.5" aria-hidden />,
    },
  ];

  const edges: DotGridEdge[] = [
    { id: "e-trigger-scope", from: "trigger", to: "scope", label: "fires", active: spec.status === "active" },
    { id: "e-scope-action", from: "scope", to: "action", label: "matches", active: spec.status === "active" },
    { id: "e-action-approval", from: "action", to: "approval", label: "prepares", active: spec.status === "active" },
  ];

  return { nodes, edges };
}

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
  const hasLiveRun = (data?.workflows ?? []).some((w) => (w.runs ?? []).some((r) => r.job && LIVE.has(r.job.status)));
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
              runs: (w.runs ?? []).map((r) => (r.job_id === job.job_id ? { ...r, job } : r)),
            })),
          }
        : prev,
    );
  }, []);
  return {
    data,
    error,
    errorStatus,
    errorCode,
    reload: load,
    addWorkflow,
    replaceWorkflow,
    removeWorkflow,
    restoreWorkflow,
    replaceJob,
  };
}

type CreateMode = "plain" | "template" | "routine";

function CreateWorkflowPanel({
  templates,
  routines,
  onCreated,
  composerRef,
  onDraftPreview,
}: {
  templates: AssistWorkflowTemplate[];
  routines: AssistMinedRoutine[];
  onCreated: (wf: AssistWorkflow) => void;
  composerRef?: React.RefObject<HTMLTextAreaElement | null>;
  onDraftPreview?: (spec: WorkflowPipelineSpec | null) => void;
}) {
  const [mode, setMode] = useState<CreateMode>("plain");
  const [plain, setPlain] = useState("");
  const [preview, setPreview] = useState<AssistWorkflowParsed | null>(null);
  const [triggerKind, setTriggerKind] = useState<AssistWorkflowTriggerKind | "auto">("auto");
  const [cadence, setCadence] = useState("weekly");
  const [weekday, setWeekday] = useState("monday");
  const [day, setDay] = useState(1);
  const [hour, setHour] = useState(8);
  const [eventKind, setEventKind] = useState<EventKind>("mail_from");
  const [sender, setSender] = useState("");
  const [subject, setSubject] = useState("");
  const [supplier, setSupplier] = useState("");
  const [product, setProduct] = useState("");
  const [tplParams, setTplParams] = useState<
    Record<string, { sender?: string; subject?: string; supplier?: string; product?: string }>
  >({});
  const [busy, setBusy] = useState(false);
  const [, setError, problem] = useProblem();

  const checkPreview = async () => {
    if (!plain.trim()) return;
    setError(null);
    try {
      const parsed = await parseAssistWorkflow(plain.trim());
      setPreview(parsed);
      onDraftPreview?.({
        id: "draft-preview",
        name: parsed.name || "Draft workflow",
        status: "draft",
        triggerKind: parsed.trigger_kind ?? "manual",
        triggerSummary: parsed.trigger_summary ?? "",
        trigger: parsed.trigger && typeof parsed.trigger === "object" ? parsed.trigger : {},
        instruction: parsed.instruction ?? "",
      });
    } catch (e) {
      setError(plainError(e));
    }
  };

  const buildCustomTrigger = (): { trigger_kind?: AssistWorkflowTriggerKind; trigger?: Record<string, unknown> } => {
    if (triggerKind === "auto") return {};
    if (triggerKind === "manual") return { trigger_kind: "manual", trigger: {} };
    if (triggerKind === "schedule") {
      if (cadence === "hourly") return { trigger_kind: "schedule", trigger: { cadence: "hourly", every_hours: 1 } };
      if (cadence === "weekly")
        return { trigger_kind: "schedule", trigger: { cadence: "weekly", weekday, hour, minute: 0 } };
      if (cadence === "monthly")
        return {
          trigger_kind: "schedule",
          trigger: { cadence: "monthly", day: Math.min(28, Math.max(1, day || 1)), hour, minute: 0 },
        };
      return { trigger_kind: "schedule", trigger: { cadence, hour, minute: 0 } };
    }
    if (eventKind === "mail_from" || eventKind === "teams_from") {
      return {
        trigger_kind: "event",
        trigger: { event: eventKind, sender: sender.trim(), ...(subject.trim() ? { subject: subject.trim() } : {}) },
      };
    }
    if (eventKind === "po_late") {
      return {
        trigger_kind: "event",
        trigger: { event: "po_late", ...(supplier.trim() ? { supplier: supplier.trim() } : {}) },
      };
    }
    if (eventKind === "qc_hold") {
      return {
        trigger_kind: "event",
        trigger: { event: "qc_hold", ...(product.trim() ? { product: product.trim() } : {}) },
      };
    }
    return { trigger_kind: "event", trigger: { event: eventKind } };
  };

  const missingEventFilter =
    triggerKind === "event" &&
    (eventKind === "mail_from" || eventKind === "teams_from") &&
    !sender.trim() &&
    !subject.trim();

  const createPlain = async () => {
    if (!plain.trim() || missingEventFilter) return;
    setBusy(true);
    setError(null);
    try {
      const wf = await createAssistWorkflow({ plain_text: plain.trim(), ...buildCustomTrigger() });
      onCreated(wf);
      setPlain("");
      setPreview(null);
      setTriggerKind("auto");
      setSender("");
      setSubject("");
      setSupplier("");
      setProduct("");
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };

  const createFromTemplate = async (tpl: AssistWorkflowTemplate) => {
    const extra = tplParams[tpl.template_id] ?? {};
    const tplTrigger = tpl.trigger && typeof tpl.trigger === "object" ? tpl.trigger : {};
    const isMessageSenderTemplate =
      tplTrigger.event === "mail_from" ||
      tplTrigger.event === "teams_from" ||
      tpl.template_id === "mail-from-sender" ||
      tpl.template_id === "teams-from-sender";
    if (isMessageSenderTemplate && !(extra.sender ?? "").trim() && !(extra.subject ?? "").trim()) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const trigger: Record<string, unknown> = { ...tplTrigger };
      if (isMessageSenderTemplate) {
        if (extra.sender?.trim()) trigger.sender = extra.sender.trim();
        if (extra.subject?.trim()) trigger.subject = extra.subject.trim();
      }
      if (tplTrigger.event === "po_late" && extra.supplier?.trim()) trigger.supplier = extra.supplier.trim();
      if (tplTrigger.event === "qc_hold" && extra.product?.trim()) trigger.product = extra.product.trim();
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
    <section
      className="space-y-4 rounded-xl border border-border bg-surface dark:bg-[#14161b] p-4"
      aria-label="Create automated workflow"
      data-testid="create-workflow"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-[15.5px] font-semibold tracking-tight text-fg">Create automated workflow</h2>
        <div
          className="liquid-glass-pill inline-flex rounded-lg p-0.5 text-[12px]"
          role="tablist"
          aria-label="How to create a workflow"
        >
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
              className={cn(
                "cursor-pointer rounded-md px-2.5 py-1 font-medium transition-colors",
                mode === id ? "liquid-glass-active" : "text-muted hover:text-fg",
              )}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {mode === "plain" && (
        <div className="space-y-3" data-testid="workflow-plain-form">
          <label htmlFor="workflow-plain-text" className="block text-[12.5px] text-muted">
            Describe when it should run and what Assist should prepare. Nothing sends or changes without your confirmation.
          </label>
          <textarea
            ref={composerRef}
            id="workflow-plain-text"
            rows={2}
            value={plain}
            disabled={busy}
            onChange={(e) => {
              setPlain(e.target.value);
              if (preview) setPreview(null);
            }}
            placeholder='For example: "every Monday 8 AM send me late POs by supplier", "when mail arrives from croda.com, draft a reply", or "when a PO goes late, draft a supplier chase"'
            className="w-full resize-none rounded-lg border border-border-strong bg-bg dark:bg-[#0d0e11] px-3 py-2 text-[13.5px] leading-[1.55] text-fg placeholder:text-faint focus:border-accent focus:outline-none"
          />

          {/* 4 Quick Petra Templates */}
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4" data-testid="quick-petra-templates">
            {QUICK_PETRA_TEMPLATES.map((qt) => (
              <button
                key={qt.id}
                type="button"
                disabled={busy}
                onClick={() => {
                  setPlain(qt.prompt);
                  setPreview(null);
                  onDraftPreview?.({
                    id: qt.id,
                    name: qt.label,
                    status: "draft",
                    triggerKind: qt.triggerKind,
                    triggerSummary: qt.triggerLabel,
                    trigger: qt.trigger,
                    instruction: qt.prompt,
                  });
                  composerRef?.current?.focus();
                }}
                className="flex flex-col items-start rounded-lg border border-border bg-bg dark:bg-[#0d0e11] px-3 py-2 text-left transition-colors hover:border-border-strong hover:bg-hover cursor-pointer"
              >
                <span className="inline-flex items-center gap-1.5 text-[12.5px] font-medium text-fg">
                  <Sparkles className="size-3 shrink-0 text-muted" aria-hidden />
                  <span>{qt.label}</span>
                </span>
                <span className="mt-0.5 text-[11.5px] text-faint">{qt.triggerLabel}</span>
              </button>
            ))}
          </div>

          <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
            <div className="flex flex-wrap items-center gap-2 text-[12.5px]">
              <span className="font-medium text-muted">Trigger:</span>
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
                  {cadence === "monthly" && (
                    <StyledSelect
                      ariaLabel="Schedule monthly day"
                      value={String(day)}
                      onChange={(v) => setDay(Number(v))}
                      options={Array.from({ length: 28 }, (_, i) => ({
                        value: String(i + 1),
                        label: `Day ${i + 1}`,
                      }))}
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
                    onChange={(v) => setEventKind(v as EventKind)}
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
                      <input
                        aria-label="Sender address or domain"
                        placeholder={eventKind === "teams_from" ? "Sender name" : "Sender or domain"}
                        className={INPUT}
                        value={sender}
                        onChange={(e) => setSender(e.target.value)}
                      />
                      <input
                        aria-label="Subject contains"
                        placeholder={eventKind === "teams_from" ? "Topic or words (optional)" : "Subject (optional)"}
                        className={INPUT}
                        value={subject}
                        onChange={(e) => setSubject(e.target.value)}
                      />
                    </>
                  )}
                  {eventKind === "po_late" && (
                    <input
                      aria-label="Supplier filter"
                      placeholder="Supplier (optional)"
                      className={INPUT}
                      value={supplier}
                      onChange={(e) => setSupplier(e.target.value)}
                    />
                  )}
                  {eventKind === "qc_hold" && (
                    <input
                      aria-label="Product filter"
                      placeholder="Product or batch (optional)"
                      className={INPUT}
                      value={product}
                      onChange={(e) => setProduct(e.target.value)}
                    />
                  )}
                </>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              {triggerKind === "auto" && (
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-8 rounded-md px-2.5 text-[12.5px] text-muted hover:bg-surface-2 hover:text-fg"
                  disabled={busy || !plain.trim()}
                  onClick={() => void checkPreview()}
                >
                  Preview trigger
                </Button>
              )}
              <button
                type="button"
                disabled={busy || !plain.trim() || missingEventFilter}
                onClick={() => void createPlain()}
                className="inline-flex h-8 items-center rounded-md bg-accent px-3.5 text-[12.5px] font-medium text-accent-fg transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40 cursor-pointer"
              >
                {busy ? "Saving..." : "Save workflow"}
              </button>
            </div>
          </div>

          {preview && triggerKind === "auto" && (
            <p
              className="border-t border-border pt-2.5 text-[13px] leading-relaxed text-muted"
              data-testid="workflow-parse-preview"
            >
              Parsed trigger: <span className="font-medium text-fg">{preview.trigger_summary}</span> · Prepares:{" "}
              {preview.instruction}
            </p>
          )}
        </div>
      )}

      {mode === "template" && (
        <ul className="divide-y divide-border border-t border-border" data-testid="workflow-templates">
          {templates.map((tpl) => {
            const p = tplParams[tpl.template_id] ?? {};
            const tplTrigger = tpl.trigger && typeof tpl.trigger === "object" ? tpl.trigger : {};
            const needsSender =
              tplTrigger.event === "mail_from" ||
              tplTrigger.event === "teams_from" ||
              tpl.template_id === "mail-from-sender" ||
              tpl.template_id === "teams-from-sender";
            const allowsSupplier = tplTrigger.event === "po_late";
            const allowsProduct = tplTrigger.event === "qc_hold";
            const missingRequiredFilter = needsSender && !(p.sender ?? "").trim() && !(p.subject ?? "").trim();
            return (
              <li
                key={tpl.template_id}
                className="flex flex-wrap items-start justify-between gap-3 py-3"
                data-testid="workflow-template-row"
              >
                <div className="min-w-0 flex-1 space-y-1">
                  <p className="text-[13.5px] font-medium text-fg">
                    {tpl.name} <span className="font-normal text-muted">· {tpl.trigger_summary}</span>
                  </p>
                  <p className="text-[12.5px] leading-relaxed text-muted">{tpl.description}</p>
                  {needsSender && (
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <input
                        aria-label={`Sender for ${tpl.name}`}
                        placeholder={
                          tplTrigger.event === "teams_from" || tpl.template_id === "teams-from-sender"
                            ? "Sender name"
                            : "Sender email or domain"
                        }
                        value={p.sender ?? ""}
                        onChange={(e) =>
                          setTplParams((s) => ({ ...s, [tpl.template_id]: { ...p, sender: e.target.value } }))
                        }
                        className={`${INPUT} w-52`}
                      />
                      <input
                        aria-label={`Subject for ${tpl.name}`}
                        placeholder={
                          tplTrigger.event === "teams_from" || tpl.template_id === "teams-from-sender"
                            ? "Topic or keywords"
                            : "Subject contains"
                        }
                        value={p.subject ?? ""}
                        onChange={(e) =>
                          setTplParams((s) => ({ ...s, [tpl.template_id]: { ...p, subject: e.target.value } }))
                        }
                        className={`${INPUT} w-52`}
                      />
                      {missingRequiredFilter && (
                        <span className="text-[11.5px] text-faint">Enter a sender or subject to enable</span>
                      )}
                    </div>
                  )}
                  {allowsSupplier && (
                    <input
                      aria-label={`Supplier for ${tpl.name}`}
                      placeholder="Supplier name (optional)"
                      value={p.supplier ?? ""}
                      onChange={(e) =>
                        setTplParams((s) => ({ ...s, [tpl.template_id]: { ...p, supplier: e.target.value } }))
                      }
                      className={`${INPUT} mt-1.5 w-60`}
                    />
                  )}
                  {allowsProduct && (
                    <input
                      aria-label={`Product for ${tpl.name}`}
                      placeholder="Product or batch (optional)"
                      value={p.product ?? ""}
                      onChange={(e) =>
                        setTplParams((s) => ({ ...s, [tpl.template_id]: { ...p, product: e.target.value } }))
                      }
                      className={`${INPUT} mt-1.5 w-60`}
                    />
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-1.5">
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-8 rounded-md px-2.5 text-[12.5px] text-muted hover:bg-surface-2 hover:text-fg"
                    onClick={() =>
                      onDraftPreview?.({
                        id: `tpl:${tpl.template_id}`,
                        name: tpl.name ?? "",
                        status: "draft",
                        triggerKind: tpl.trigger_kind ?? "manual",
                        triggerSummary: tpl.trigger_summary ?? "",
                        trigger: {
                          ...tplTrigger,
                          ...(p.sender?.trim() ? { sender: p.sender.trim() } : {}),
                          ...(p.subject?.trim() ? { subject: p.subject.trim() } : {}),
                          ...(p.supplier?.trim() ? { supplier: p.supplier.trim() } : {}),
                          ...(p.product?.trim() ? { product: p.product.trim() } : {}),
                        },
                        instruction: tpl.instruction || tpl.description || tpl.name || "",
                      })
                    }
                  >
                    Preview map
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    className="h-8 rounded-md px-3 text-[12.5px] font-medium"
                    disabled={busy || missingRequiredFilter}
                    onClick={() => void createFromTemplate(tpl)}
                  >
                    Use template
                  </Button>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {mode === "routine" && (
        <div data-testid="workflow-routines-picker">
          {routines.length === 0 ? (
            <p className="text-[13px] text-muted">No mined routines are on your account yet.</p>
          ) : (
            <ul className="divide-y divide-border border-t border-border">
              {routines.map((r) => (
                <li
                  key={r.routine_id}
                  className="flex flex-wrap items-start justify-between gap-3 py-3"
                  data-testid="mined-routine-row"
                >
                  <div className="min-w-0 flex-1 space-y-0.5">
                    <p className="text-[13.5px] font-medium text-fg">
                      {r.suggested_name} <span className="font-normal text-muted">· {r.suggested_trigger_summary}</span>
                    </p>
                    {r.description && <p className="text-[12.5px] text-muted">{r.description}</p>}
                    <p className="text-[12px] text-faint">
                      Observed {String(r.mined?.acts ?? 0)} acts
                      {r.mined?.days != null ? ` over ${String(r.mined.days)} days` : ""}
                      {r.mined?.support != null ? ` · support ${Math.round(Number(r.mined.support) * 100)}%` : ""}
                    </p>
                  </div>
                  <Button
                    size="sm"
                    variant="secondary"
                    className="h-8 rounded-md px-3 text-[12.5px] font-medium"
                    disabled={busy}
                    onClick={() => void createFromRoutine(r)}
                  >
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
  selected,
  onSelect,
  onWorkflow,
  onDelete,
  onJob,
  onFeedback,
}: {
  wf: AssistWorkflow;
  selected?: boolean;
  onSelect?: () => void;
  onWorkflow: (w: AssistWorkflow) => void;
  onDelete: (w: AssistWorkflow) => void;
  onJob: (j: AssistJob) => void;
  onFeedback: (message: string) => void;
}) {
  const wfTrigger = wf.trigger && typeof wf.trigger === "object" ? wf.trigger : {};
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(wf.name ?? "");
  const [editInstruction, setEditInstruction] = useState(wf.instruction ?? wf.name ?? "");
  const [editTriggerKind, setEditTriggerKind] = useState<AssistWorkflowTriggerKind>(wf.trigger_kind ?? "manual");
  const [editCadence, setEditCadence] = useState(String(wfTrigger.cadence ?? "weekly"));
  const [editWeekday, setEditWeekday] = useState(String(wfTrigger.weekday ?? "monday"));
  const [editDay, setEditDay] = useState(Number(wfTrigger.day ?? 1));
  const [editHour, setEditHour] = useState(Number(wfTrigger.hour ?? 8));
  const [editEventKind, setEditEventKind] = useState<EventKind>((wfTrigger.event as EventKind | undefined) ?? "mail_from");
  const [editSender, setEditSender] = useState(String(wfTrigger.sender ?? ""));
  const [editSubject, setEditSubject] = useState(String(wfTrigger.subject ?? ""));
  const [editSupplier, setEditSupplier] = useState(String(wfTrigger.supplier ?? ""));
  const [editProduct, setEditProduct] = useState(String(wfTrigger.product ?? ""));
  const [openRun, setOpenRun] = useState<string | null>((wf.runs ?? [])[0]?.job_id ?? null);
  const [, setError, problem] = useProblem();

  const startEditing = () => {
    const trig = wf.trigger && typeof wf.trigger === "object" ? wf.trigger : {};
    setEditName(wf.name ?? "");
    setEditInstruction(wf.instruction ?? wf.name ?? "");
    setEditTriggerKind(wf.trigger_kind ?? "manual");
    setEditCadence(String(trig.cadence ?? "weekly"));
    setEditWeekday(String(trig.weekday ?? "monday"));
    setEditDay(Number(trig.day ?? 1));
    setEditHour(Number(trig.hour ?? 8));
    setEditEventKind((trig.event as EventKind | undefined) ?? "mail_from");
    setEditSender(String(trig.sender ?? ""));
    setEditSubject(String(trig.subject ?? ""));
    setEditSupplier(String(trig.supplier ?? ""));
    setEditProduct(String(trig.product ?? ""));
    setError(null);
    setEditing(true);
  };

  const buildEditedTrigger = (): Record<string, unknown> => {
    const trig = wf.trigger && typeof wf.trigger === "object" ? wf.trigger : {};
    if (editTriggerKind === "manual") return {};
    if (editTriggerKind === "schedule") {
      if (editCadence === "hourly") return { cadence: "hourly", every_hours: Number(trig.every_hours ?? 1) || 1 };
      if (editCadence === "weekly") return { cadence: "weekly", weekday: editWeekday, hour: editHour, minute: 0 };
      if (editCadence === "monthly")
        return { cadence: "monthly", day: Math.min(28, Math.max(1, editDay || 1)), hour: editHour, minute: 0 };
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
    const trimmedInstr = (editInstruction ?? "").trim();
    if (!trimmedInstr) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await updateAssistWorkflow(wf.workflow_id, {
        name: (editName ?? "").trim() || trimmedInstr,
        instruction: trimmedInstr,
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
    <article
      className={cn(
        "space-y-3 rounded-xl border p-4 transition-colors",
        selected
          ? "border-border-strong bg-row-on dark:bg-[#1b1e25]"
          : "border-border bg-surface dark:bg-[#14161b] hover:border-border-strong",
      )}
      data-testid="assist-workflow"
      aria-label={wf.name}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1 space-y-1.5">
          <button
            type="button"
            onClick={onSelect}
            className="text-left text-[15px] font-semibold tracking-tight text-fg transition-colors hover:text-accent cursor-pointer"
          >
            {wf.name}
          </button>
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[12.5px] text-muted" data-testid="workflow-meta">
            <WorkflowStatusBadge status={wf.status} label={statusText} />
            <span>{wf.trigger_summary}</span>
            <span aria-hidden>·</span>
            <span>{SOURCE_LABELS[wf.source_kind]}</span>
            {wf.next_run_at && wf.status === "active" && (
              <>
                <span aria-hidden>·</span>
                <span className="num">next {formatDateTime(wf.next_run_at)}</span>
              </>
            )}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-1">
          {wf.status !== "stopped" && (
            <button
              type="button"
              disabled={busy}
              onClick={() => void runNow()}
              className="inline-flex h-8 items-center gap-1.5 rounded-md bg-accent px-3 text-[12.5px] font-medium text-accent-fg transition-opacity hover:opacity-90 disabled:opacity-50 cursor-pointer"
            >
              <Play className="size-3" aria-hidden />
              <span>Run now</span>
            </button>
          )}
          {wf.status !== "stopped" && !editing && (
            <Button
              size="sm"
              variant="ghost"
              className="h-8 rounded-md px-2.5 text-[12.5px] text-muted hover:bg-surface-2 hover:text-fg"
              disabled={busy}
              onClick={startEditing}
            >
              <Pencil className="size-3.5" aria-hidden />
              <span>Edit</span>
            </Button>
          )}
          {wf.status === "active" && (
            <Button
              size="sm"
              variant="ghost"
              className="h-8 rounded-md px-2.5 text-[12.5px] text-muted hover:bg-surface-2 hover:text-fg"
              disabled={busy}
              onClick={() =>
                void actOptimistic(
                  { ...wf, status: "paused", pause_reason: "paused_by_person", next_run_at: null },
                  "Workflow paused.",
                  () => pauseAssistWorkflow(wf.workflow_id),
                )
              }
            >
              <Pause className="size-3.5" aria-hidden />
              <span>Pause</span>
            </Button>
          )}
          {wf.status === "paused" && (
            <Button
              size="sm"
              variant="ghost"
              className="h-8 rounded-md px-2.5 text-[12.5px] text-muted hover:bg-surface-2 hover:text-fg"
              disabled={busy}
              onClick={() =>
                void actOptimistic(
                  {
                    ...wf,
                    status: "active",
                    pause_reason: "",
                    consecutive_failures: 0,
                    backoff_until: null,
                    last_error: "",
                  },
                  "Workflow resumed.",
                  () => resumeAssistWorkflow(wf.workflow_id),
                )
              }
            >
              <Play className="size-3.5" aria-hidden />
              <span>Resume</span>
            </Button>
          )}
          {wf.status !== "stopped" && (
            <Button
              size="sm"
              variant="ghost"
              className="h-8 rounded-md px-2.5 text-[12.5px] text-muted hover:bg-surface-2 hover:text-fg"
              disabled={busy}
              onClick={() =>
                void actOptimistic(
                  { ...wf, status: "stopped", pause_reason: "stopped_by_person", next_run_at: null },
                  "Workflow stopped.",
                  () => stopAssistWorkflow(wf.workflow_id),
                )
              }
            >
              <Square className="size-3" aria-hidden />
              <span>Stop</span>
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            className="h-8 rounded-md px-2.5 text-[12.5px] text-muted hover:bg-surface-2 hover:text-late"
            disabled={busy}
            onClick={() => onDelete(wf)}
          >
            <Trash2 className="size-3.5" aria-hidden />
            <span>Delete</span>
          </Button>
        </div>
      </div>

      {editing ? (
        <div className="space-y-3 rounded-lg border border-border bg-bg dark:bg-[#0d0e11] p-3.5" data-testid="workflow-edit-form">
          <div className="space-y-1">
            <label htmlFor={`wf-name-${wf.workflow_id}`} className="block text-[12px] font-medium text-muted">
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
            <label htmlFor={`wf-instr-${wf.workflow_id}`} className="block text-[12px] font-medium text-muted">
              Instruction
            </label>
            <textarea
              id={`wf-instr-${wf.workflow_id}`}
              aria-label="Workflow instruction"
              rows={3}
              value={editInstruction}
              disabled={busy}
              onChange={(e) => setEditInstruction(e.target.value)}
              className="w-full resize-none rounded-md border border-border-strong bg-bg px-3 py-2 text-[13.5px] leading-[1.6] text-fg focus:border-accent focus:outline-none"
            />
          </div>
          <div className="flex flex-wrap items-center gap-2 text-[12.5px]">
            <span className="font-medium text-muted">Trigger:</span>
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
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <button
              type="button"
              disabled={
                busy ||
                !(editInstruction ?? "").trim() ||
                (editTriggerKind === "event" &&
                  (editEventKind === "mail_from" || editEventKind === "teams_from") &&
                  !editSender.trim() &&
                  !editSubject.trim())
              }
              onClick={() => void saveEdit()}
              className="inline-flex h-8 items-center rounded-md bg-accent px-3.5 text-[12.5px] font-medium text-accent-fg transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40 cursor-pointer"
            >
              Save changes
            </button>
            <Button
              size="sm"
              variant="ghost"
              className="h-8 rounded-md px-2.5 text-[12.5px] text-muted hover:bg-surface-2 hover:text-fg"
              disabled={busy}
              onClick={() => setEditing(false)}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <p className="text-[13.5px] leading-[1.6] text-fg/90">{wf.instruction ?? wf.name ?? ""}</p>
      )}

      {wf.backoff_until && wf.status === "active" && (
        <p
          className="rounded-lg border border-risk-line bg-risk-soft px-3 py-2 text-[12.5px] leading-relaxed text-muted"
          data-testid="workflow-backoff"
        >
          Waiting after {wf.consecutive_failures} failed {wf.consecutive_failures === 1 ? "run" : "runs"} so it does not
          spin; next automatic try after {formatDateTime(wf.backoff_until)}.
        </p>
      )}
      {wf.last_error && (
        <p
          className="rounded-lg border border-late-line bg-late-soft px-3 py-2 text-[12.5px] leading-relaxed text-late"
          data-testid="workflow-error"
        >
          Last failure: {wf.last_error}
        </p>
      )}

      {(wf.runs ?? []).length > 0 && (
        <div className="space-y-2 border-t border-border pt-3" data-testid="workflow-runs">
          <p className="num text-[12px] font-medium text-faint">
            {(wf.runs ?? []).length} {(wf.runs ?? []).length === 1 ? "run" : "runs"}
          </p>
          <ul className="divide-y divide-border/70">
            {(wf.runs ?? []).map((r) => {
              const expanded = openRun === r.job_id;
              return (
                <li key={r.job_id} className="py-2">
                  <div className="flex flex-wrap items-center justify-between gap-2 text-[12.5px]">
                    <span>
                      <span className="font-medium text-fg">
                        {r.trigger_note || (r.kind === "run_now" ? "Run on demand" : "Triggered")}
                      </span>
                      <span className="num text-muted"> · {formatDateTime(r.triggered_at)}</span>
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
                        className="cursor-pointer rounded-md px-2 py-0.5 text-[12px] font-medium text-muted transition-colors hover:bg-surface-2 hover:text-fg"
                      >
                        {expanded ? "Hide run" : "Review pieces"}
                      </button>
                    )}
                  </div>
                  {expanded && r.job && (
                    <div className="mt-3 rounded-lg border border-border bg-bg dark:bg-[#0d0e11] p-3.5">
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

/** The Workflows tab: compact composer + 4 quick Petra templates above a clean split of
 *  elevated workflow cards and the interactive 4-node DotGridCanvas pipeline preview. */
export function AssistWorkflowsView() {
  const {
    data,
    error,
    errorStatus,
    errorCode,
    addWorkflow,
    replaceWorkflow,
    removeWorkflow,
    restoreWorkflow,
    replaceJob,
  } = useAssistWorkflows();
  const [toast, setToast] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [selectedWorkflowId, setSelectedWorkflowId] = useState<string | null>(null);
  const [draftSpec, setDraftSpec] = useState<WorkflowPipelineSpec | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>("trigger");
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);

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
      setDraftSpec(null);
      setSelectedWorkflowId(wf.workflow_id);
      showToast("Workflow saved.");
    },
    [addWorkflow, showToast],
  );

  const handleDelete = useCallback(
    async (wf: AssistWorkflow) => {
      const removed = removeWorkflow(wf.workflow_id);
      if (selectedWorkflowId === wf.workflow_id) setSelectedWorkflowId(null);
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
    [removeWorkflow, restoreWorkflow, selectedWorkflowId, showToast],
  );

  const workflows = useMemo(() => data?.workflows ?? [], [data?.workflows]);
  const activeWorkflow = useMemo(
    () => workflows.find((w) => w.workflow_id === selectedWorkflowId) ?? workflows[0] ?? null,
    [workflows, selectedWorkflowId],
  );

  const canvasSpec = useMemo<WorkflowPipelineSpec | null>(() => {
    if (draftSpec) return draftSpec;
    if (activeWorkflow) {
      return {
        id: activeWorkflow.workflow_id,
        name: activeWorkflow.name ?? "",
        status: activeWorkflow.status ?? "active",
        triggerKind: activeWorkflow.trigger_kind ?? "manual",
        triggerSummary: activeWorkflow.trigger_summary ?? "",
        trigger: activeWorkflow.trigger && typeof activeWorkflow.trigger === "object" ? activeWorkflow.trigger : {},
        instruction: activeWorkflow.instruction ?? activeWorkflow.name ?? "",
        runsCount: (activeWorkflow.runs ?? []).length,
      };
    }
    const firstTpl = data?.templates?.[0];
    if (firstTpl) {
      return {
        id: `tpl:${firstTpl.template_id}`,
        name: firstTpl.name ?? "",
        status: "draft",
        triggerKind: firstTpl.trigger_kind ?? "manual",
        triggerSummary: firstTpl.trigger_summary ?? "",
        trigger: firstTpl.trigger && typeof firstTpl.trigger === "object" ? firstTpl.trigger : {},
        instruction: firstTpl.instruction || firstTpl.description || firstTpl.name || "",
      };
    }
    const fallback = QUICK_PETRA_TEMPLATES[0];
    return {
      id: fallback.id,
      name: fallback.label,
      status: "draft",
      triggerKind: fallback.triggerKind,
      triggerSummary: fallback.triggerLabel,
      trigger: fallback.trigger,
      instruction: fallback.prompt,
    };
  }, [activeWorkflow, data?.templates, draftSpec]);

  const pipelineGraph = useMemo(
    () => (canvasSpec ? buildWorkflowPipelineGraph(canvasSpec) : { nodes: [], edges: [] }),
    [canvasSpec],
  );
  const inspectedNode = useMemo(
    () => pipelineGraph.nodes.find((n) => n.id === selectedNodeId) ?? pipelineGraph.nodes[0] ?? null,
    [pipelineGraph.nodes, selectedNodeId],
  );

  if (!data && !error) return <Pending page label="Reading your workflows" />;
  if (!data && error && errorStatus === 403) {
    return (
      <div className="mx-auto w-full max-w-6xl" data-testid="assist-workflows-view">
        <p
          className="rounded-xl border border-border bg-surface px-4 py-3 text-[13.5px] leading-relaxed text-muted"
          role={errorCode === "canary_route_refused" ? "status" : "alert"}
          data-testid="assist-workflows-restricted"
        >
          {error}
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-6xl space-y-6 pb-10" data-testid="assist-workflows-view">
      {error && (
        <p className="rounded-lg border border-risk-line bg-risk-soft px-3.5 py-2 text-[13px] text-muted">{error}</p>
      )}
      {toast && (
        <div
          role="status"
          data-testid="workflow-toast"
          className="flex items-center justify-between gap-2 rounded-xl border border-border bg-surface px-4 py-2.5 text-[13px] font-medium text-fg"
        >
          <span className="inline-flex items-center gap-2">
            <CheckCircle2 className="size-4 shrink-0 text-ok" aria-hidden />
            {toast}
          </span>
          <button
            type="button"
            aria-label="Dismiss message"
            onClick={() => setToast(null)}
            className="cursor-pointer rounded-md p-1 text-muted hover:bg-surface-2 hover:text-fg"
          >
            <X className="size-3.5" aria-hidden />
          </button>
        </div>
      )}
      {actionError && (
        <div
          role="alert"
          data-testid="workflow-action-error"
          className="flex items-center justify-between gap-2 rounded-xl border border-border bg-surface px-4 py-2.5 text-[13px] text-fg"
        >
          <span className="inline-flex items-center gap-2">
            <CircleAlert className="size-4 shrink-0 text-late" aria-hidden />
            {actionError}
          </span>
          <button
            type="button"
            aria-label="Dismiss error"
            onClick={() => setActionError(null)}
            className="cursor-pointer rounded-md p-1 text-muted hover:bg-surface-2 hover:text-fg"
          >
            <X className="size-3.5" aria-hidden />
          </button>
        </div>
      )}

      {/* Top Compact Composer + 4 Quick Petra Templates */}
      <CreateWorkflowPanel
        templates={data?.templates ?? []}
        routines={data?.routines ?? []}
        onCreated={handleCreated}
        composerRef={composerRef}
        onDraftPreview={(spec) => {
          setDraftSpec(spec);
          setSelectedNodeId("trigger");
        }}
      />

      {/* Split Layout: Elevated Workflow Cards + Interactive 4-Node DotGridCanvas Pipeline Preview */}
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(360px,46%)_1fr]">
        <div className="space-y-3">
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-[14px] font-semibold text-fg">
              Active workflows <span className="num font-normal text-muted">({workflows.length})</span>
            </h2>
            <button
              type="button"
              onClick={() => composerRef.current?.focus()}
              className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-surface px-2.5 text-[12px] font-medium text-fg transition-colors hover:border-border-strong hover:bg-surface-2 cursor-pointer"
            >
              <Plus className="size-3.5" aria-hidden />
              <span>New workflow</span>
            </button>
          </div>

          {workflows.length === 0 ? (
            <div className="rounded-xl border border-border bg-surface/50 p-6">
              <EmptyState
                title="No workflow set up yet."
                line="Describe one above, click a quick Petra template, or turn a mined routine into a workflow."
              />
            </div>
          ) : (
            <div className="space-y-3">
              {workflows.map((w) => (
                <WorkflowCard
                  key={w.workflow_id}
                  wf={w}
                  selected={!draftSpec && activeWorkflow?.workflow_id === w.workflow_id}
                  onSelect={() => {
                    setDraftSpec(null);
                    setSelectedWorkflowId(w.workflow_id);
                    setSelectedNodeId("trigger");
                  }}
                  onWorkflow={replaceWorkflow}
                  onDelete={(wf) => void handleDelete(wf)}
                  onJob={replaceJob}
                  onFeedback={showToast}
                />
              ))}
            </div>
          )}
        </div>

        {/* Interactive 4-Node DotGridCanvas Pipeline Preview */}
        <section
          className="space-y-3 xl:sticky xl:top-4 xl:self-start"
          aria-label="Workflow visual pipeline"
          data-testid="workflow-dot-grid-section"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="space-y-0.5">
              <h2 className="text-[14px] font-semibold text-fg">Pipeline preview</h2>
              <p className="text-[12.5px] text-muted">
                {canvasSpec
                  ? `${draftSpec ? "Draft preview: " : "Selected: "}${canvasSpec.name}`
                  : "Select any workflow to inspect its trigger, scope, action, and approval gate."}
              </p>
            </div>
            {draftSpec && (
              <button
                type="button"
                onClick={() => setDraftSpec(null)}
                className="rounded-md border border-border bg-surface px-2.5 py-1 text-[12px] font-medium text-muted transition-colors hover:border-border-strong hover:text-fg cursor-pointer"
              >
                Exit draft preview
              </button>
            )}
          </div>

          <DotGridCanvas
            nodes={pipelineGraph.nodes}
            edges={pipelineGraph.edges}
            selectedId={selectedNodeId}
            onSelectNode={setSelectedNodeId}
            height={280}
            emptyMessage="Describe a workflow or choose a template to visualize its pipeline."
            ariaLabel="Assist workflow pipeline canvas"
          />

          {inspectedNode && (
            <div
              className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-surface dark:bg-[#14161b] px-4 py-3 text-[12.5px]"
              data-testid="workflow-node-inspector"
            >
              <div className="min-w-0 flex-1 space-y-0.5">
                <p className="font-medium text-fg">
                  <span className="text-accent-text">{inspectedNode.kindLabel}</span>
                  <span className="mx-2 text-faint" aria-hidden>
                    ·
                  </span>
                  <span>{inspectedNode.title}</span>
                </p>
                {inspectedNode.description && <p className="text-muted">{inspectedNode.description}</p>}
              </div>
              {inspectedNode.badge && (
                <span className="shrink-0 rounded-md border border-border-strong bg-bg px-2 py-0.5 text-[11.5px] font-medium text-muted">
                  {inspectedNode.badge}
                </span>
              )}
            </div>
          )}
        </section>
      </div>
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
    <section className="mb-8 max-w-[820px]" data-testid="mined-routines-section">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="text-[14px] font-semibold text-fg">Mined routines from your past work</h2>
        <Link
          href="/assist?show=workflows"
          className="text-[12.5px] text-muted underline underline-offset-2 hover:text-fg"
        >
          Open Workflows
        </Link>
      </div>
      {error && <p className="mb-2 text-[13px] text-muted">{error}</p>}
      {routines.length === 0 ? (
        <p className="text-[13px] text-muted">No mined routines are on your account yet.</p>
      ) : (
        <ul className="divide-y divide-border border-t border-border">
          {routines.map((r) => (
            <li
              key={r.routine_id}
              className="flex flex-wrap items-center justify-between gap-3 py-3 text-[13.5px]"
              data-testid="routines-tab-mined-row"
            >
              <div className="min-w-0 flex-1 space-y-0.5">
                <p className="font-medium text-fg">
                  {r.suggested_name} <span className="font-normal text-muted">· {r.suggested_trigger_summary}</span>
                </p>
                {r.description && <p className="text-[12.5px] text-muted">{r.description}</p>}
                <p className="text-[12px] text-muted">
                  Observed {String(r.mined?.acts ?? 0)} acts
                  {r.mined?.days != null ? ` over ${String(r.mined.days)} days` : ""}
                </p>
              </div>
              {created[r.routine_id] ? (
                <Link
                  href="/assist?show=workflows"
                  className="text-[12.5px] font-medium text-ok underline underline-offset-2"
                >
                  Workflow created. Open it
                </Link>
              ) : (
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-8 rounded-md px-3 text-[13px] text-muted hover:bg-surface-2 hover:text-fg"
                  disabled={busyId === r.routine_id}
                  onClick={() => void make(r)}
                >
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
