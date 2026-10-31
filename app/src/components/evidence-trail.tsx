"use client";

import { ArrowUpRight, CircleAlert, CircleCheck, CircleHelp, EyeOff, Lightbulb, Route } from "lucide-react";
import Link from "next/link";
import { cn } from "@/lib/cn";
import { formatDate, formatDateTime, objectHref, safeHref, SYSTEM_LABEL } from "@/lib/format";
import type { EtaCheck, EvidenceTrail, MaterialRow, NextAction, PoTrail, Tone, TrailStep } from "@/lib/types";
import { Badge } from "./ui/badge";

/** What each step kind is, in words, and its dot colour. */
const KIND: Record<TrailStep["kind"], { label: string; dot: string }> = {
  due_date: { label: "Requested-by date", dot: "bg-slate-400" },
  po_issued: { label: "Purchase order", dot: "bg-sky-500" },
  confirmation: { label: "Vendor confirmation", dot: "bg-emerald-500" },
  revision: { label: "Promise revised", dot: "bg-amber-500" },
  lead: { label: "Possible confirmation", dot: "bg-amber-300" },
  follow_up: { label: "Follow-up", dot: "bg-violet-500" },
  shipped: { label: "Shipped", dot: "bg-sky-400" },
  receipt: { label: "Receipt", dot: "bg-emerald-600" },
  verdict: { label: "Verdict", dot: "bg-accent" },
  claim: { label: "Checked fact", dot: "bg-accent" },
  // the why trail's (operating/why_trail.py)
  decision: { label: "Decision", dot: "bg-violet-600" },
  communication: { label: "Message", dot: "bg-sky-500" },
  record_change: { label: "Record", dot: "bg-slate-500" },
  document: { label: "Document", dot: "bg-emerald-500" },
};

const ETA: Record<EtaCheck["state"], { label: string; tone: Tone }> = {
  confirmed: { label: "ETA confirmed by the vendor", tone: "good" },
  promises_disagree: { label: "Vendor dates disagree", tone: "warn" },
  lead_only: { label: "ETA: lead only", tone: "warn" },
  no_confirmation: { label: "No vendor confirmation", tone: "bad" },
  no_confirmation_found: { label: "No vendor confirmation found", tone: "warn" },
  not_determined: { label: "ETA not determined", tone: "neutral" },
  withheld: { label: "Confirmation not visible to you", tone: "neutral" },
  not_applicable: { label: "No ETA needed", tone: "neutral" },
};

const CHECK: Record<string, string> = {
  read_from_record: "Read from the record",
  lead: "Machine-read reference (a lead)",
  computed_from_cited_records: "Computed from the cited records",
  text_match: "Names the record by text (a match, not a link)",
  not_established: "Not established from records you can see",
  unchecked: "Not checked against its source",
};

/** Checks that say a step is not confirmed: shown with a question mark, never a tick. */
const UNCONFIRMED = new Set(["lead", "text_match", "not_established", "unchecked"]);

function stateTone(state: string | null | undefined): Tone {
  if (state === "late") return "bad";
  if (state === "on_time") return "good";
  if (state === "not_due") return "info";
  return "neutral";
}

function when(at: string | null | undefined): string | null {
  if (!at) return null;
  return at.length > 10 ? formatDateTime(at) : formatDate(at);
}

function Step({ step }: { step: TrailStep }) {
  const kind = KIND[step.kind] ?? { label: "Step", dot: "bg-muted" };
  const href = safeHref(step.source?.open_url);
  const sys = step.source ? (SYSTEM_LABEL[step.source.system] ?? step.source.system) : null;
  const body = (
    <>
      <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted">
        <span className="font-medium">{kind.label}</span>
        {when(step.at) && (
          <>
            <span aria-hidden>·</span>
            <time dateTime={step.at ?? undefined}>{when(step.at)}</time>
          </>
        )}
        {sys && (
          <>
            <span aria-hidden>·</span>
            <span>{sys}</span>
          </>
        )}
      </span>
      <span className="mt-0.5 block text-sm font-medium text-fg">{step.title}</span>
      {(step.detail || step.sender) && (
        <span className="block text-sm text-muted">{[step.sender ? `From ${step.sender}` : null, step.detail].filter(Boolean).join(" · ")}</span>
      )}
      <span className="mt-0.5 flex items-center gap-1.5 text-xs text-muted">
        {UNCONFIRMED.has(step.check) ? <CircleHelp className="size-3" aria-hidden /> : <CircleCheck className="size-3 text-good" aria-hidden />}
        {CHECK[step.check] ?? "Checked against its source"}
        {href && step.source && (
          <span className="inline-flex items-center gap-0.5 text-accent">
            · Open {step.source.label}
            <ArrowUpRight className="size-3" aria-hidden />
          </span>
        )}
      </span>
    </>
  );
  return (
    <li className="relative" data-testid="trail-step" data-kind={step.kind}>
      <span className={cn("absolute -left-[25.5px] top-1.5 size-2.5 rounded-full border-2 border-surface", kind.dot)} aria-hidden />
      {href ? (
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="-mx-2 block rounded-lg px-2 py-1 hover:bg-surface-2 focus-visible:bg-surface-2"
          aria-label={`${kind.label}: ${step.title}. Open ${step.source?.label ?? "source"}`}
        >
          {body}
        </a>
      ) : (
        <div className="-mx-2 px-2 py-1">{body}</div>
      )}
    </li>
  );
}

export function EtaBadge({ eta }: { eta: EtaCheck }) {
  const shown = ETA[eta.state] ?? { label: "ETA", tone: "neutral" as Tone };
  const href = safeHref(eta.source?.open_url);
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="eta-check">
      <Badge tone={shown.tone}>{shown.label}</Badge>
      <span className="text-muted">{eta.text}</span>
      {href && (
        <a href={href} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-0.5 text-xs font-medium text-accent hover:underline">
          Open the confirmation <ArrowUpRight className="size-3" aria-hidden />
        </a>
      )}
    </div>
  );
}

export function NextActionCard({ action }: { action: NextAction }) {
  const href = safeHref(action.contact_source?.open_url);
  return (
    <section className="flex gap-3 rounded-xl border border-accent/30 bg-accent-soft/50 p-3 text-sm" aria-label="Suggested next step" data-testid="next-action">
      <Lightbulb className="mt-0.5 size-4 shrink-0 text-accent" aria-hidden />
      <div className="min-w-0 space-y-1">
        <p className="font-medium text-fg">Suggested next step</p>
        <p className="text-fg">{action.text}</p>
        {href && (
          <a href={href} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-0.5 text-xs font-medium text-accent hover:underline">
            Open the last message from the vendor <ArrowUpRight className="size-3" aria-hidden />
          </a>
        )}
        <p className="text-xs text-muted">A suggestion only: nothing has been sent or changed.</p>
      </div>
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
    <p className="flex items-center gap-1.5 text-xs text-muted" data-testid="trail-hidden">
      <EyeOff className="size-3.5" aria-hidden />
      {text}
    </p>
  );
}

export function PoTrailView({ trail }: { trail: PoTrail }) {
  const href = safeHref(trail.subject.open_url) ?? objectHref(trail.subject.type, trail.subject.object_id);
  return (
    <div className="space-y-3" data-testid="po-trail">
      <div className="flex flex-wrap items-center gap-2">
        <Link href={href} className="text-sm font-semibold hover:underline">
          {trail.subject.label}
        </Link>
        {trail.state && <Badge tone={stateTone(trail.state)}>{trail.state === "late" && trail.days_late != null ? `${trail.days_late} days late` : trail.state.replace(/_/g, " ")}</Badge>}
      </div>
      {trail.eta && <EtaBadge eta={trail.eta} />}
      {(trail.steps ?? []).length > 0 && (
        <ol className="relative ml-1.5 space-y-3 border-l border-border pl-5" aria-label={`How we know: ${trail.subject.label}`}>
          {(trail.steps ?? []).map((s, i) => (
            <Step key={i} step={s} />
          ))}
        </ol>
      )}
      <Hidden text={hiddenText(trail.hidden_steps ?? 0, !!trail.may_hide_more)} />
      {(trail.not_found ?? []).map((n, i) => (
        <p key={i} className="flex gap-1.5 text-xs text-muted">
          <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-warn" aria-hidden />
          <span>
            {n.what}: {n.where_it_would_live}
          </span>
        </p>
      ))}
      {trail.next_action && <NextActionCard action={trail.next_action} />}
    </div>
  );
}

function Materials({ rows }: { rows: MaterialRow[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="-mx-1 overflow-x-auto">
      <table className="w-full text-sm" data-testid="materials">
        <caption className="sr-only">Purchase orders of the order's materials, late first</caption>
        <thead>
          <tr className="border-b border-border text-left text-xs text-muted">
            <th scope="col" className="px-1 py-2 font-medium">Material</th>
            <th scope="col" className="py-2 font-medium">Assembly</th>
            <th scope="col" className="py-2 font-medium">PO</th>
            <th scope="col" className="px-1 py-2 text-right font-medium">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-border align-top last:border-0">
              <td className="px-1 py-2">{r.item.label}</td>
              <td className="py-2 text-muted">{r.assembly.label}</td>
              <td className="py-2">
                <Link href={safeHref(r.po.open_url) ?? objectHref(r.po.type, r.po.object_id)} className="font-medium hover:underline">
                  {r.po.label}
                </Link>
              </td>
              <td className="px-1 py-2 text-right" title={r.verdict ?? undefined}>
                <Badge tone={stateTone(r.state)}>{r.state === "late" && r.days_late != null ? `${r.days_late} d late` : (r.state ?? "lateness not read").replace(/_/g, " ")}</Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** "How we know": the steps, records and messages behind an answer, each opening its source. */
export function EvidenceTrailCard({ trail }: { trail: EvidenceTrail | null | undefined }) {
  if (!trail) return null;
  const trails = trail.trails ?? [];
  const steps = trail.steps ?? [];
  const rows = trail.materials ?? [];
  const gaps = trail.gaps ?? [];
  if (trails.length === 0 && steps.length === 0 && rows.length === 0 && gaps.length === 0) return null;
  return (
    <section className="space-y-4 rounded-xl border border-border bg-surface p-4" aria-label="How we know" data-testid="evidence-trail">
      <h3 className="flex items-center gap-2 text-sm font-semibold">
        <Route className="size-4 text-accent" aria-hidden /> {trail.title}
      </h3>
      <Materials rows={rows} />
      {gaps.length > 0 && (
        <ul className="space-y-1 text-xs text-muted" data-testid="trail-gaps">
          {gaps.map((g, i) => (
            <li key={i} className="flex gap-1.5">
              <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-warn" aria-hidden />
              <span>
                <span className="text-fg">{g.what}</span>
                {g.state === "note"
                  ? `: ${g.where_it_would_live}`
                  : `: ${g.state === "absent_in_source" ? "not recorded" : "not found on record"} (would be in ${g.where_it_would_live})`}
              </span>
            </li>
          ))}
        </ul>
      )}
      {trails.map((t, i) => (
        <PoTrailView key={i} trail={t} />
      ))}
      {steps.length > 0 && (
        <ol className="relative ml-1.5 space-y-3 border-l border-border pl-5">
          {steps.map((s, i) => (
            <Step key={i} step={s} />
          ))}
        </ol>
      )}
      {/* one purchase-order trail shows its own caveat; a materials walk or several trails
          state the answer's own (the walk's hidden steps are not in any one PO's count) */}
      {trail.kind === "why" ? (
        <>
          {trail.complete === false && (
            <p className="flex items-center gap-1.5 text-xs text-muted" data-testid="trail-incomplete">
              <CircleAlert className="size-3.5 text-warn" aria-hidden />
              Only the latest steps are shown; the record has more.
            </p>
          )}
          <Hidden text={hiddenText(trail.hidden_steps ?? 0, !!trail.may_hide_more)} />
          {trail.next_action && <NextActionCard action={trail.next_action} />}
        </>
      ) : (
        (trail.kind !== "purchase_order" || trails.length !== 1) && (
          <Hidden text={trail.hidden_message ?? hiddenText(trail.hidden_steps ?? 0, false)} />
        )
      )}
    </section>
  );
}
