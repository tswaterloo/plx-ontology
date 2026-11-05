"use client";

import Link from "next/link";
import { useState } from "react";
import { cn } from "@/lib/cn";
import { formatDate, objectHref, readable, recordLabel, safeHref, SYSTEM_LABEL } from "@/lib/format";
import { etaShown, promiseLate } from "@/lib/eta";
import { lateness, STATUS_CLASS, type Status } from "@/lib/status";
import type { EtaCheck, EvidenceTrail, MaterialRow, NextAction, PoTrail, Source, TrailStep } from "@/lib/types";
import { EtaPill } from "./eta-confirmed";
import { EvidenceDrawer } from "./source-chip";
import { StatusBadge, StatusWord } from "./ui/status-badge";

/** What each step kind is, in words, and its dot's status colour. */
const KIND: Record<TrailStep["kind"], { label: string; status: Status }> = {
  due_date: { label: "Needed-by date", status: "neutral" },
  po_issued: { label: "Purchase order", status: "neutral" },
  confirmation: { label: "Vendor confirmation", status: "ok" },
  revision: { label: "Promise revised", status: "risk" },
  lead: { label: "Possible confirmation", status: "risk" },
  follow_up: { label: "Follow-up", status: "new" },
  shipped: { label: "Shipped", status: "new" },
  receipt: { label: "Receipt", status: "ok" },
  verdict: { label: "Verdict", status: "neutral" },
  claim: { label: "Checked fact", status: "neutral" },
  // the why trail's (operating/why_trail.py)
  decision: { label: "Decision", status: "new" },
  communication: { label: "Message", status: "new" },
  record_change: { label: "Record", status: "neutral" },
  document: { label: "Document", status: "ok" },
};

/** A step's check, said only when it is not a confirmation. */
const UNCONFIRMED_WORDS: Record<string, string> = {
  lead: "Mentions this record; not a confirmation",
  text_match: "Names the record in its text; not a confirmation",
  not_established: "Not established from records you can see",
  unchecked: "Not checked against its source",
};

/** The rule behind a lateness verdict, in plain words (shown behind "Rule"). */
export const LATE_RULE = "Late means still open after the needed-by date, or received after it. A supplier's promise doesn't move that date.";

/** Server step wording, said plainly. */
function stepWords(text: string | null | undefined): string {
  return readable(text)
    .replace(/^a line's expected receipt date: the due date only when no header records a requested-by date\.?$/i, "Needed-by date on the order line")
    .replace(/requested-by date/gi, "needed-by date")
    .replace(/the verdict below is read now/i, "the conclusion above is read now");
}

/** Lateness in words, kept for callers that print a short state. */
export function stateWords(state: string, daysLate?: number | null, short = false): string {
  const s = lateness(state, daysLate);
  if (!s) return state.replace(/_/g, " ");
  return short && state === "late" && daysLate != null ? `${daysLate} d late` : s.label;
}

function StepSource({ source, children, label }: { source: Source | null | undefined; children: React.ReactNode; label: string }) {
  const [open, setOpen] = useState(false);
  const opens = !!source && (!!source.handle || !!safeHref(source.open_url));
  if (!opens || !source) return <div className="-mx-2 px-2 py-1">{children}</div>;
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="group -mx-2 flex w-[calc(100%+1rem)] items-start gap-2 rounded-sm px-2 py-1 text-left hover:bg-surface-2 cursor-pointer"
        aria-label={label}
      >
        <span className="min-w-0 flex-1">{children}</span>
        <span className="shrink-0 text-meta text-accent group-hover:underline" aria-hidden>Source</span>
      </button>
      {open && <EvidenceDrawer source={source} open={open} onOpenChange={setOpen} />}
    </>
  );
}

function Step({ step, group }: { step: TrailStep; group?: TrailStep[] }) {
  const [open, setOpen] = useState(false);
  const kind = KIND[step.kind] ?? { label: "Step", status: "neutral" as Status };
  const sys = step.source ? (SYSTEM_LABEL[step.source.system] ?? step.source.system) : null;
  const unconfirmed = UNCONFIRMED_WORDS[step.check];
  const many = group && group.length > 1;
  const dates = many ? group.map((g) => g.at).filter(Boolean) as string[] : [];
  const span =
    many && dates.length
      ? `${formatDate(dates[dates.length - 1], { short: true })}${dates.length > 1 && dates[0] !== dates[dates.length - 1] ? ` – ${formatDate(dates[0], { short: true })}` : ""}`
      : step.at
        ? formatDate(step.at, { short: true })
        : null;
  // a thread's row is named by its first message, without "RE:"/"FW:"
  const title = many
    ? `${group.length} messages: ${stepWords(group[group.length - 1].title).replace(/^Email(?: from [^:]+)?:\s*/i, "").replace(/^(?:(?:RE|FW|FWD|AW)\s*:\s*)+/i, "")}`
    : stepWords(step.title);
  const body = (
    <>
      <span className="flex flex-wrap items-center gap-x-1.5 text-meta text-muted">
        <span>{kind.label}</span>
        {span && (
          <>
            <span aria-hidden className="-ml-1.5">,</span>
            <time dateTime={step.at ?? undefined}>{span}</time>
          </>
        )}
        {sys && (
          <>
            <span aria-hidden className="-ml-1.5">,</span>
            <span>{sys}</span>
          </>
        )}
      </span>
      <span className="block break-words text-row font-medium text-fg">{title}</span>
      {(step.detail || step.sender) && !many && (
        <span className="block break-words text-row text-muted">
          {stepWords([step.sender ? `From ${step.sender.replace(/\s*<[^>]+>/, "")}` : null, step.detail].filter(Boolean).join(" · "))}
        </span>
      )}
      {unconfirmed && <span className="mt-0.5 block text-meta text-risk">{unconfirmed}</span>}
    </>
  );
  return (
    <li className="relative" data-testid="trail-step" data-kind={step.kind}>
      <span className="absolute -left-[23.5px] top-[8px] size-[7px] rounded-full border border-control bg-bg" aria-hidden />
      <StepSource source={step.source} label={`${kind.label}: ${title}. Open the evidence`}>
        {body}
      </StepSource>
      {many && (
        <>
          <button type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open} className="mt-0.5 text-meta text-accent hover:underline cursor-pointer">
            {open ? "Hide them" : `Show all ${group.length}`}
          </button>
          {open && (
            <ol className="mt-1 space-y-1 border-l border-border pl-3">
              {group.map((g, i) => (
                <li key={i} data-testid="trail-step-grouped">
                  <StepSource source={g.source} label={`${stepWords(g.title)}. Open the evidence`}>
                    <span className="block text-meta text-muted">{g.at ? formatDate(g.at, { short: true }) : ""}</span>
                    <span className="block text-row">{stepWords(g.title)}</span>
                  </StepSource>
                </li>
              ))}
            </ol>
          )}
        </>
      )}
    </li>
  );
}

/** A message's subject without "RE:"/"FW:" and its reminder number, for grouping repeats. */
function threadKey(s: TrailStep): string {
  return stepWords(s.title)
    .toLowerCase()
    .replace(/^email(?: from [^:]+)?:\s*/, "")
    .replace(/^(?:(?:re|fw|fwd|aw)\s*:\s*)+/g, "")
    .replace(/\s*-\s*(?:first|second|third|final)?\s*payment reminder.*$/, "")
    .replace(/\s+as of .*$/, "")
    .trim();
}

const MESSAGE_KINDS = new Set(["follow_up", "communication", "lead"]);

/**
 * The steps as a reader takes them: newest first; a "Goods received" step dropped when a
 * partial-receipt step tells the same receipt (the pair reads as "received in full"), the
 * partial one dated; repeated messages of one thread collapsed into one row.
 */
export function shapeSteps(steps: TrailStep[]): { step: TrailStep; group?: TrailStep[] }[] {
  let list = steps.filter((s) => s.kind !== "verdict");
  const partial = list.find((s) => s.kind === "receipt" && /^partial receipt/i.test(s.title));
  if (partial) {
    const full = list.find((s) => s.kind === "receipt" && s !== partial && /^goods received$/i.test(s.title.trim()));
    if (full) {
      const merged: TrailStep = {
        ...partial,
        at: partial.at ?? full.at,
        title: `Receipt${partial.at ?? full.at ? ` on ${formatDate(partial.at ?? full.at, { short: true })}` : ""}: ${partial.title.replace(/^partial receipt:\s*/i, "")}`,
      };
      list = list.filter((s) => s !== full).map((s) => (s === partial ? merged : s));
    }
  }
  const dated = [...list].sort((a, b) => (b.at ?? "").localeCompare(a.at ?? ""));
  const out: { step: TrailStep; group?: TrailStep[] }[] = [];
  for (const s of dated) {
    const prev = out[out.length - 1];
    if (prev && MESSAGE_KINDS.has(s.kind) && prev.step.kind === s.kind && threadKey(prev.step) === threadKey(s)) {
      prev.group = [...(prev.group ?? [prev.step]), s];
      continue;
    }
    out.push({ step: s });
  }
  return out;
}

/** The verdict first, its rule (when it has one) behind "Rule ⓘ". */
function Verdict({ text, rule, check, status = "neutral", beside = [] }: { text: string; rule?: string | null; check?: string; status?: Status; beside?: TrailStep[] }) {
  const [open, setOpen] = useState(false);
  const words = rule ? (/^rule: an open purchase order|^late means/i.test(rule) ? LATE_RULE : stepWords(rule.replace(/^Rule:\s*/, ""))) : null;
  return (
    <div data-testid="trail-verdict">
      <p className="flex flex-wrap items-baseline gap-x-2 text-row font-semibold">
        <span className={status !== "neutral" ? STATUS_CLASS[status].fg : undefined}>{stepWords(text)}</span>
        {words && (
          <button type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open} className="text-meta font-normal text-accent hover:underline cursor-pointer">
            Rule
          </button>
        )}
      </p>
      {check && UNCONFIRMED_WORDS[check] && <p className="mt-0.5 text-meta text-risk">{UNCONFIRMED_WORDS[check]}</p>}
      {open && words && <p className="mt-1 text-meta text-muted">{words}</p>}
      {beside.map((b, i) => (
        <div key={i} className="mt-2 border-t border-border pt-2" data-testid="trail-verdict-beside">
          <StepSource source={b.source} label={`${stepWords(b.title)} Open the evidence`}>
            <span className="block text-row text-fg">{stepWords(b.title)}</span>
            {b.detail && <span className="block text-meta text-muted">{stepWords(b.detail)}</span>}
          </StepSource>
        </div>
      ))}
    </div>
  );
}

function Steps({ steps, label }: { steps: TrailStep[]; label: string }) {
  const [all, setAll] = useState(false);
  const shaped = shapeSteps(steps);
  if (shaped.length === 0) return null;
  const shown = all ? shaped : shaped.slice(0, 4);
  return (
    <div>
      <ol className="relative ml-1 space-y-2 border-l border-border pl-5" aria-label={label}>
        {shown.map((s, i) => (
          <Step key={i} step={s.step} group={s.group} />
        ))}
      </ol>
      {shaped.length > 4 && (
        <button type="button" onClick={() => setAll((v) => !v)} aria-expanded={all} className="mt-2 text-meta font-medium text-accent hover:underline cursor-pointer" data-testid="trail-show-all">
          {all ? "Show fewer steps" : `Show all ${shaped.length} steps`}
        </button>
      )}
    </div>
  );
}

export function EtaBadge({ eta }: { eta: EtaCheck }) {
  const shown = etaShown(eta);
  const [open, setOpen] = useState(false);
  if (!shown) return null;
  const src = eta.promise?.source ?? eta.source;
  const late = eta.promise ? promiseLate(eta.promise, eta.promise.link) : null;
  return (
    <div className="flex flex-wrap items-center gap-2 text-row" data-testid="eta-check">
      <EtaPill shown={shown} />
      <EtaPill shown={late} />
      {!eta.promise && <span className="text-muted">{stepWords(eta.text)}</span>}
      {src && (src.handle || safeHref(src.open_url)) && (
        <>
          <button type="button" onClick={() => setOpen(true)} className="text-meta text-accent hover:underline cursor-pointer">
            Open confirmation
          </button>
          {open && <EvidenceDrawer source={src} open={open} onOpenChange={setOpen} />}
        </>
      )}
    </div>
  );
}

/**
 * The suggested next step, in the "Who to chase" card style: who, what to ask, the last
 * message from them one click away, and that nothing is sent.
 */
export function NextActionCard({ action, title = "Who to chase" }: { action: NextAction; title?: string }) {
  const [open, setOpen] = useState(false);
  const src = action.contact_source;
  return (
    <section className="border-t border-border pt-3 text-row" aria-label={title} data-testid="next-action">
      <p className="text-meta text-muted">{title}</p>
      {action.contact ? (
        <p className="mt-1 font-medium">{action.contact.replace(/\s*<[^>]+>/, "")}</p>
      ) : null}
      <p className="mt-1 text-fg">{stepWords(action.text)}</p>
      {src && (src.handle || safeHref(src.open_url)) && (
        <>
          <button type="button" onClick={() => setOpen(true)} className="mt-1 text-meta text-accent hover:underline cursor-pointer">
            Open their last message
          </button>
          {open && <EvidenceDrawer source={src} open={open} onOpenChange={setOpen} />}
        </>
      )}
      <p className="mt-1 text-meta text-muted">Nothing is sent or changed from here.</p>
    </section>
  );
}

/** Hidden steps, composed from the count and the "may be more" flag only (never a name). */
export function hiddenText(count: number, mayBeMore: boolean): string | null {
  const n = Number.isFinite(count) ? Math.max(0, Math.trunc(count)) : 0;
  if (n > 0) return `${n} more step${n === 1 ? "" : "s"} in places you can't open.${mayBeMore ? " There may be more." : ""}`;
  return mayBeMore ? "Some steps may be in places you can't open." : null;
}

function Hidden({ text }: { text: string | null }) {
  if (!text) return null;
  return (
    <p className="text-meta text-muted" data-testid="trail-hidden">
      {text}
    </p>
  );
}

/** A gap line's words as compared with the verdict's: its reason after any "x:" lead-in. */
const reasonKey = (t: string) => readable(t).replace(/^[^:]*:\s*/, "").replace(/[.\s]+$/, "").toLowerCase();

export function PoTrailView({ trail, showSubject = true, showNext = true, showEta = true }: { trail: PoTrail; showSubject?: boolean; showNext?: boolean; showEta?: boolean }) {
  const href = safeHref(trail.subject.open_url) ?? objectHref(trail.subject.type, trail.subject.object_id);
  // the live verdict's own step; any other verdict step (the late list's check when it
  // last ran, which the server adds when it differs) is shown beside it, never dropped
  const verdicts = (trail.steps ?? []).filter((s) => s.kind === "verdict");
  const verdictStep = verdicts.find((s) => s.title === trail.verdict) ?? (trail.verdict ? undefined : verdicts[verdicts.length - 1]);
  const verdict = trail.verdict ?? verdictStep?.title;
  const beside = verdicts.filter((s) => s !== verdictStep && s.title !== verdict);
  const late = lateness(trail.state, trail.days_late);
  return (
    <div className="space-y-3" data-testid="po-trail">
      {showSubject && (
        <div className="flex flex-wrap items-center gap-2">
          <Link href={href} className="text-row font-semibold hover:underline">
            {recordLabel(trail.subject.type, trail.subject.label)}
          </Link>
          {late && trail.state !== "not_applicable" && <StatusBadge shown={late} />}
        </div>
      )}
      {verdict && <Verdict text={verdict} rule={verdictStep?.detail ?? (trail.state ? LATE_RULE : null)} check={verdictStep?.check} status={late?.status} beside={beside} />}
      {showEta && trail.eta && <EtaBadge eta={trail.eta} />}
      <Steps steps={trail.steps ?? []} label={`Evidence: ${trail.subject.label}`} />
      <Hidden text={hiddenText(trail.hidden_steps ?? 0, !!trail.may_hide_more)} />
      {(trail.not_found ?? []).filter((n) => !verdict || !reasonKey(verdict).includes(reasonKey(n.where_it_would_live)) || !reasonKey(n.where_it_would_live)).map((n, i) => (
        <p key={i} className="text-meta text-muted">
          <span className="text-fg">{readable(n.what)}:</span> {readable(n.where_it_would_live)}
        </p>
      ))}
      {showNext && trail.next_action && <NextActionCard action={trail.next_action} />}
    </div>
  );
}

function Materials({ rows }: { rows: MaterialRow[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-row" data-testid="materials">
        <caption className="sr-only">Purchase orders of the order's materials, late first</caption>
        <thead>
          <tr className="h-8 border-y border-border-strong bg-bg-subtle text-left text-meta text-muted">
            <th scope="col" className="px-2 font-medium">Material</th>
            <th scope="col" className="font-medium">Assembly order</th>
            <th scope="col" className="font-medium">PO</th>
            <th scope="col" className="px-2 text-right font-medium">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-border align-top last:border-0">
              <td className="px-2 py-2">{r.item.label}</td>
              <td className="py-2 text-muted">{r.assembly.label}</td>
              <td className="py-2">
                <Link href={safeHref(r.po.open_url) ?? objectHref(r.po.type, r.po.object_id)} className="font-medium hover:text-accent hover:underline">
                  {recordLabel(r.po.type, r.po.label)}
                </Link>
              </td>
              <td className="px-2 py-2 text-right" title={r.verdict ?? undefined}>
                {r.state ? <StatusWord shown={{ ...lateness(r.state, r.days_late)!, label: stateWords(r.state, r.days_late, true) }} /> : <StatusWord status="neutral" label="Not checked" />}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The trail's suggested next step (one PO trail's, or a why trail's), if any. */
export function trailNextAction(trail: EvidenceTrail | null | undefined): NextAction | null {
  if (!trail) return null;
  if (trail.next_action) return trail.next_action;
  const t = trail.trails ?? [];
  return t.length === 1 ? (t[0].next_action ?? null) : null;
}

/** How many steps the trail shows (after merging), for "How we know (N steps)". */
export function trailStepCount(trail: EvidenceTrail | null | undefined): number {
  if (!trail) return 0;
  const n = (trail.trails ?? []).reduce((acc, t) => acc + shapeSteps(t.steps ?? []).length + (t.verdict || (t.steps ?? []).some((s) => s.kind === "verdict") ? 1 : 0), 0);
  const why = shapeSteps(trail.steps ?? []).length + ((trail.steps ?? []).some((s) => s.kind === "verdict") ? 1 : 0);
  return n + why + (trail.materials ?? []).length + (trail.gaps ?? []).length;
}

/**
 * "How we know": the verdict, then the steps behind it newest first (four, then "Show all"),
 * each opening its evidence in the drawer. `bare`: no card frame or title (inside an
 * answer's disclosure or a record page section). `inPage`: the page header names the record.
 */
export function EvidenceTrailCard({
  trail,
  bare = false,
  inPage = false,
  showNext = true,
  showEta = true,
}: {
  trail: EvidenceTrail | null | undefined;
  bare?: boolean;
  inPage?: boolean;
  showNext?: boolean;
  /** false where the page shows the ETA check in its own card */
  showEta?: boolean;
}) {
  if (!trail) return null;
  const trails = trail.trails ?? [];
  const steps = trail.steps ?? [];
  const rows = trail.materials ?? [];
  const gaps = trail.gaps ?? [];
  if (trails.length === 0 && steps.length === 0 && rows.length === 0 && gaps.length === 0) return null;
  const whyVerdict = steps.find((s) => s.kind === "verdict");
  return (
    <section
      className={cn("space-y-4", !bare && "border-t border-rule pb-2")}
      aria-label="Evidence"
      data-testid="evidence-trail"
    >
      {!bare && <h2 className="-mb-1 flex min-h-10 items-center text-section">Evidence</h2>}
      {/^how we know/i.test(trail.title) ? <p className="sr-only">{trail.title}</p> : <p className="text-meta text-muted">{readable(trail.title)}</p>}
      <Materials rows={rows} />
      {gaps.length > 0 && (
        <ul className="space-y-1 text-meta text-muted" data-testid="trail-gaps">
          {gaps.map((g, i) => (
            <li key={i}>
              <span>
                <span className="text-fg">{readable(g.what)}</span>
                {g.state === "note"
                  ? `: ${readable(g.where_it_would_live)}`
                  : `: ${g.state === "absent_in_source" ? "not recorded" : "not found on record"} (would be in ${readable(g.where_it_would_live)})`}
              </span>
            </li>
          ))}
        </ul>
      )}
      {trails.map((t, i) => (
        <PoTrailView key={i} trail={t} showSubject={!(inPage && trails.length === 1)} showNext={showNext} showEta={showEta} />
      ))}
      {whyVerdict && <Verdict text={whyVerdict.title} rule={whyVerdict.detail} check={whyVerdict.check} />}
      {steps.length > 0 && <Steps steps={steps} label={trail.title} />}
      {/* one purchase-order trail shows its own caveat; a materials walk or several trails
          state the answer's own (the walk's hidden steps are not in any one PO's count) */}
      {trail.kind === "why" ? (
        <>
          {trail.complete === false && (
            <p className="text-meta text-muted" data-testid="trail-incomplete">
              Latest steps only; the record has more.
            </p>
          )}
          <Hidden text={hiddenText(trail.hidden_steps ?? 0, !!trail.may_hide_more)} />
          {showNext && trail.next_action && <NextActionCard action={trail.next_action} title="Next step" />}
        </>
      ) : (
        (trail.kind !== "purchase_order" || trails.length !== 1) && (
          <Hidden text={trail.hidden_message ?? hiddenText(trail.hidden_steps ?? 0, false)} />
        )
      )}
    </section>
  );
}
