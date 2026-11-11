"use client";

import { ArrowRight } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorNotice, LoadProblem } from "@/components/ui/notice";
import { Sheet } from "@/components/ui/sheet";
import { Pending } from "@/components/ui/skeleton";
import { getAutopilotWorkflow, setAutopilotMode } from "@/lib/api";
import { MODES, type Filters, type Mode, countdownText, filterWorkflows, modesFor, rowMode, rowPassed, rowRole, rowStateShown, workflowLabel } from "@/lib/autopilot";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { ago, formatDateTime, plural } from "@/lib/format";
import type { AutopilotArea as Area, AutopilotWorkflowDetail, AutopilotWorkflowRow } from "@/lib/types";

const pctText = (p: number | null | undefined) => (p == null ? "—" : `${p.toFixed(1)}%`);

/** The mode switch: five modes in one segmented control; a mode the row may not take is
 * drawn but disabled with why. Watch asks once more before it is set. */
export function ModeControl({
  row,
  area,
  builder,
  busy,
  onSet,
}: {
  row: AutopilotWorkflowRow;
  area: Area;
  builder: boolean;
  busy: boolean;
  onSet: (mode: Mode) => void;
}) {
  const mode = rowMode(row);
  const allowed = modesFor(row, builder);
  const [confirm, setConfirm] = useState<Mode | null>(null);
  const who = rowRole(area, row);
  const why = (m: Mode) =>
    m === "auto" || m === "watch"
      ? rowPassed(row)
        ? "Arrives with the workflow builder"
        : "Opens after its gate passes"
      : "Arrives with the workflow builder";
  return (
    <div className="flex flex-col items-start gap-1">
      <div role="radiogroup" aria-label={`${workflowLabel(row.workflow)} mode for ${who}`} className="inline-flex h-7 rounded-[4px] ring-1 ring-border">
        {MODES.map(({ mode: m, label, words }) => {
          const on = m === mode;
          const ok = allowed.has(m) && !area.switches.global_off;
          return (
            <button
              key={m}
              type="button"
              role="radio"
              aria-checked={on}
              aria-label={label}
              disabled={busy || !ok}
              title={ok ? words : `${label}: ${why(m)}`}
              onClick={() => (on ? undefined : m === "watch" ? setConfirm("watch") : onSet(m))}
              className={cn(
                "-ml-px h-7 px-2.5 text-[12px] first:ml-0 first:rounded-l-[4px] last:rounded-r-[4px] cursor-pointer disabled:cursor-not-allowed",
                on ? "bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] font-medium text-accent ring-1 ring-accent" : ok ? "text-fg hover:bg-hover" : "text-faint",
              )}
            >
              {label}
            </button>
          );
        })}
      </div>
      {confirm === "watch" && (
        <div className="flex flex-wrap items-center gap-2 text-[12px]" role="status">
          <span>Watch acts without asking you first; each action is shown with undo.</span>
          <Button size="sm" variant="outline" className="h-7" onClick={() => (setConfirm(null), onSet("watch"))}>
            Confirm Watch
          </Button>
          <Button size="sm" variant="ghost" className="h-7" onClick={() => setConfirm(null)}>
            Cancel
          </Button>
        </div>
      )}
    </div>
  );
}

function Gate({ row }: { row: AutopilotWorkflowRow }) {
  const s = row.fresh ?? row.dev;
  const c = row.countdown;
  return (
    <span className="block">
      <span className="flex items-baseline gap-2">
        <span className={cn("num text-[13px]", row.fresh ? "text-fg" : "text-muted")}>{pctText(s?.percent)}</span>
        <span className="num text-[11px] text-faint">{s ? `n ${s.judged}` : ""}</span>
        {!row.fresh && row.dev && <span className="text-[11px] text-faint">tuning</span>}
      </span>
      {c && (
        <span className="num block text-[11px] text-faint" data-testid="gate-countdown" title={countdownText(c) ?? undefined}>
          {c.judged}/{c.need_judged} items
        </span>
      )}
    </span>
  );
}

/** The countdown toward the write gate: a bar of judged fresh items against what the gate
 * needs and the precision so far against its bar. */
function Countdown({ row }: { row: AutopilotWorkflowRow }) {
  const c = row.countdown;
  if (!c) return <p className="text-[12px] text-muted">No fresh items judged yet: the count starts once the data refresh covers a whole new day.</p>;
  const share = Math.min(1, c.judged / Math.max(1, c.need_judged));
  const under = c.percent != null && c.percent < c.need_percent;
  return (
    <div className="space-y-1" data-testid="workflow-countdown">
      <div className="flex items-baseline justify-between gap-3 text-[12px]">
        <span className="num">{countdownText(c)}</span>
        <span className={cn("shrink-0 text-[11px]", c.passes ? "text-ok" : "text-faint")}>{c.passes ? "meets the gate" : "not yet"}</span>
      </div>
      <div className="h-1.5 w-full rounded-[2px] bg-hover" aria-hidden>
        <div className={cn("h-1.5 rounded-[2px]", c.passes ? "bg-ok" : under ? "bg-risk" : "bg-accent")} style={{ width: `${Math.round(share * 100)}%` }} />
      </div>
      {c.window?.length === 2 && (
        <p className="num text-[11px] text-faint">
          Judged on fresh days {formatDateTime(c.window[0])} to {formatDateTime(c.window[1])}
        </p>
      )}
    </div>
  );
}

/** The workflows: one row per workflow and person or role, its gate and mode; a row opens
 * the workflow's page. */
export function WorkflowsView({ area, filters, builder, onChanged }: { area: Area; filters: Filters; builder: boolean; onChanged: () => void }) {
  const rows = useMemo(() => filterWorkflows(area, filters), [area, filters]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const set = async (row: AutopilotWorkflowRow, mode: Mode) => {
    setBusy(`${row.role}/${row.workflow}`);
    setError(null);
    try {
      await setAutopilotMode(row.workflow, mode, mode === "off" ? null : row.role);
      onChanged();
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(null);
    }
  };
  if ((area.workflows ?? []).length === 0) return <EmptyState title="No workflow has been scored yet." line="Scores appear after the first replay of a role's days." />;
  if (rows.length === 0) return <EmptyState title="No workflow matches this view." />;
  const grid = "md:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)_7.5rem_minmax(0,1.1fr)_3.5rem_minmax(0,1.6fr)]";
  return (
    <div className="space-y-3">
      {error && <ErrorNotice message={error} />}
      <div className={cn("hidden gap-4 border-b border-border px-3 pb-1.5 text-[11px] text-muted md:grid", grid)} aria-hidden>
        <span>Workflow</span>
        <span>Person or role</span>
        <span>Gate</span>
        <span>State</span>
        <span className="text-right">Open</span>
        <span>Mode</span>
      </div>
      <ul className="divide-y divide-border" aria-label="Workflows">
        {rows.map((r) => {
          const shown = rowStateShown(r, area.owner);
          const k = `${r.role}/${r.workflow}`;
          return (
            <li key={k} className={cn("grid grid-cols-2 items-center gap-x-4 gap-y-1.5 px-3 py-2 text-[13px] hover:bg-hover/50", grid)} data-testid="autopilot-workflow">
              <button type="button" className="col-span-2 flex min-w-0 items-center gap-1.5 text-left font-medium hover:underline md:col-span-1 cursor-pointer" onClick={() => setOpen(r.workflow)}>
                <span className="truncate">{workflowLabel(r.workflow)}</span>
              </button>
              <span className="truncate text-muted md:text-fg">{rowRole(area, r)}</span>
              <span>
                <Gate row={r} />
                {r.dev && r.fresh && <span className="sr-only">tuning {pctText(r.dev.percent)}</span>}
              </span>
              <span className="text-[12px]">
                <span className={cn(shown.status === "ok" ? "text-ok" : shown.status === "new" ? "text-accent" : shown.status === "late" ? "text-late" : shown.status === "risk" ? "text-risk" : "text-muted")}>{shown.label}</span>
                <span className="block text-[11px] text-faint">{r.last_run ? `ran ${ago(r.last_run)}` : "not run yet"}</span>
              </span>
              <span className="num text-right text-[13px]">{r.open_items || ""}</span>
              <span className="col-span-2 md:col-span-1">
                {area.owner ? (
                  <ModeControl row={r} area={area} builder={builder} busy={busy === k} onSet={(m) => void set(r, m)} />
                ) : (
                  <span className="text-[12px] text-muted">{MODES.find((m) => m.mode === rowMode(r))?.label}</span>
                )}
              </span>
            </li>
          );
        })}
      </ul>
      <WorkflowSheet workflow={open} area={area} builder={builder} onClose={() => setOpen(null)} onChanged={onChanged} />
    </div>
  );
}

function Steps({ title, steps, empty }: { title: string; steps: { kind: string; words: string }[]; empty: string }) {
  return (
    <div>
      <h3 className="mb-1 text-[11px] font-semibold tracking-[0.04em] text-muted uppercase">{title}</h3>
      {steps.length === 0 ? (
        <p className="text-[12px] text-muted">{empty}</p>
      ) : (
        <ol className="space-y-1">
          {steps.map((s, i) => (
            <li key={`${s.kind}-${i}`} className="flex items-baseline gap-2 text-[13px]">
              <span className="num w-4 shrink-0 text-[11px] text-faint">{i + 1}</span>
              <span>{s.words}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/** A workflow's page in the sheet: what starts it, what it checks and does, its gate,
 * what it caught and missed, hours saved, its mode per person or role and its history. */
function WorkflowSheet({ workflow, area, builder, onClose, onChanged }: { workflow: string | null; area: Area; builder: boolean; onClose: () => void; onChanged: () => void }) {
  const [detail, setDetail] = useState<AutopilotWorkflowDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [modeError, setModeError] = useState<string | null>(null);
  useEffect(() => {
    if (!workflow) return;
    const ctl = new AbortController();
    setDetail(null);
    setError(null);
    getAutopilotWorkflow(workflow, ctl.signal).then(setDetail, (e) => !ctl.signal.aborted && setError(e));
    return () => ctl.abort();
  }, [workflow]);
  const set = async (row: AutopilotWorkflowRow, mode: Mode) => {
    if (!workflow) return;
    setBusy(true);
    setModeError(null);
    try {
      setDetail(await setAutopilotMode(workflow, mode, mode === "off" ? null : row.role));
      onChanged();
    } catch (e) {
      setModeError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const d = detail;
  return (
    <Sheet open={!!workflow} onOpenChange={(o) => !o && onClose()} title={workflow ? workflowLabel(workflow) : "Workflow"}>
      <div className="space-y-6 px-5 py-4 text-[13px]" data-testid="workflow-sheet">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={() => setDetail(null)} />
        ) : !d ? (
          <Pending label="Reading the workflow" />
        ) : (
          <>
            <header>
              <h2 className="text-[17px] font-semibold tracking-[-0.01em]">{d.label}</h2>
              <p className="text-[12px] text-muted">
                {workflowLabel(d.workflow)} · {MODES.find((m) => m.mode === d.mode)?.label} mode · {plural(d.open_items, "open item")}
              </p>
            </header>
            <dl className="grid grid-cols-4 gap-4 border-y border-border py-3" data-testid="workflow-figures">
              <div>
                <dt className="text-[11px] text-muted">Gate</dt>
                <dd className={cn("num text-[17px]", d.gate.passed ? "text-ok" : "text-fg")}>{pctText(d.gate.percent)}</dd>
                <dd className="num text-[11px] text-faint">n {d.gate.judged}{d.gate.fresh ? " held-out" : " tuning"}</dd>
              </div>
              <div>
                <dt className="text-[11px] text-muted">Caught</dt>
                <dd className="num text-[17px]">{d.caught ?? "—"}</dd>
              </div>
              <div>
                <dt className="text-[11px] text-muted">Missed</dt>
                <dd className={cn("num text-[17px]", (d.missed ?? 0) > 0 && "text-risk")}>{d.missed ?? "—"}</dd>
                {d.false_alarms == null && <dd className="text-[11px] text-faint">false alarms not counted</dd>}
              </div>
              <div>
                <dt className="text-[11px] text-muted">Hours saved</dt>
                <dd className="num text-[17px]">{d.hours_saved ?? "—"}</dd>
                {d.hours_basis && <dd className="text-[11px] leading-4 text-faint">{d.hours_basis}</dd>}
              </div>
            </dl>
            <p className="text-[12px] text-muted">
              {d.gate.passed ? "Passed its gate" : "Not passed yet"}: {d.gate.rule}.
            </p>
            <div className="grid gap-5 sm:grid-cols-[1fr_auto_1fr_auto_1fr] sm:items-start" data-testid="workflow-steps">
              <Steps title="When" steps={d.trigger ? [d.trigger] : []} empty="No registered trigger." />
              <ArrowRight className="hidden size-3.5 text-faint sm:mt-6 sm:block" aria-hidden />
              <Steps title="If" steps={d.conditions ?? []} empty="Always." />
              <ArrowRight className="hidden size-3.5 text-faint sm:mt-6 sm:block" aria-hidden />
              <Steps title="Then" steps={d.actions ?? []} empty="No action registered." />
            </div>
            {(d.rows ?? []).length > 0 && (
              <section>
                <h3 className="mb-1 text-[11px] font-semibold tracking-[0.04em] text-muted uppercase">Mode and gate countdown by person or role</h3>
                {modeError && <ErrorNotice message={modeError} />}
                <ul className="divide-y divide-border">
                  {(d.rows ?? []).map((r) => (
                    <li key={r.role} className="space-y-2 py-2">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span>
                          {rowRole(area, r)} <span className="num text-[12px] text-muted">{r.dev ? `practice ${pctText(r.dev.percent)} of ${r.dev.judged}` : "no practice score"}</span>
                          {r.draft && <span className="ml-2 text-[12px] text-accent">Draft: prepared for approval, nothing goes out without accept and confirm</span>}
                        </span>
                        {area.owner ? <ModeControl row={r} area={area} builder={builder || (d.modes ?? []).includes("suggest")} busy={busy} onSet={(m) => void set(r, m)} /> : <span className="text-muted">{MODES.find((m) => m.mode === rowMode(r))?.label}</span>}
                      </div>
                      <Countdown row={r} />
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {(d.notes ?? []).map((n) => (
              <p key={n} className="text-[12px] text-muted">
                {n}
              </p>
            ))}
            <section>
              <h3 className="mb-1 text-[11px] font-semibold tracking-[0.04em] text-muted uppercase">History</h3>
              {(d.history ?? []).length === 0 ? (
                <p className="text-[12px] text-muted">{area.owner ? "Nothing on the audit log for it yet." : "The history is the owner's."}</p>
              ) : (
                <ul className="space-y-1.5">
                  {(d.history ?? []).map((h, i) => (
                    <li key={`${h.at}-${i}`} className="flex items-baseline gap-3 text-[12px]">
                      <span className="num w-36 shrink-0 whitespace-nowrap text-muted">{formatDateTime(h.at)}</span>
                      <span>{h.event === "gate_passed" ? "Gate passed" : h.event === "switch" ? "Switch changed" : (h.status ?? h.event).replace(/_/g, " ")}</span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </>
        )}
      </div>
    </Sheet>
  );
}
