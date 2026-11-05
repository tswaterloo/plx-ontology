"use client";

import { Check, ChevronDown, ChevronRight, Copy } from "lucide-react";
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
import { Card, CardBody, CardHeader, CardTitle } from "./ui/card";
import { LoadProblem } from "./ui/notice";
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

const CONFIDENCE_TEXT: Record<string, string> = { high: "text-ok", medium: "text-risk", low: "text-muted" };

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
  const title = draft.audience === "vendor" ? "Draft to the vendor" : draft.audience === "customer" ? "Draft to the customer" : "Internal note";
  return (
    <div className="rounded-lg border border-border bg-surface p-4" data-testid="suggestion-draft">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <p className="text-label font-medium">
          {title} <span className="font-normal text-muted">· not sent; nothing goes until you review and confirm it</span>
        </p>
        <button type="button" onClick={copy} className="inline-flex items-center gap-1 rounded border border-control px-2 py-1 text-label hover:bg-surface-2 cursor-pointer" data-testid="copy-draft">
          {copied ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
          {copied ? "Copied" : "Copy draft"}
        </button>
      </div>
      <dl className="mb-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-meta">
        <dt className="text-muted">To</dt>
        <dd data-testid="draft-to">
          {draft.to.address ? (draft.to.name ? `${draft.to.name} <${draft.to.address}>` : draft.to.address) : draft.to.name ?? "—"}
          {!draft.to.address && <span className="block text-muted">{draft.to.basis}</span>}
        </dd>
        <dt className="text-muted">Subject</dt>
        <dd>{draft.subject}</dd>
      </dl>
      <pre className="whitespace-pre-wrap break-words border-t border-border pt-3 font-sans text-row">{draft.body}</pre>
      {draft.review && <p className="mt-2 text-meta text-muted">{draft.review}</p>}
      {(draft.facts ?? []).length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="text-meta text-muted">Every fact in it is cited:</span>
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

export function SuggestionRow({ s, position, state, onDone }: { s: ProactiveSuggestion; position: number; state?: ActionResult | null; onDone?: (r: ActionResult) => void }) {
  const [open, setOpen] = useState(position === 1);
  const eta = etaBadge(s.eta);
  const chip = actionChip(state, !!s.draft);
  const href = safeHref(s.subject.open_url) ?? objectHref(s.subject.type, s.subject.object_id);
  const amount = s.impact.amount && !s.impact.amount_suspect ? formatMoney(s.impact.amount.amount, s.impact.amount.currency) : null;
  return (
    <li className="py-3" data-testid="suggestion">
      <div className="flex items-start gap-3">
        <span className="w-4 shrink-0 pt-px text-right font-serif text-[15px] leading-5 tabular-nums text-faint">{position}</span>
        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <Link href={href} className="text-row font-medium hover:underline" data-testid="suggestion-headline">
              {suggestionHeadline(s)}
            </Link>
            {eta && <StatusWord status={eta.status} label={eta.label} data-testid="suggestion-eta" />}
            {s.impact.amount_suspect && <StatusWord status="suspect" label="Total suspect" title={s.impact.amount_suspect} />}
            <span className="ml-auto flex items-baseline gap-3">
              {amount && <span className="text-meta tabular-nums text-muted">{amount}</span>}
              {chip && <StatusWord shown={chip} title={chip.title} data-testid="action-state" />}
            </span>
          </div>
          <p className="text-row text-muted">
            <span className="text-faint">Next: </span>
            {s.action.text}
          </p>
          {(s.impact.orders ?? []).length > 0 && (
            <p className="text-meta text-muted">
              Holds up{" "}
              {(s.impact.orders ?? []).map((o, n) => (
                <span key={o.object_id}>
                  {n > 0 && ", "}
                  <Link href={safeHref(o.open_url) ?? objectHref(o.type, o.object_id)} className="text-accent hover:underline">
                    {o.label}
                  </Link>
                </span>
              ))}
            </p>
          )}
          <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} className="inline-flex items-center gap-1 text-meta text-muted hover:text-fg cursor-pointer" data-testid="suggestion-toggle">
            {open ? <ChevronDown className="size-3.5" aria-hidden /> : <ChevronRight className="size-3.5" aria-hidden />}
            {onDone && (s.actions ?? []).length > 0 ? `Act on it${s.draft ? ", see the draft" : ""} and how we know` : `How we know${s.draft ? " and the draft" : ""}`}
          </button>
          {open && (
            <div className="space-y-3 border-l-2 border-border-strong pt-1 pl-4" data-testid="suggestion-detail">
              {onDone && <CardActions card={s} onDone={onDone} state={state} />}
              {s.eta && (
                <p className="text-row">
                  <span className="font-medium">ETA confirmed? </span>
                  {readable(s.eta.text)}
                  {s.eta.source && (
                    <span className="ml-1.5 align-middle">
                      <SourceChip source={s.eta.source} />
                    </span>
                  )}
                </p>
              )}
              {(s.reasoning ?? []).length > 0 && (
                <ol className="list-decimal space-y-1 pl-5 text-row" data-testid="suggestion-reasoning">
                  {(s.reasoning ?? []).map((r, n) => (
                    <li key={n}>
                      {readable(r.text)}
                      {r.source && (
                        <span className="ml-1.5 align-middle">
                          <SourceChip source={r.source} />
                        </span>
                      )}
                    </li>
                  ))}
                </ol>
              )}
              <p className="text-meta text-muted">
                <span className={cn("font-medium", CONFIDENCE_TEXT[s.confidence.level] ?? "text-muted")}>{sentence(s.confidence.level)} confidence.</span> {sentence(readable(s.confidence.why))}
              </p>
              {(s.missing ?? []).length > 0 && (
                <div className="text-meta" data-testid="suggestion-missing">
                  <p className="font-medium">What's missing</p>
                  <ul className="list-disc pl-5 text-muted">
                    {(s.missing ?? []).map((m, n) => (
                      <li key={n}>{m}</li>
                    ))}
                  </ul>
                </div>
              )}
              {s.draft && <Draft draft={s.draft} />}
            </div>
          )}
        </div>
      </div>
    </li>
  );
}

/** "What to do next" on Today: the ranked suggestions, loaded apart from the briefing. */
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
    <Card className={cn("mt-6", className)} data-testid="suggestions">
      <CardHeader>
        <CardTitle>What to do next</CardTitle>
        {data && data.total > 0 && <span className="text-meta text-muted">{plural(data.total, "item")}, by impact</span>}
      </CardHeader>
      <CardBody>
        {error ? (
          <LoadProblem message={error} onRetry={load} />
        ) : !data ? (
          <SkeletonRows rows={3} label="Working out what to do next" />
        ) : every.length === 0 ? (
          <p className="text-row text-muted">Nothing needs a follow-up from you right now.</p>
        ) : (
          <>
            <ol className="divide-y divide-border border-t border-border">
              {shown.map((s, n) => (
                <SuggestionRow key={s.id} s={s} position={n + 1} state={states[s.id]} onDone={done} />
              ))}
            </ol>
            {items.length > shown.length && (
              <button type="button" onClick={() => setAll(true)} className="mt-1 text-row font-medium text-accent hover:underline cursor-pointer">
                Show {items.length - shown.length} more
              </button>
            )}
            {away.length > 0 && (
              <button type="button" onClick={() => setPutAway(!putAway)} className="mt-1 block text-meta text-muted hover:text-fg hover:underline cursor-pointer" data-testid="put-away-toggle">
                {putAway ? "Hide" : "Show"} {plural(away.length, "card")} you put away (done, snoozed or dismissed)
              </button>
            )}
            {data.more > 0 && <p className="mt-1 text-meta text-muted">{plural(data.more, "more record")} need attention; see the briefing.</p>}
          </>
        )}
        {data && (
          <div className="mt-2 space-y-1 text-meta text-muted">
            {extra && <p data-testid="suggestions-collapsed">{extra}</p>}
            <p>{data.note}</p>
          </div>
        )}
      </CardBody>
    </Card>
  );
}
