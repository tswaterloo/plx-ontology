"use client";

import { CalendarClock, Check, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";
import { createAssistWorkflow, type AssistWorkflow } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";

export interface ScheduleCadenceOption {
  id: "weekdays_8am" | "monday_8am" | "daily_8am" | "every_6h";
  label: string;
  trigger: Record<string, unknown>;
}

export const SCHEDULE_CADENCES: readonly ScheduleCadenceOption[] = [
  { id: "weekdays_8am", label: "Every weekday 8 AM", trigger: { cadence: "weekdays", hour: 8, minute: 0 } },
  { id: "monday_8am", label: "Every Monday 8 AM", trigger: { cadence: "weekly", weekday: "monday", hour: 8, minute: 0 } },
  { id: "daily_8am", label: "Daily 8 AM", trigger: { cadence: "daily", hour: 8, minute: 0 } },
  { id: "every_6h", label: "Every 6 hours", trigger: { cadence: "hourly", every_hours: 6 } },
];

/**
 * Popover on any Ask answer to turn the question into a recurring Assist workflow
 * via POST /api/assist/workflows, with a clear confirmation toast linking to /assist?show=workflows.
 */
export function AskSchedulePopover({
  question,
  spaceLabel,
  className,
}: {
  question: string;
  spaceLabel?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [cadenceId, setCadenceId] = useState<ScheduleCadenceOption["id"]>("weekdays_8am");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [created, setCreated] = useState<{ workflow: AssistWorkflow; cadenceLabel: string } | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const noteId = useId();

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const cleanQuestion = question.trim();
  if (!cleanQuestion) return null;

  const selected = SCHEDULE_CADENCES.find((c) => c.id === cadenceId) ?? SCHEDULE_CADENCES[0];

  const submit = async () => {
    if (busy) return;
    setBusy(true);
    setProblem(null);
    try {
      const parts = [
        spaceLabel ? `[${spaceLabel}] ${cleanQuestion}` : cleanQuestion,
        note.trim() ? `Delivery note: ${note.trim()}` : "",
      ].filter(Boolean);
      const wf = await createAssistWorkflow({
        name: cleanQuestion.slice(0, 120),
        instruction: parts.join("\n"),
        trigger_kind: "schedule",
        trigger: selected.trigger,
      });
      setCreated({ workflow: wf, cadenceLabel: selected.label });
      setNote("");
      setOpen(false);
    } catch (e) {
      setProblem(plainError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div ref={wrapRef} className={cn("relative inline-flex items-center gap-2", className)}>
      <button
        type="button"
        onClick={() => {
          setOpen((v) => !v);
          setProblem(null);
        }}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label="Schedule update"
        title="Schedule recurring update"
        data-testid="schedule-update-trigger"
        className={cn(
          "inline-flex h-7 items-center gap-1.5 rounded-lg border border-transparent px-2 py-1 text-[12px] font-medium text-muted transition-all duration-150 hover:border-border hover:bg-surface-2 hover:text-fg active:scale-[0.98] cursor-pointer",
          open && "border-border bg-surface-2 text-fg",
        )}
      >
        <CalendarClock className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
        <span>Schedule</span>
      </button>

      {created && !open && (
        <span
          role="status"
          data-testid="schedule-update-toast"
          className="inline-flex items-center gap-1.5 rounded-lg border border-ok-line bg-ok-soft px-2.5 py-1 text-[12px] text-ok"
        >
          <Check className="size-3.5 shrink-0" strokeWidth={2.2} aria-hidden />
          <span>Scheduled ({created.cadenceLabel})</span>
          <Link
            href="/assist?show=workflows"
            className="font-medium underline underline-offset-2 hover:opacity-80"
          >
            View in Workflows
          </Link>
        </span>
      )}

      {open && (
        <div
          role="dialog"
          aria-label="Schedule recurring update"
          data-testid="schedule-update-popover"
          className="absolute bottom-full left-0 z-40 mb-2 w-[312px] max-w-[calc(100vw-2rem)] rounded-2xl border border-border-strong bg-raised p-3.5 shadow-pop"
        >
          <div className="mb-2.5 flex items-center justify-between gap-2">
            <h4 className="text-[13px] font-semibold text-fg">Schedule recurring update</h4>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="Close schedule popover"
              className="inline-flex size-6 items-center justify-center rounded-md text-faint transition-colors duration-150 hover:bg-surface-2 hover:text-fg active:scale-[0.96] cursor-pointer"
            >
              <X className="size-3.5" strokeWidth={2} aria-hidden />
            </button>
          </div>

          <p className="mb-3 line-clamp-2 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-[12px] leading-4 text-muted">
            {cleanQuestion}
          </p>

          <div className="space-y-1" role="radiogroup" aria-label="Update cadence">
            {SCHEDULE_CADENCES.map((c) => {
              const active = c.id === cadenceId;
              return (
                <button
                  key={c.id}
                  type="button"
                  role="radio"
                  aria-checked={active}
                  onClick={() => setCadenceId(c.id)}
                  data-testid={`cadence-${c.id}`}
                  className={cn(
                    "flex h-8 w-full items-center justify-between rounded-lg border px-2.5 text-left text-[12.5px] transition-all duration-150 active:scale-[0.99] cursor-pointer",
                    active
                      ? "border-border-strong bg-surface-2 font-medium text-fg"
                      : "border-transparent text-muted hover:bg-hover hover:text-fg",
                  )}
                >
                  <span>{c.label}</span>
                  {active && <Check className="size-3.5 text-fg" strokeWidth={2.2} aria-hidden />}
                </button>
              );
            })}
          </div>

          <div className="mt-3">
            <label htmlFor={noteId} className="mb-1 block text-[11.5px] font-medium text-muted">
              Delivery note (optional)
            </label>
            <input
              id={noteId}
              type="text"
              value={note}
              onChange={(e) => setNote(e.target.value.slice(0, 300))}
              placeholder="e.g. Flag changes over 10% or draft summary"
              className="h-8 w-full rounded-lg border border-border bg-bg px-2.5 text-[12.5px] text-fg placeholder:text-faint transition-all duration-150 focus:border-fg/30 focus:ring-2 focus:ring-fg/10 focus:outline-none focus-visible:outline-none"
            />
          </div>

          {problem && (
            <p role="alert" className="mt-2 text-[12px] text-late">
              {problem}
            </p>
          )}

          <div className="mt-3.5 flex items-center justify-end gap-1.5">
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="inline-flex h-8 items-center rounded-lg px-2.5 text-[12.5px] text-muted transition-all duration-150 hover:bg-surface-2 hover:text-fg active:scale-[0.98] cursor-pointer"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={busy}
              data-testid="schedule-update-submit"
              className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-fg px-3 text-[12.5px] font-medium text-bg transition-all duration-150 hover:opacity-90 active:scale-[0.97] disabled:opacity-50 cursor-pointer"
            >
              {busy ? "Scheduling" : "Schedule"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
