"use client";

import { Plus, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorNotice, LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { draftAutopilotWorkflow, getAutopilotSchedules, replayAutopilotWorkflow } from "@/lib/api";
import { personName, workflowLabel } from "@/lib/autopilot";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { ago, formatDate } from "@/lib/format";
import type { AutopilotArea as Area, AutopilotCatalog, AutopilotReplay, AutopilotSchedules, AutopilotStep } from "@/lib/types";

const control = "h-8 min-w-0 rounded-[4px] border border-border bg-bg px-2 text-[13px] text-fg";
const label = "text-[11px] font-semibold tracking-[0.04em] text-muted uppercase";

type Cond = { kind: string; value: string };

/** The workflow builder: When (one trigger), If (conditions), Then (actions), for whom;
 * kept as a draft, tried on past days, and only then switched on from its workflow page.
 * Nothing here goes live. */
export function BuilderView({ area, catalog }: { area: Area; catalog: AutopilotCatalog | null }) {
  const [name, setName] = useState("");
  const [trigger, setTrigger] = useState("");
  const [conds, setConds] = useState<Cond[]>([]);
  const [actions, setActions] = useState<string[]>([]);
  const [person, setPerson] = useState("");
  const [kept, setKept] = useState<string | null>(null);
  const [days, setDays] = useState(14);
  const [replay, setReplay] = useState<AutopilotReplay | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!catalog) return <Pending label="Reading what the builder offers" />;
  const step = (list: AutopilotStep[], kind: string) => list.find((s) => s.kind === kind) ?? { kind, words: kind };
  const ready = !!name.trim() && !!trigger && actions.length > 0;
  const keep = async () => {
    setBusy(true);
    setError(null);
    try {
      const out = await draftAutopilotWorkflow({
        label: name.trim(),
        trigger: step(catalog.triggers, trigger),
        conditions: conds.filter((c) => c.kind).map((c) => ({ ...step(catalog.conditions, c.kind), params: c.value ? { value: c.value } : {} })),
        actions: actions.map((a) => step(catalog.actions, a)),
        person: person || null,
        role: null,
      });
      setKept(out.workflow);
      setReplay(null);
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const run = async () => {
    if (!kept) return;
    setBusy(true);
    setError(null);
    try {
      setReplay(await replayAutopilotWorkflow(kept, days));
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const people = area.people ?? [];
  return (
    <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_320px]" data-testid="autopilot-builder">
      <div className="space-y-6">
        {!catalog.builder && <p className="text-[12px] text-muted">This server lists what a workflow can use; keeping and trying a draft arrive with the builder.</p>}
        <label className="block space-y-1">
          <span className={label}>Name</span>
          <input className={cn(control, "w-full")} value={name} maxLength={120} onChange={(e) => setName(e.target.value)} placeholder="Chase a supplier PO with no confirmation" />
        </label>
        <div className="space-y-1">
          <span className={label}>When</span>
          <select className={cn(control, "w-full")} aria-label="Trigger" value={trigger} onChange={(e) => setTrigger(e.target.value)}>
            <option value="">Choose what starts it</option>
            {catalog.triggers.map((t) => (
              <option key={t.kind} value={t.kind}>
                {t.words}
              </option>
            ))}
          </select>
        </div>
        <div className="space-y-1.5">
          <span className={label}>If</span>
          {conds.length === 0 && <p className="text-[12px] text-muted">Every time it starts.</p>}
          {conds.map((c, i) => (
            <div key={i} className="flex gap-2">
              <select className={cn(control, "w-1/2")} aria-label={`Condition ${i + 1}`} value={c.kind} onChange={(e) => setConds((cs) => cs.map((x, j) => (j === i ? { ...x, kind: e.target.value } : x)))}>
                <option value="">Choose a condition</option>
                {catalog.conditions.map((t) => (
                  <option key={t.kind} value={t.kind}>
                    {t.words}
                  </option>
                ))}
              </select>
              <input className={cn(control, "flex-1")} aria-label={`Condition ${i + 1} value`} value={c.value} maxLength={120} onChange={(e) => setConds((cs) => cs.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))} placeholder="supplier, customer, PurchaseOrder…" />
              <button type="button" className="text-faint hover:text-fg cursor-pointer" aria-label={`Remove condition ${i + 1}`} onClick={() => setConds((cs) => cs.filter((_, j) => j !== i))}>
                <X className="size-4" />
              </button>
            </div>
          ))}
          <Button size="sm" variant="ghost" className="-ml-2 h-7 text-muted" onClick={() => setConds((cs) => [...cs, { kind: "", value: "" }])}>
            <Plus aria-hidden /> Condition
          </Button>
        </div>
        <fieldset className="space-y-1.5">
          <legend className={label}>Then</legend>
          <div className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
            {catalog.actions.map((a) => (
              <label key={a.kind} className="flex items-center gap-2 text-[13px]">
                <input type="checkbox" checked={actions.includes(a.kind)} onChange={(e) => setActions((xs) => (e.target.checked ? [...xs, a.kind] : xs.filter((x) => x !== a.kind)))} />
                {a.words}
              </label>
            ))}
          </div>
        </fieldset>
        <div className="space-y-1">
          <span className={label}>For</span>
          <select className={cn(control, "w-full sm:w-1/2")} aria-label="For whom" value={person} onChange={(e) => setPerson(e.target.value)}>
            <option value="">Whoever the trigger reaches</option>
            {people.map((p) => (
              <option key={p.id} value={p.id}>
                {personName(area, p.id)}
              </option>
            ))}
          </select>
        </div>
        {error && <ErrorNotice message={error} />}
        <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
          <Button size="sm" disabled={busy || !ready || !catalog.builder || !area.owner} onClick={() => void keep()}>
            {kept ? "Keep changes" : "Keep as draft"}
          </Button>
          <span className="text-[12px] text-muted">{area.owner ? "A kept workflow starts in Draft mode. It goes Auto from its page after its gate passes." : "Only the owner keeps a workflow."}</span>
        </div>
      </div>
      <aside className="space-y-4 lg:border-l lg:border-border lg:pl-6" data-testid="builder-replay">
        <h3 className={label}>Try it on past days</h3>
        <div className="flex items-center gap-2">
          <select className={control} aria-label="Days" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {[7, 14, 30, 60].map((d) => (
              <option key={d} value={d}>
                Last {d} days
              </option>
            ))}
          </select>
          <Button size="sm" variant="secondary" disabled={busy || !kept} onClick={() => void run()}>
            Replay
          </Button>
        </div>
        {!kept && <p className="text-[12px] text-muted">Keep it as a draft first.</p>}
        {replay && (
          <div className="space-y-3">
            <dl className="grid grid-cols-3 gap-3">
              {(
                [
                  ["Caught", replay.caught, "text-ok"],
                  ["Missed", replay.missed, "text-risk"],
                  ["False alarms", replay.false_alarms, "text-late"],
                ] as const
              ).map(([k, v, tone]) => (
                <div key={k}>
                  <dt className="text-[11px] text-muted">{k}</dt>
                  <dd className={cn("num text-[19px]", v > 0 && tone)}>{v}</dd>
                </div>
              ))}
            </dl>
            <p className="text-[12px] text-muted">
              Of {replay.judged} judged over {replay.days} days. Nothing was sent or written.
            </p>
            <ul className="divide-y divide-border text-[12px]">
              {(replay.items ?? []).map((it, i) => (
                <li key={i} className="py-1.5">
                  <span className={cn("mr-2 font-medium", it.outcome === "caught" ? "text-ok" : it.outcome === "missed" ? "text-risk" : "text-late")}>{it.outcome === "false_alarm" ? "False alarm" : it.outcome === "caught" ? "Caught" : "Missed"}</span>
                  <span className="num text-muted">{formatDate(it.at, { short: true })}</span>
                  {it.why && <p className="text-muted">{it.why}</p>}
                </li>
              ))}
            </ul>
          </div>
        )}
        <p className="border-t border-border pt-3 text-[12px] text-muted">Switch live from the workflow&apos;s page once its replay passes the gate.</p>
      </aside>
    </div>
  );
}

/** The workflows that run on a schedule: what each run checks, when it last ran. */
export function SchedulesView({ filterWorkflow }: { filterWorkflow: (workflow: string) => boolean }) {
  const [data, setData] = useState<AutopilotSchedules | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    getAutopilotSchedules(ctl.signal).then(setData, (e) => !ctl.signal.aborted && setError(e));
    return () => ctl.abort();
  }, [round]);
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={() => setRound((n) => n + 1)} />;
  if (!data) return <Pending label="Reading the schedules" />;
  const rows = (data.schedules ?? []).filter((s) => filterWorkflow(s.workflow));
  if (rows.length === 0) return <EmptyState title="No scheduled workflow in this view." line={(data.notes ?? [])[0]} />;
  return (
    <ul className="divide-y divide-border" data-testid="autopilot-schedules">
      {rows.map((s) => (
        <li key={s.id} className="grid grid-cols-[minmax(0,1.4fr)_minmax(0,1.6fr)_8rem_4rem] items-baseline gap-4 px-3 py-2 text-[13px] max-sm:grid-cols-[minmax(0,1fr)_auto] max-sm:gap-x-3 max-sm:gap-y-1">
          <span className="min-w-0 max-sm:row-start-1">
            <span className="block truncate font-medium">{s.label}</span>
            <span className="block truncate text-[12px] text-muted">{workflowLabel(s.workflow)}</span>
          </span>
          <span className="min-w-0 text-[12px] max-sm:col-start-1 max-sm:row-start-2">
            <span className="block">{s.cadence}</span>
            <span className="block truncate text-muted">{s.checks}</span>
          </span>
          <span className="text-[12px] text-muted max-sm:col-start-2 max-sm:row-start-2 max-sm:text-right">{s.last_run ? `ran ${ago(s.last_run)}` : "not run yet"}</span>
          <span className={cn("text-right text-[12px] max-sm:col-start-2 max-sm:row-start-1", s.on ? "text-fg" : "text-faint")}>{s.on ? "On" : "Off"}</span>
        </li>
      ))}
    </ul>
  );
}
