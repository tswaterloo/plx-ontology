"use client";

import { Check, ChevronDown, ChevronRight, Copy } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { actionChip, isPutAway } from "@/lib/actions";
import { getActionStates, getSuggestions } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDate, formatMoney, objectHref, plural, safeHref } from "@/lib/format";
import type { Status } from "@/lib/status";
import type { ActionResult, ProactiveSuggestion, SuggestionDraft, SuggestionEta, Suggestions } from "@/lib/types";
import { CardActions } from "./card-actions";
import { SourceChip } from "./source-chip";
import { Card, CardBody, CardHeader, CardTitle } from "./ui/card";
import { LoadProblem } from "./ui/notice";
import { SkeletonRows } from "./ui/skeleton";
import { StatusBadge } from "./ui/status-badge";

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

const CONFIDENCE: Record<string, Status> = { high: "ok", medium: "risk", low: "neutral" };

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
    <div className="rounded-lg border border-border bg-surface-2 p-3" data-testid="suggestion-draft">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <p className="text-label font-medium">
          {title} <span className="font-normal text-muted">· not sent; nothing goes until you review and confirm it</span>
        </p>
        <button type="button" onClick={copy} className="inline-flex items-center gap-1 rounded-md border border-border bg-surface px-2 py-1 text-label hover:border-control cursor-pointer" data-testid="copy-draft">
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
      <pre className="whitespace-pre-wrap break-words font-sans text-row">{draft.body}</pre>
      {draft.review && <p className="mt-2 text-meta text-muted">{draft.review}</p>}
      {(draft.facts ?? []).length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="text-meta text-muted">Every fact in it is cited:</span>
          {(draft.facts ?? []).slice(0, 6).map((f, n) => (
            <SourceChip key={`${f.source.handle}-${n}`} source={f.source} record={f.value} />
          ))}
        </div>
      )}
    </div>
  );
}

export function SuggestionRow({ s, position, state, onDone }: { s: ProactiveSuggestion; position: number; state?: ActionResult | null; onDone?: (r: ActionResult) => void }) {
  const [open, setOpen] = useState(position === 1);
  const eta = etaBadge(s.eta);
  const chip = actionChip(state, !!s.draft);
  const href = safeHref(s.subject.open_url) ?? objectHref(s.subject.type, s.subject.object_id);
  const amount = s.impact.amount && !s.impact.amount_suspect ? formatMoney(s.impact.amount.amount, s.impact.amount.currency) : null;
  return (
    <li className="py-3" data-testid="suggestion">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 w-5 shrink-0 text-right tabular-nums text-meta text-muted">{position}</span>
        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <Link href={href} className="font-medium hover:underline" data-testid="suggestion-headline">
              {s.headline}
            </Link>
            {amount && <span className="tabular-nums text-meta text-muted">{amount}</span>}
            {s.impact.amount_suspect && <StatusBadge status="suspect" label="Total suspect" title={s.impact.amount_suspect} />}
            {eta && <StatusBadge status={eta.status} label={eta.label} dot data-testid="suggestion-eta" />}
            <StatusBadge status={CONFIDENCE[s.confidence.level] ?? "neutral"} label={`${s.confidence.level} confidence`} title={s.confidence.why} />
            {chip && <StatusBadge shown={chip} title={chip.title} data-testid="action-state" />}
          </div>
          <p className="text-row">
            <span className="text-muted">Next: </span>
            {s.action.text}
          </p>
          {onDone && <CardActions card={s} onDone={onDone} />}
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
          <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} className="inline-flex items-center gap-1 text-meta font-medium text-accent hover:underline cursor-pointer" data-testid="suggestion-toggle">
            {open ? <ChevronDown className="size-3.5" aria-hidden /> : <ChevronRight className="size-3.5" aria-hidden />}
            How we know{s.draft ? " and the draft" : ""}
          </button>
          {open && (
            <div className="space-y-3 pt-1" data-testid="suggestion-detail">
              {s.eta && (
                <p className="text-row">
                  <span className="font-medium">ETA confirmed? </span>
                  {s.eta.text}
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
                      {r.text}
                      {r.source && (
                        <span className="ml-1.5 align-middle">
                          <SourceChip source={r.source} />
                        </span>
                      )}
                    </li>
                  ))}
                </ol>
              )}
              <p className="text-meta text-muted">Confidence {s.confidence.level}: {s.confidence.why}</p>
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
            <ol className="divide-y divide-border">
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
