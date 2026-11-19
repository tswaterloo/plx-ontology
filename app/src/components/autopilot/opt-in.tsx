"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { ErrorNotice } from "@/components/ui/notice";
import { setAutopilotDrafts, setAutopilotOptIn, setAutopilotPaused } from "@/lib/api";
import { optInScore, personName, suspendedWords, workflowLabel } from "@/lib/autopilot";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import type { AutopilotArea as Area, AutopilotDraftChoice, AutopilotOptIn } from "@/lib/types";

// Your own switches: each workflow that passed its check for you, off until you turn it on,
// and one pause for all of them. Nobody else can turn yours on, the owner included; the
// owner and admins see who has what on (read only).

function Row({ o, busy, onSet }: { o: AutopilotOptIn; busy: boolean; onSet: (on: boolean) => void }) {
  const can = o.on || o.eligible;
  return (
    <li className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2 py-2.5" data-testid="opt-in-row">
      <div className="min-w-0 flex-1 space-y-0.5">
        <p className="text-[13px] font-medium">{workflowLabel(o.workflow)}</p>
        <p className="num text-[12px] text-muted">{optInScore(o)}</p>
        {o.on && o.since && <p className="text-[12px] text-ok">On since {formatDateTime(o.since)}.</p>}
        {!o.on && o.suspended && (
          <p className="text-[12px] text-risk" data-testid="opt-in-suspended">
            {suspendedWords(o.suspended.reason, o.suspended.score ?? o.score, o.suspended.at ? formatDateTime(o.suspended.at) : "")}
          </p>
        )}
      </div>
      <Button
        size="sm"
        variant="outline"
        role="switch"
        aria-checked={o.on}
        aria-label={`${workflowLabel(o.workflow)}: ${o.on ? "on" : "off"}`}
        disabled={busy || !can}
        title={can ? undefined : "It can be turned on once it passes its check for you."}
        onClick={() => onSet(!o.on)}
      >
        {o.on ? "Turn off" : "Turn on"}
      </Button>
    </li>
  );
}

function DraftRow({ c, busy, onSet }: { c: AutopilotDraftChoice; busy: boolean; onSet: (on: boolean) => void }) {
  const on = c.partly ? "Partly on" : "On";
  const state = !c.on ? "Off" : c.by_owner ? `${on}, set by the owner` : c.since && !c.partly ? `On since ${formatDateTime(c.since)}` : on;
  return (
    <li className="flex items-center justify-between gap-6 py-2" data-testid="draft-choice">
      <div className="min-w-0 space-y-0.5">
        <p className="text-[13px]">{c.label}</p>
        <p className={cn("text-[12px]", c.on ? "text-ok" : "text-muted")} data-testid="draft-choice-state">
          {state}
        </p>
      </div>
      <Button size="sm" variant="outline" role="switch" aria-checked={c.on} aria-label={`${c.label}: ${c.on ? "on" : "off"}`} disabled={busy} onClick={() => onSet(!c.on)}>
        {c.on ? "Turn off" : "Turn on"}
      </Button>
    </li>
  );
}

// What the autopilot prepares for you: a draft or a prepared action waiting in My work.
// Nothing needs a score to be prepared, because nothing goes out until you confirm it.
export function DraftsPanel({ area, onArea }: { area: Area; onArea: (a: Area) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const choices = area.my_drafts ?? [];
  if (choices.length === 0) return null;
  const set = async (choice: string, on: boolean) => {
    setBusy(true);
    setError(null);
    try {
      onArea(await setAutopilotDrafts(choice, on));
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="space-y-2 border-b border-border pb-4" aria-labelledby="ap-prepare" data-testid="autopilot-drafts">
      <h2 id="ap-prepare" className="text-[13px] font-semibold">
        What to prepare for me
      </h2>
      <p className="max-w-[46rem] text-[12px] text-muted">
        Switched on, a reply or an action is drafted for you and waits on Home. Nothing is sent or changed until you confirm it there, and you can edit it first. What the owner turned on for you, you can turn off; your choice wins.
      </p>
      {error && <ErrorNotice message={error} />}
      <ul className="divide-y divide-border" aria-label="What to prepare for me">
        {choices.map((c) => (
          <DraftRow key={c.choice} c={c} busy={busy} onSet={(on) => void set(c.choice, on)} />
        ))}
      </ul>
    </section>
  );
}

export function OptInPanel({ area, onArea }: { area: Area; onArea: (a: Area) => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mine = area.my_opt_ins ?? [];
  const who = area.switched_on;
  const drafting = area.drafts_switched_on;
  if (mine.length === 0 && !area.paused && who == null && drafting == null) return null;
  const run = async (key: string, call: () => Promise<Area>) => {
    setBusy(key);
    setError(null);
    try {
      onArea(await call());
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(null);
    }
  };
  const anyOn = mine.some((o) => o.on);
  return (
    <section className="space-y-3 border-b border-border pb-4" aria-labelledby="ap-yours" data-testid="autopilot-opt-in">
      {(mine.length > 0 || area.paused) && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h2 id="ap-yours" className="text-[13px] font-semibold">
              Your autopilot
            </h2>
            {(anyOn || area.paused) && (
              <Button size="sm" variant="outline" role="switch" aria-checked={!!area.paused} disabled={busy !== null} onClick={() => void run("pause", () => setAutopilotPaused(!area.paused))}>
                {area.paused ? "Resume" : "Pause everything"}
              </Button>
            )}
          </div>
          <p className="max-w-[46rem] text-[12px] text-muted">
            Each one below has done what you would have done on enough of your past items. It does nothing for you until you turn it on, and then only routine items: never money, prices, credit, permissions or a new customer or supplier. You can turn it off at any time. If its score drops under the bar, it turns itself off and tells you why.
          </p>
          {area.paused && <p className="text-[12px] text-risk">Paused. Nothing runs for you until you resume.</p>}
          {!area.switches.master_switch && anyOn && (
            <p className="text-[12px] text-muted">Live actions are off on this server for now, so what you turn on only records what it would have done.</p>
          )}
          {area.switches.global_off && anyOn && <p className="text-[12px] text-muted">The owner has stopped all autopilot writes for now.</p>}
          {error && <ErrorNotice message={error} />}
          <ul className="divide-y divide-border" aria-label="Your workflows">
            {mine.map((o) => (
              <Row key={o.workflow} o={o} busy={busy !== null} onSet={(on) => void run(o.workflow, () => setAutopilotOptIn(o.workflow, on))} />
            ))}
          </ul>
        </div>
      )}
      {(who != null || drafting != null) && (
        <div className="space-y-1" data-testid="autopilot-who-is-on">
          <h2 className="text-[13px] font-semibold">Who has it on</h2>
          <p className="text-[12px] text-muted">Each person turns their own on or off. You can see it here but can&apos;t change it for them.</p>
          {(who ?? []).length === 0 && (drafting ?? []).length === 0 ? (
            <p className="text-[12px] text-muted">No one has switched on drafts or actions yet.</p>
          ) : (
            <ul className="text-[13px]">
              {(drafting ?? []).map((d) => (
                <li key={`${d.person}/drafts/${d.choice}`} className="flex flex-wrap gap-x-3 py-0.5">
                  <span>{personName(area, d.person)}</span>
                  <span>{d.label}</span>
                  <span className="text-[12px] text-muted">{`${d.partly ? "some drafts" : "drafts"}${d.by_owner ? ", set by the owner" : ""}`}</span>
                </li>
              ))}
              {(who ?? []).map((w) => (
                <li key={`${w.person}/${w.workflow}`} className={cn("flex flex-wrap gap-x-3 py-0.5", w.paused && "text-muted")}>
                  <span>{personName(area, w.person)}</span>
                  <span>{workflowLabel(w.workflow)}</span>
                  <span className="text-[12px] text-muted">{w.since ? `since ${formatDateTime(w.since)}` : ""}{w.paused ? " · paused" : ""}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
