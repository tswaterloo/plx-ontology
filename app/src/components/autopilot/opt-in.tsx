"use client";

import { useState } from "react";
import { useAuth } from "@/components/auth";
import { Button } from "@/components/ui/button";
import { ErrorNotice } from "@/components/ui/notice";
import { setAutopilotOptIn, setAutopilotPaused, setMyWorkMode, setSwitchLimits, type SwitchLimits } from "@/lib/api";
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

type Mode = "off" | "confirm" | "auto";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** The limits on a switch's Auto in a line: "only for x; Mon to Fri; 8:00 to 17:00; at 95% or better". */
export function limitsLine(l: SwitchLimits | null | undefined): string {
  if (!l) return "";
  const parts: string[] = [];
  if (l.senders.length) parts.push(`only for ${l.senders.join(", ")}`);
  if (l.days.length) parts.push(l.days.map((d) => DAYS[d]).join(", "));
  if (l.start && l.end) parts.push(`${l.start} to ${l.end} ET`);
  if (l.min_percent != null) parts.push(`only work that matched you ${l.min_percent}% or more`);
  return parts.join("; ");
}

/** Limits on one switch's Auto: senders, days, hours (US Eastern) and a stricter bar. */
function Limits({ c, onArea }: { c: AutopilotDraftChoice; onArea: (a: Area) => void }) {
  const cur = (c.conditions ?? null) as SwitchLimits | null;
  const [open, setOpen] = useState(false);
  const [senders, setSenders] = useState((cur?.senders ?? []).join(", "));
  const [days, setDays] = useState<number[]>(cur?.days ?? []);
  const [start, setStart] = useState(cur?.start ?? "");
  const [end, setEnd] = useState(cur?.end ?? "");
  const [bar, setBar] = useState(cur?.min_percent != null ? String(cur.min_percent) : "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    const pct = bar.trim() ? Number(bar.trim()) : null;
    if (pct !== null && !Number.isFinite(pct)) {
      setError("The bar is a percentage, for example 95.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const limits: SwitchLimits = { senders: senders.split(/[,\s]+/).map((x) => x.trim()).filter(Boolean), days, start: start || null, end: end || null, min_percent: pct };
      onArea(await setSwitchLimits(c.choice, limits));
      setOpen(false);
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const line = limitsLine(cur);
  return (
    <div className="space-y-1 pt-1 text-[12px]" data-testid="auto-limits">
      <p className="text-muted">
        {line ? `Auto: ${line}.` : "Auto: no limits of your own."}{" "}
        <button type="button" className="text-accent underline-offset-2 hover:underline cursor-pointer" onClick={() => setOpen((v) => !v)}>
          {open ? "Close" : "Change limits"}
        </button>
      </p>
      {open && (
        <div className="max-w-[40rem] space-y-2 border-l-2 border-border pl-3">
          <label className="block text-muted">
            Only for these senders (addresses or @domain, comma separated; empty: anyone)
            <input value={senders} onChange={(e) => setSenders(e.target.value)} className="mt-1 h-8 w-full border border-border bg-bg px-2 text-[13px] text-fg" />
          </label>
          <fieldset className="flex flex-wrap items-center gap-3 text-muted">
            <legend className="pb-1">Only on these days (none: every day)</legend>
            {DAYS.map((d, n) => (
              <label key={d} className="flex items-center gap-1">
                <input type="checkbox" checked={days.includes(n)} onChange={(e) => setDays((xs) => (e.target.checked ? [...xs, n].sort() : xs.filter((x) => x !== n)))} />
                {d}
              </label>
            ))}
          </fieldset>
          <div className="flex flex-wrap items-end gap-3 text-muted">
            <label>
              From (ET)
              <input type="time" aria-label="From" value={start} onChange={(e) => setStart(e.target.value)} className="mt-1 block h-8 border border-border bg-bg px-2 text-[13px] text-fg" />
            </label>
            <label>
              To (ET)
              <input type="time" aria-label="To" value={end} onChange={(e) => setEnd(e.target.value)} className="mt-1 block h-8 border border-border bg-bg px-2 text-[13px] text-fg" />
            </label>
            <label>
              Only work that matched me at least (%)
              <input inputMode="decimal" aria-label="Your bar" value={bar} onChange={(e) => setBar(e.target.value)} placeholder={c.score ? String(c.score.need_percent) : "90"} className="mt-1 block h-8 w-24 border border-border bg-bg px-2 text-[13px] text-fg" />
            </label>
          </div>
          <Button size="sm" disabled={busy} onClick={() => void save()}>
            Save limits
          </Button>
          {error && <ErrorNotice message={error} />}
        </div>
      )}
    </div>
  );
}

function DraftRow({ c, busy, onSet, onArea, owner = false }: { c: AutopilotDraftChoice; busy: boolean; onSet: (mode: Mode) => void; onArea: (a: Area) => void; owner?: boolean }) {
  const on = c.partly ? "Partly on" : "On";
  const auto = c.mode === "auto";
  // the owner reads their own choices: "set by the owner" is said only to the people they set it for
  const who = c.by_owner && !owner ? ", set by the owner" : c.since && !c.partly ? ` since ${formatDateTime(c.since)}` : "";
  // what the state means, in a sentence: prepared and waiting, or sent by itself
  const state = !c.on ? "Off. Nothing is prepared for you." : `${on}${who}. ${auto ? "Goes out by itself, within your limits." : "Prepared for you; waits for your confirm."}`;
  return (
    <li className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2 py-3" data-testid="draft-choice">
      <div className="min-w-0 flex-1 space-y-1">
        <p className="text-[13.5px] font-medium text-fg">{c.label}</p>
        <p className={cn("text-[12px]", c.on ? "text-ok" : "text-muted")} data-testid="draft-choice-state">
          {state}
        </p>
        {c.score && (
          <p className="num text-[12px] text-muted" data-testid="draft-choice-score">
            {optInScore({ eligible: !!c.auto_ok, score: c.score })}
          </p>
        )}
        {c.on && (c.auto_ok || c.conditions) && <Limits c={c} onArea={onArea} />}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {c.on && (
          <div
            className="inline-flex items-center gap-0.5 rounded-lg border border-border bg-surface p-0.5 text-[12px]"
            role="radiogroup"
            aria-label={`${c.label}: how it goes out`}
            data-testid="draft-choice-mode"
          >
            <button
              type="button"
              role="radio"
              aria-checked={!auto}
              disabled={busy}
              onClick={() => onSet("confirm")}
              className={cn(
                "inline-flex h-6 items-center rounded-md px-2.5 transition-colors cursor-pointer",
                !auto ? "bg-bg font-medium text-fg shadow-sm" : "text-muted hover:text-fg",
              )}
            >
              Confirm
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={auto}
              disabled={busy || (!c.auto_ok && !auto)}
              title={c.auto_ok ? undefined : "Auto opens once past days replayed score high enough for you."}
              onClick={() => onSet("auto")}
              className={cn(
                "inline-flex h-6 items-center rounded-md px-2.5 transition-colors",
                auto ? "bg-bg font-medium text-fg shadow-sm cursor-pointer" : "text-muted hover:text-fg",
                !c.auto_ok && !auto ? "opacity-45 cursor-not-allowed" : "cursor-pointer",
              )}
            >
              Auto
            </button>
          </div>
        )}
        <Button size="sm" variant="outline" role="switch" aria-checked={c.on} aria-label={`${c.label}: ${c.on ? "on" : "off"}`} disabled={busy} onClick={() => onSet(c.on ? "off" : "confirm")}>
          {c.on ? "Turn off" : "Turn on"}
        </Button>
      </div>
    </li>
  );
}

// What the autopilot prepares for you: a draft or a prepared action waiting in My work.
// Nothing needs a score to be prepared, because nothing goes out until you confirm it.
export function DraftsPanel({ area, onArea }: { area: Area; onArea: (a: Area) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { state: auth } = useAuth();
  const owner = auth.status === "signed-in" && auth.me.owner === true;
  const choices = area.my_drafts ?? [];
  if (choices.length === 0) return null;
  const set = async (choice: string, mode: Mode) => {
    setBusy(true);
    setError(null);
    try {
      onArea(await setMyWorkMode(choice, mode));
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
        Turned on, replies and changes are prepared for you and wait in Assist until you confirm them. Auto lets them go without your click, and only opens where replaying past days matched what you did often enough; money and anything unclear still wait for you.{owner ? "" : " You can turn off what the owner turned on for you."}
      </p>
      {error && <ErrorNotice message={error} />}
      <ul className="divide-y divide-border" aria-label="What to prepare for me">
        {choices.map((c) => (
          <DraftRow key={c.choice} c={c} busy={busy} owner={owner} onSet={(mode) => void set(c.choice, mode)} onArea={onArea} />
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
