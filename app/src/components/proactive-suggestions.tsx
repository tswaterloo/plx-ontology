"use client";

import { Check, ChevronRight, Copy } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { actionChip, isPutAway } from "@/lib/actions";
import { getActionStates, getSuggestions } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDate, formatMoney, objectHref, plural, readable, safeHref } from "@/lib/format";
import type { Status } from "@/lib/status";
import type { ActionResult, ProactiveSuggestion, SuggestionDraft, SuggestionEta, Suggestions } from "@/lib/types";
import { CardActions } from "./card-actions";
import { SourceChip } from "./source-chip";
import { OpenRing, StatusGlyph } from "./ui/glyphs";
import { LoadProblem, StaleNote } from "./ui/notice";
import { SkeletonRows } from "./ui/skeleton";
import { StatusWord } from "./ui/status-badge";

/** "ETA confirmed?" in at most three words and a status. Unknown answers fail closed. */
export function etaBadge(eta: SuggestionEta | null | undefined): { status: Status; label: string } | null {
  if (!eta) return null;
  switch (eta.answer) {
    case "yes":
      return { status: "ok", label: eta.when ? `ETA ${formatDate(eta.when, { short: true })}` : "ETA confirmed" };
    case "no":
      return { status: "late", label: "ETA not confirmed" };
    case "possibly":
      return { status: "risk", label: "ETA unclear" };
    case "conflicting":
      return { status: "suspect", label: "ETAs disagree" };
    case "hidden":
      return { status: "neutral", label: "ETA hidden" };
    default:
      return { status: "neutral", label: "ETA unknown" };
  }
}

/** Other open findings, counted (no drafted action): "592 batches on quality hold". */
const COLLAPSED: Record<string, [string, string]> = {
  lot_on_hold: ["batch on quality hold", "batches on quality hold"],
  lot_near_expiry: ["lot near expiry", "lots near expiry"],
  quantity_mismatch: ["quantity mismatch", "quantity mismatches"],
  quote_accepted_no_order: ["accepted quote with no order", "accepted quotes with no order"],
  vendor_terms_unapplied: ["vendor term not applied", "vendor terms not applied"],
};

export function collapsedLine(collapsed: Record<string, number> | undefined): string {
  const parts = Object.entries(collapsed ?? {})
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => {
      const [one, many] = COLLAPSED[k] ?? [k.replace(/_/g, " "), k.replace(/_/g, " ")];
      return `${n} ${n === 1 ? one : many}`;
    });
  return parts.length ? `Also open, no action drafted: ${parts.join(", ")}.` : "";
}

/** The draft as text a person pastes into their own mail. */
export function draftText(d: SuggestionDraft): string {
  const to = d.to.address ? `${d.to.name ? `${d.to.name} <${d.to.address}>` : d.to.address}` : d.to.name ?? "";
  return [to ? `To: ${to}` : null, `Subject: ${d.subject}`, "", d.body].filter((l) => l !== null).join("\n");
}

/** The draft as a lifted mail sheet: who it goes to, its subject and body; copied whole, never sent from here. */
function Draft({ draft }: { draft: SuggestionDraft }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(draftText(draft)).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      },
      () => undefined,
    );
  };
  const title = draft.audience === "vendor" ? "Draft to the supplier" : draft.audience === "customer" ? "Draft to the customer" : "Internal note";
  const to = draft.to.address ? (draft.to.name ? `${draft.to.name} <${draft.to.address}>` : draft.to.address) : null;
  return (
    <div data-testid="suggestion-draft">
      <div className="mb-2 flex items-center justify-between gap-2">
        <p className="text-[12px] font-medium text-muted">
          {title} <span className="font-normal">(not sent)</span>
        </p>
        <button type="button" onClick={copy} className="-my-1 inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-[12px] text-muted hover:bg-hover hover:text-fg cursor-pointer" data-testid="copy-draft">
          {copied ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
          {copied ? "Copied" : "Copy draft"}
        </button>
      </div>
      <div className="overflow-hidden rounded-xl bg-bg shadow-lift">
        <div className="flex items-start gap-3 border-b border-border px-4 py-2.5 text-[13px]">
          <span className="w-14 shrink-0 text-muted">To</span>
          <span data-testid="draft-to" className="min-w-0 flex-1 break-words">
            {to ?? (
              <span className="inline-flex flex-wrap items-center gap-x-1.5 text-muted">
                <OpenRing size={12} tone="neutral" />
                {draft.to.name ?? "No address"}
                <span className="basis-full text-[12px]">{draft.to.basis}</span>
              </span>
            )}
          </span>
        </div>
        <div className="flex items-center gap-3 border-b border-border px-4 py-2.5 text-[13px]">
          <span className="w-14 shrink-0 text-muted">Subject</span>
          <span className="min-w-0 truncate font-medium">{draft.subject}</span>
        </div>
        <pre className="max-h-[240px] overflow-y-auto px-4 pt-3.5 pb-4 font-sans text-[13px] leading-[21px] break-words whitespace-pre-wrap text-fg/90">{draft.body}</pre>
      </div>
      {draft.review && <p className="mt-2 text-[12px] text-muted">{draft.review}</p>}
      {(draft.facts ?? []).length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[12px]">
          <span className="text-muted">Figures from</span>
          {(draft.facts ?? []).slice(0, 6).map((f, n) => (
            <SourceChip key={`${f.source.handle}-${n}`} source={f.source} record={readable(f.value)} />
          ))}
        </div>
      )}
    </div>
  );
}

/** A card's headline as the rest of the app says it: "PO 106001 is 134 days late", never "106001 is…" or "PO100095 is…". */
export function suggestionHeadline(s: Pick<ProactiveSuggestion, "headline" | "subject">): string {
  const h = readable(s.headline).replace(/\b(PO|SO)(?=\d{3,}\b)/g, "$1 ");
  return /^PurchaseOrder$/.test(s.subject.type) && /^\d{4,}\b/.test(h) ? `PO ${h}` : h;
}

const sentence = (t: string) => (t ? t.charAt(0).toUpperCase() + t.slice(1) : t);

/** The server's standing note under the follow-ups (operator_api SUGGESTIONS_NOTE), said shorter. */
const SERVER_NOTE = "Nothing is sent until you review a draft and confirm it yourself; it then goes from your own mailbox, or is recorded as a dry run while sending is not enabled.";

export function SuggestionRow({ s, position, state, onDone }: { s: ProactiveSuggestion; position: number; state?: ActionResult | null; onDone?: (r: ActionResult) => void }) {
  const [open, setOpen] = useState(position === 1);
  const eta = etaBadge(s.eta);
  const chip = actionChip(state, !!s.draft);
  const href = safeHref(s.subject.open_url) ?? objectHref(s.subject.type, s.subject.object_id);
  // a reason that only repeats the supplier-date line above it is said once
  const etaWords = s.eta ? readable(s.eta.text).trim() : "";
  const reasons = (s.reasoning ?? []).filter((r) => !etaWords || !readable(r.text).includes(etaWords));
  const amount = s.impact.amount && !s.impact.amount_suspect ? formatMoney(s.impact.amount.amount, s.impact.amount.currency) : null;
  return (
    <li className="py-3.5" data-testid="suggestion">
      <div className="min-w-0 space-y-1.5">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <Link href={href} className="text-[13px] font-semibold hover:underline" data-testid="suggestion-headline">
            {suggestionHeadline(s)}
          </Link>
          {eta && <StatusWord status={eta.status} label={eta.label} data-testid="suggestion-eta" />}
          {s.impact.amount_suspect && <StatusWord status="suspect" label="Total suspect" title={s.impact.amount_suspect} />}
          <span className="ml-auto flex items-baseline gap-3">
            {amount && <span className="text-[12px] tabular-nums text-muted">{amount}</span>}
            {chip && <StatusWord shown={chip} title={chip.title} data-testid="action-state" />}
          </span>
        </div>
        <p className="text-[13px] leading-5">{s.action.text}</p>
        {(s.impact.orders ?? []).length > 0 && (
          <p className="text-[12px] text-muted">
            Holds up{" "}
            {(s.impact.orders ?? []).map((o, n) => (
              <span key={o.object_id}>
                {n > 0 && ", "}
                <Link href={safeHref(o.open_url) ?? objectHref(o.type, o.object_id)} className="text-fg hover:underline">
                  {o.label}
                </Link>
              </span>
            ))}
          </p>
        )}
        <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} className="-ml-0.5 inline-flex h-6 items-center gap-1 text-[12px] text-muted hover:text-fg cursor-pointer" data-testid="suggestion-toggle">
          <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} aria-hidden />
          {onDone && (s.actions ?? []).length > 0 ? (s.draft ? "Draft, actions and evidence" : "Actions and evidence") : s.draft ? "Draft and evidence" : "Evidence"}
        </button>
        {open && (
          <div className="space-y-4 pt-1" data-testid="suggestion-detail">
            {onDone && <CardActions card={s} onDone={onDone} state={state} />}
            {s.eta && (
              <p className="text-[13px] leading-5">
                <span className="text-muted">Supplier date </span>
                {readable(s.eta.text)}
                {s.eta.source && (
                  <span className="ml-1.5 align-middle">
                    <SourceChip source={s.eta.source} />
                  </span>
                )}
              </p>
            )}
            {reasons.length > 0 && (
              <ul className="space-y-1 text-[13px] leading-5" data-testid="suggestion-reasoning">
                {reasons.map((r, n) => (
                  <li key={n}>
                    {readable(r.text).replace(/^ETA confirmed\? (?:yes|no|possibly|unknown)\.\s*/i, "")}
                    {r.source && (
                      <span className="ml-1.5 align-middle">
                        <SourceChip source={r.source} />
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {s.confidence.level !== "high" && (
              <p className="flex gap-2 text-[12px] leading-[18px]">
                <StatusGlyph status={s.confidence.level === "medium" ? "risk" : "neutral"} size={12} className="mt-[3px]" />
                <span>
                  <span className="font-medium">{s.confidence.level === "medium" ? "Check before acting." : "Weak evidence."}</span> <span className="text-muted">{sentence(readable(s.confidence.why))}</span>
                </span>
              </p>
            )}
            {(s.missing ?? []).length > 0 && (
              <div data-testid="suggestion-missing">
                <p className="mb-1.5 text-[12px] font-medium text-muted">Not on record</p>
                <ul className="space-y-1.5">
                  {(s.missing ?? []).map((m, n) => (
                    <li key={n} className="flex gap-2.5 text-[13px] leading-5">
                      <OpenRing size={13} tone="neutral" className="mt-[3px]" />
                      {m}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {s.draft && <Draft draft={s.draft} />}
          </div>
        )}
      </div>
    </li>
  );
}

/** "Follow-ups" on Today: the ranked suggestions, loaded apart from the briefing. */
export function ProactiveSuggestions({ limit = 5, className }: { limit?: number; className?: string }) {
  const [data, setData] = useState<Suggestions | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  const [states, setStates] = useState<Record<string, ActionResult>>({});
  const [putAway, setPutAway] = useState(false);
  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    getSuggestions(c.signal)
      .then(setData)
      .catch((e) => {
        if (!c.signal.aborted) setError(plainError(e));
      });
    // a card's chip is only a hint: without states every card still reads "Drafted"
    getActionStates(c.signal).then((r) => setStates(r.cards ?? {}), () => undefined);
    return () => c.abort();
  }, []);
  useEffect(() => load(), [load]);
  const done = useCallback((r: ActionResult) => setStates((prev) => ({ ...prev, [r.card_id]: r })), []);
  if (data && data.status !== "read") return null;
  const every = data?.items ?? [];
  const away = every.filter((s) => isPutAway(states[s.id]));
  const items = putAway ? every : every.filter((s) => !isPutAway(states[s.id]));
  const shown = all ? items : items.slice(0, limit);
  const extra = collapsedLine(data?.collapsed);
  return (
    <section className={className} data-testid="suggestions">
      <div className="mb-1 flex items-baseline justify-between gap-3">
        <h2 className="text-[12px] font-medium text-muted">Follow-ups</h2>
        {data && data.total > 0 && <span className="text-[12px] tabular-nums text-faint">{data.total}</span>}
      </div>
      <StaleNote since={data?.stale_since} className="mb-1 text-[12px]" />
      {error ? (
        <LoadProblem message={error} onRetry={load} />
      ) : !data ? (
        <SkeletonRows rows={3} label="Loading follow-ups" />
      ) : every.length === 0 ? (
        <p className="py-2 text-[13px] text-muted">No follow-ups.</p>
      ) : (
        <>
          <ol className="divide-y divide-border">
            {shown.map((s, n) => (
              <SuggestionRow key={s.id} s={s} position={n + 1} state={states[s.id]} onDone={done} />
            ))}
          </ol>
          <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px]">
            {items.length > shown.length && (
              <button type="button" onClick={() => setAll(true)} className="font-medium text-muted hover:text-fg cursor-pointer">
                Show {items.length - shown.length} more
              </button>
            )}
            {away.length > 0 && (
              <button type="button" onClick={() => setPutAway(!putAway)} className="text-muted hover:text-fg cursor-pointer" data-testid="put-away-toggle">
                {putAway ? "Hide" : "Show"} {away.length} done, snoozed or dismissed
              </button>
            )}
            {data.more > 0 && (
              <Link href="/briefing" className="text-muted hover:text-fg">
                {plural(data.more, "more record")} in the briefing
              </Link>
            )}
          </div>
        </>
      )}
      {data && (
        <div className="mt-3 space-y-1 text-[12px] text-muted">
          {extra && <p data-testid="suggestions-collapsed">{extra}</p>}
          <p>{data.note === SERVER_NOTE ? "Nothing is sent until you confirm it." : data.note}</p>
        </div>
      )}
    </section>
  );
}
