"use client";

import { Bell, CheckCircle2, Clock3, Loader2, Mail, Plus, ShieldAlert, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { createAssistWorkflow, type AssistWorkflow } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import type { InsightList } from "@/lib/types";

export type TriggerConditionOp = "exceeds" | "drops_below" | "changes_pct" | "any_new";
export type TriggerScheduleId = "hourly" | "daily_8am" | "weekdays_8am" | "weekly_mon_8am";
export type TriggerDeliveryId = "home" | "email" | "both";

export interface CreateTriggerModalProps {
  open: boolean;
  onClose: () => void;
  measures?: NonNullable<InsightList["measures"]>;
  initialMeasureName?: string;
  initialPrompt?: string;
  onCreated?: (workflow: AssistWorkflow) => void;
}

interface TriggerMetricOption {
  id: string;
  label: string;
  defaultOp: TriggerConditionOp;
  defaultThreshold: string;
  unitHint: string;
  group: "Operational" | "Governed measure" | "Custom";
}

const OPERATIONAL_PRESETS: readonly TriggerMetricOption[] = [
  {
    id: "late_pos",
    label: "Late purchase orders",
    defaultOp: "exceeds",
    defaultThreshold: "10",
    unitHint: "orders",
    group: "Operational",
  },
  {
    id: "open_qc_holds",
    label: "Open QC holds",
    defaultOp: "any_new",
    defaultThreshold: "1",
    unitHint: "lots",
    group: "Operational",
  },
  {
    id: "uninvoiced_shipments",
    label: "Uninvoiced shipments",
    defaultOp: "exceeds",
    defaultThreshold: "5",
    unitHint: "shipments",
    group: "Operational",
  },
  {
    id: "overdue_ar",
    label: "Overdue AR",
    defaultOp: "exceeds",
    defaultThreshold: "50000",
    unitHint: "USD",
    group: "Operational",
  },
  {
    id: "margin_below_target",
    label: "Margin below target",
    defaultOp: "drops_below",
    defaultThreshold: "25",
    unitHint: "%",
    group: "Operational",
  },
];

const CONDITION_OPTIONS: readonly { id: TriggerConditionOp; label: string; phrase: string }[] = [
  { id: "exceeds", label: "Exceeds", phrase: "exceeds" },
  { id: "drops_below", label: "Drops below", phrase: "drops below" },
  { id: "changes_pct", label: "Changes by %", phrase: "changes by more than" },
  { id: "any_new", label: "Any new item", phrase: "has any new items" },
];

const SCHEDULE_OPTIONS: readonly {
  id: TriggerScheduleId;
  label: string;
  trigger: Record<string, unknown>;
}[] = [
  { id: "hourly", label: "Every hour", trigger: { cadence: "hourly" } },
  { id: "daily_8am", label: "Daily · 8:00 AM ET", trigger: { cadence: "daily", hour: 8, minute: 0 } },
  { id: "weekdays_8am", label: "Weekdays · 8:00 AM ET", trigger: { cadence: "weekdays", hour: 8, minute: 0 } },
  {
    id: "weekly_mon_8am",
    label: "Weekly · Monday 8:00 AM ET",
    trigger: { cadence: "weekly", weekday: "monday", hour: 8, minute: 0 },
  },
];

const DELIVERY_OPTIONS: readonly { id: TriggerDeliveryId; label: string; instructionSuffix: string }[] = [
  { id: "home", label: "Home alert", instructionSuffix: "Surface an alert on Home when triggered." },
  { id: "email", label: "Email digest", instructionSuffix: "Prepare a summary email digest when triggered." },
  {
    id: "both",
    label: "Home + Email",
    instructionSuffix: "Surface an alert on Home and prepare an email digest when triggered.",
  },
];

/**
 * Real in-page Trigger / Monitor builder that submits directly to `POST /api/assist/workflows`
 * with a clean schedule, threshold rule, and delivery channel.
 */
export function CreateTriggerModal({
  open,
  onClose,
  measures = [],
  initialMeasureName,
  initialPrompt,
  onCreated,
}: CreateTriggerModalProps) {
  const metricOptions = useMemo<TriggerMetricOption[]>(() => {
    const out: TriggerMetricOption[] = [...OPERATIONAL_PRESETS];
    const seen = new Set(out.map((o) => o.label.toLowerCase()));
    for (const m of measures) {
      if (seen.has(m.name.toLowerCase())) continue;
      seen.add(m.name.toLowerCase());
      out.push({
        id: `measure:${m.id}`,
        label: m.name,
        defaultOp: m.unit === "money" ? "drops_below" : "exceeds",
        defaultThreshold: m.unit === "money" ? "100000" : "10",
        unitHint: m.unit === "money" ? "USD" : m.unit || "units",
        group: "Governed measure",
      });
    }
    out.push({
      id: "custom",
      label: "Custom condition",
      defaultOp: "exceeds",
      defaultThreshold: "",
      unitHint: "",
      group: "Custom",
    });
    return out;
  }, [measures]);

  const [metricId, setMetricId] = useState<string>("late_pos");
  const [customCondition, setCustomCondition] = useState<string>("");
  const [conditionOp, setConditionOp] = useState<TriggerConditionOp>("exceeds");
  const [threshold, setThreshold] = useState<string>("10");
  const [scheduleId, setScheduleId] = useState<TriggerScheduleId>("weekdays_8am");
  const [deliveryId, setDeliveryId] = useState<TriggerDeliveryId>("home");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createdWorkflow, setCreatedWorkflow] = useState<AssistWorkflow | null>(null);

  useEffect(() => {
    if (!open) return;
    setCreatedWorkflow(null);
    setError(null);
    setSubmitting(false);

    if (initialPrompt && initialPrompt.trim()) {
      setMetricId("custom");
      setCustomCondition(initialPrompt.trim());
      setConditionOp("any_new");
      setThreshold("");
      return;
    }

    if (initialMeasureName) {
      const match = metricOptions.find(
        (opt) => opt.label.toLowerCase() === initialMeasureName.trim().toLowerCase(),
      );
      if (match) {
        setMetricId(match.id);
        setConditionOp(match.defaultOp);
        setThreshold(match.defaultThreshold);
        return;
      }
      setMetricId("custom");
      setCustomCondition(initialMeasureName.trim());
      setConditionOp("exceeds");
      setThreshold("10");
      return;
    }

    setMetricId("late_pos");
    setConditionOp("exceeds");
    setThreshold("10");
    setCustomCondition("");
  }, [open, initialMeasureName, initialPrompt, metricOptions]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const activeMetric = useMemo(
    () => metricOptions.find((m) => m.id === metricId) ?? OPERATIONAL_PRESETS[0],
    [metricOptions, metricId],
  );
  const activeSchedule = useMemo(
    () => SCHEDULE_OPTIONS.find((s) => s.id === scheduleId) ?? SCHEDULE_OPTIONS[2],
    [scheduleId],
  );
  const activeDelivery = useMemo(
    () => DELIVERY_OPTIONS.find((d) => d.id === deliveryId) ?? DELIVERY_OPTIONS[0],
    [deliveryId],
  );
  const activeCondition = useMemo(
    () => CONDITION_OPTIONS.find((c) => c.id === conditionOp) ?? CONDITION_OPTIONS[0],
    [conditionOp],
  );

  const isCustom = metricId === "custom";
  const needsValue = conditionOp !== "any_new";

  const previewSummary = useMemo(() => {
    const subject = isCustom ? customCondition.trim() || "Custom condition" : activeMetric.label;
    const val = threshold.trim();
    const unit = conditionOp === "changes_pct" ? "%" : activeMetric.unitHint ? ` ${activeMetric.unitHint}` : "";
    const ruleText =
      conditionOp === "any_new"
        ? `Check ${subject} and alert on any new items.`
        : `Check ${subject} and alert when it ${activeCondition.phrase} ${val || "threshold"}${unit}.`;
    return {
      name: isCustom ? subject.slice(0, 80) : `${subject} monitor`,
      instruction: `${ruleText} ${activeDelivery.instructionSuffix}`,
      ruleText,
    };
  }, [isCustom, customCondition, activeMetric, threshold, conditionOp, activeCondition, activeDelivery]);

  if (!open) return null;

  const canSubmit =
    !submitting &&
    (!isCustom || customCondition.trim().length > 2) &&
    (!needsValue || threshold.trim().length > 0);

  const handleSelectMetric = (opt: TriggerMetricOption) => {
    setMetricId(opt.id);
    setConditionOp(opt.defaultOp);
    setThreshold(opt.defaultThreshold);
    setError(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const wf = await createAssistWorkflow({
        name: previewSummary.name,
        instruction: previewSummary.instruction,
        trigger_kind: "schedule",
        trigger: activeSchedule.trigger,
      });
      setCreatedWorkflow(wf);
      onCreated?.(wf);
    } catch (err) {
      setError(plainError(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-xs"
      role="dialog"
      aria-modal="true"
      aria-label="Create trigger or monitor"
      data-testid="create-trigger-modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="flex max-h-[88vh] w-full max-w-[600px] flex-col overflow-hidden rounded-2xl border border-border-strong/80 bg-raised shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_24px_60px_-16px_rgba(0,0,0,0.65)] dark:bg-[#141519]">
        {/* Header */}
        <div className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h2 className="font-display text-[16px] font-semibold tracking-[-0.02em] text-fg">
              New trigger &amp; monitor
            </h2>
            <p className="mt-0.5 text-[12.5px] text-muted">
              Watch a governed measure or operational condition on a schedule and get alerted automatically.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close trigger builder"
            className="inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg cursor-pointer"
          >
            <X className="size-4" strokeWidth={2} aria-hidden />
          </button>
        </div>

        {createdWorkflow ? (
          <div className="space-y-4 px-5 py-6" data-testid="create-trigger-success">
            <div className="flex items-start gap-3 rounded-xl border border-border-strong bg-surface/60 p-4">
              <CheckCircle2 className="mt-0.5 size-5 shrink-0 text-ok" strokeWidth={2} aria-hidden />
              <div className="min-w-0 space-y-1">
                <h3 className="text-[14px] font-semibold text-fg">Trigger created</h3>
                <p className="text-[13px] text-fg/90">{createdWorkflow.name}</p>
                <p className="text-[12px] text-muted">
                  {createdWorkflow.trigger_summary || activeSchedule.label} · {activeDelivery.label}
                </p>
              </div>
            </div>
            <div className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => setCreatedWorkflow(null)}
                className="h-8 rounded-lg border border-border px-3 text-[12.5px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
              >
                Create another
              </button>
              <button
                type="button"
                onClick={onClose}
                className="h-8 rounded-lg bg-fg px-3.5 text-[12.5px] font-medium text-bg hover:opacity-90 cursor-pointer"
              >
                Done
              </button>
            </div>
          </div>
        ) : (
          <form onSubmit={(e) => void handleSubmit(e)} className="flex min-h-0 flex-1 flex-col">
            <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-4">
              {/* 1. Metric / Condition Picker */}
              <div className="space-y-2">
                <label className="block text-[12px] font-medium text-fg">
                  Measure or operational condition
                </label>
                <div className="flex flex-wrap gap-1.5" role="group" aria-label="Trigger preset conditions">
                  {OPERATIONAL_PRESETS.map((opt) => {
                    const active = metricId === opt.id;
                    return (
                      <button
                        key={opt.id}
                        type="button"
                        aria-pressed={active}
                        onClick={() => handleSelectMetric(opt)}
                        data-testid={`trigger-preset-${opt.id}`}
                        className={cn(
                          "inline-flex h-7 items-center gap-1.5 rounded-lg border px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
                          active
                            ? "border-border-strong bg-surface-2 text-fg"
                            : "border-border/80 bg-bg/60 text-muted hover:border-border-strong hover:text-fg",
                        )}
                      >
                        <ShieldAlert className="size-3 shrink-0 opacity-75" strokeWidth={1.9} aria-hidden />
                        {opt.label}
                      </button>
                    );
                  })}
                  <button
                    type="button"
                    aria-pressed={isCustom}
                    onClick={() => {
                      setMetricId("custom");
                      setError(null);
                    }}
                    data-testid="trigger-preset-custom"
                    className={cn(
                      "inline-flex h-7 items-center gap-1 rounded-lg border px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
                      isCustom
                        ? "border-border-strong bg-surface-2 text-fg"
                        : "border-border/80 bg-bg/60 text-muted hover:border-border-strong hover:text-fg",
                    )}
                  >
                    <Plus className="size-3 shrink-0" strokeWidth={2} aria-hidden />
                    Custom
                  </button>
                </div>

                {measures.length > 0 && (
                  <label className="block pt-1">
                    <span className="sr-only">Or select a governed measure</span>
                    <select
                      value={metricId.startsWith("measure:") ? metricId : ""}
                      onChange={(e) => {
                        const hit = metricOptions.find((m) => m.id === e.target.value);
                        if (hit) handleSelectMetric(hit);
                      }}
                      aria-label="Select governed measure"
                      className="h-8 w-full rounded-lg border border-border bg-bg/80 px-2.5 text-[12.5px] text-fg"
                    >
                      <option value="">Or pick from governed measures ({measures.length})</option>
                      {measures.map((m) => (
                        <option key={m.id} value={`measure:${m.id}`}>
                          {m.name} ({m.owner})
                        </option>
                      ))}
                    </select>
                  </label>
                )}

                {isCustom && (
                  <input
                    type="text"
                    required
                    value={customCondition}
                    onChange={(e) => setCustomCondition(e.target.value)}
                    placeholder="Example: Open orders for Croda Canada past promised ship date"
                    aria-label="Custom trigger condition"
                    data-testid="trigger-custom-input"
                    className="h-9 w-full rounded-lg border border-border-strong bg-bg px-3 text-[13px] text-fg placeholder:text-faint"
                  />
                )}
              </div>

              {/* 2. Threshold & Condition */}
              <div className="space-y-2">
                <span className="block text-[12px] font-medium text-fg">Threshold rule</span>
                <div className="flex flex-wrap items-center gap-2">
                  <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Condition operator">
                    {CONDITION_OPTIONS.map((op) => {
                      const active = conditionOp === op.id;
                      return (
                        <button
                          key={op.id}
                          type="button"
                          aria-pressed={active}
                          onClick={() => setConditionOp(op.id)}
                          data-testid={`trigger-op-${op.id}`}
                          className={cn(
                            "h-8 rounded-lg border px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
                            active
                              ? "border-border-strong bg-surface-2 text-fg"
                              : "border-border/80 bg-bg/60 text-muted hover:border-border-strong hover:text-fg",
                          )}
                        >
                          {op.label}
                        </button>
                      );
                    })}
                  </div>
                  {needsValue && (
                    <div className="flex items-center gap-1.5">
                      <input
                        type="number"
                        step="any"
                        required
                        value={threshold}
                        onChange={(e) => setThreshold(e.target.value)}
                        aria-label="Threshold value"
                        data-testid="trigger-threshold-input"
                        placeholder="10"
                        className="num h-8 w-28 rounded-lg border border-border-strong bg-bg px-2.5 text-[13px] text-fg"
                      />
                      <span className="text-[12px] text-muted">
                        {conditionOp === "changes_pct" ? "%" : activeMetric.unitHint}
                      </span>
                    </div>
                  )}
                </div>
              </div>

              {/* 3. Schedule */}
              <div className="space-y-2">
                <span className="flex items-center gap-1.5 text-[12px] font-medium text-fg">
                  <Clock3 className="size-3.5 text-muted" strokeWidth={1.9} aria-hidden />
                  Schedule
                </span>
                <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2" role="group" aria-label="Trigger schedule">
                  {SCHEDULE_OPTIONS.map((opt) => {
                    const active = scheduleId === opt.id;
                    return (
                      <button
                        key={opt.id}
                        type="button"
                        aria-pressed={active}
                        onClick={() => setScheduleId(opt.id)}
                        data-testid={`trigger-schedule-${opt.id}`}
                        className={cn(
                          "rounded-lg border px-3 py-2 text-left text-[12.5px] font-medium transition-colors cursor-pointer",
                          active
                            ? "border-border-strong bg-surface-2 text-fg"
                            : "border-border/80 bg-bg/60 text-muted hover:border-border-strong hover:text-fg",
                        )}
                      >
                        {opt.label}
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* 4. Delivery */}
              <div className="space-y-2">
                <span className="flex items-center gap-1.5 text-[12px] font-medium text-fg">
                  <Bell className="size-3.5 text-muted" strokeWidth={1.9} aria-hidden />
                  Delivery
                </span>
                <div className="liquid-glass-pill inline-flex items-center gap-1 rounded-xl p-1" role="group" aria-label="Alert delivery">
                  {DELIVERY_OPTIONS.map((opt) => {
                    const active = deliveryId === opt.id;
                    return (
                      <button
                        key={opt.id}
                        type="button"
                        aria-pressed={active}
                        onClick={() => setDeliveryId(opt.id)}
                        data-testid={`trigger-delivery-${opt.id}`}
                        className={cn(
                          "inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[12px] font-medium transition-colors cursor-pointer",
                          active ? "liquid-glass-active" : "text-muted hover:text-fg",
                        )}
                      >
                        {opt.id === "email" && <Mail className="size-3 shrink-0" aria-hidden />}
                        {opt.label}
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* Live Rule Preview */}
              <div className="rounded-xl border border-border/80 bg-bg/70 px-3.5 py-2.5 text-[12px]">
                <div className="text-[10.5px] font-semibold uppercase tracking-[0.04em] text-faint">
                  Trigger summary
                </div>
                <p className="mt-0.5 text-fg/90">{previewSummary.ruleText}</p>
                <p className="mt-0.5 text-muted">
                  {activeSchedule.label} · {activeDelivery.label}
                </p>
              </div>

              {error && (
                <p role="alert" className="text-[12.5px] text-late" data-testid="create-trigger-error">
                  {error}
                </p>
              )}
            </div>

            {/* Footer */}
            <div className="flex items-center justify-end gap-2 border-t border-border px-5 py-3.5">
              <button
                type="button"
                onClick={onClose}
                className="h-8 rounded-lg border border-border px-3 text-[12.5px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={!canSubmit}
                data-testid="create-trigger-submit"
                className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-fg px-3.5 text-[12.5px] font-medium text-bg hover:opacity-90 disabled:opacity-50 cursor-pointer"
              >
                {submitting ? (
                  <Loader2 className="size-3.5 animate-spin" aria-hidden />
                ) : (
                  <Bell className="size-3.5" strokeWidth={2.1} aria-hidden />
                )}
                Create trigger
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
