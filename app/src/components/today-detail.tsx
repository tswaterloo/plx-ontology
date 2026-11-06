"use client";

import * as Menu from "@radix-ui/react-dropdown-menu";
import { ArrowUpRight, Bookmark, Check, Copy, MessageCircle, MoreHorizontal } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { getObject, getObjectSection, mergeSection } from "@/lib/api";
import { cn } from "@/lib/cn";
import { draftText } from "@/components/proactive-suggestions";
import { formatDate, objectHref, readable, recordLabel, typeLabel } from "@/lib/format";
import { factMap, receivedOf, recordHead } from "@/lib/record";
import { toggleSaved, useSaved } from "@/lib/saved";
import { briefingKind } from "@/lib/status";
import type { ActionResult, BriefingItem, CardActionOffer, LatePo, ObjectPage, ProactiveSuggestion, Source, SuggestionDraft, TrailStep } from "@/lib/types";
import { ActionDialog } from "./card-actions";
import { SourceChip } from "./source-chip";
import { AgeRing, DoneRing, OpenRing, SystemTile } from "./ui/glyphs";
import { Monogram } from "./ui/monogram";

export interface QueueRow {
  key: string;
  group: string;
  type: string;
  objectId: string;
  title: string;
  counterpart: string | null;
  line: string;
  days: number | null;
  status: "late" | "risk" | "neutral" | "suspect" | "ok";
  href: string;
  late?: LatePo;
  item?: BriefingItem;
  suggestion?: ProactiveSuggestion;
}

const DAY = 86_400_000;

function SectionTitle({ children, aside }: { children: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <div className="mb-3 flex items-baseline justify-between gap-3">
      <h3 className="text-[12px] font-medium leading-4 text-muted">{children}</h3>
      {aside}
    </div>
  );
}

/** A day as a time: an ISO date, or BC's "Mar 28, 2026". */
function dayOf(v: string | null | undefined): number | null {
  if (!v) return null;
  const d = /^\d{4}-\d{2}-\d{2}/.test(v) ? new Date(`${v.slice(0, 10)}T12:00:00Z`) : new Date(`${v} 12:00 UTC`);
  return Number.isNaN(d.getTime()) ? null : d.getTime();
}

const short = (t: number) => formatDate(new Date(t).toISOString().slice(0, 10), { short: true });

/**
 * The order's life on one line: ordered, needed, today. The days past the needed-by date are
 * hatched; with no needed-by date the line after the order is left open.
 */
function OrderLine({ ordered, needed, daysLate }: { ordered: number | null; needed: number | null; daysLate?: number | null }) {
  const now = Date.now();
  if (ordered == null && needed == null) return null;
  const start = ordered ?? needed!;
  const end = Math.max(now, needed ?? now);
  const span = Math.max(1, end - start);
  const at = (t: number) => ((t - start) / span) * 100;
  const late = needed != null && now > needed;
  // the server's count where it gave one, so the line and the list agree
  const lateDays = late ? (daysLate ?? Math.round((now - needed!) / DAY)) : 0;
  const neededAt = needed != null ? at(needed) : null;
  return (
    <div className="pt-1" data-testid="order-line">
      <div className="relative h-[18px]">
        {late && neededAt != null && (
          <span className="absolute top-0 -translate-x-1/2 text-[12px] leading-4 font-medium whitespace-nowrap" style={{ left: `${Math.min(88, Math.max(neededAt + (100 - neededAt) / 2, 12))}%` }}>
            {lateDays} days late
          </span>
        )}
      </div>
      <div className="relative mt-1 h-[6px] rounded-full bg-[color-mix(in_srgb,var(--fg)_10%,transparent)]">
        {neededAt != null ? (
          <>
            <span className="absolute inset-y-0 left-0 rounded-l-full bg-[color-mix(in_srgb,var(--fg)_22%,transparent)]" style={{ width: `${neededAt}%` }} />
            {late && (
              <span
                className="absolute inset-y-0 right-0 rounded-r-full"
                style={{ left: `${neededAt}%`, background: "repeating-linear-gradient(135deg, var(--dot-late) 0 2px, color-mix(in srgb, var(--dot-late) 28%, transparent) 2px 5px)" }}
              />
            )}
          </>
        ) : (
          <span className="absolute inset-0 rounded-full" style={{ background: "repeating-linear-gradient(90deg, color-mix(in srgb, var(--fg) 22%, transparent) 0 4px, transparent 4px 8px)" }} />
        )}
        {[ordered != null ? at(ordered) : null, neededAt, 100].map((p, i) =>
          p == null ? null : (
            <span key={i} className={cn("absolute top-1/2 size-[10px] -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-bg", i === 2 ? (late ? "bg-dot-late" : "bg-fg") : "bg-[color-mix(in_srgb,var(--fg)_55%,var(--bg))]")} style={{ left: `${p}%` }} />
          ),
        )}
      </div>
      <div className="relative mt-2 h-8 text-[12px] leading-4">
        {ordered != null && (
          <span className="absolute left-0 top-0">
            <span className="block text-muted">Ordered</span>
            <span className="block font-medium">{short(ordered)}</span>
          </span>
        )}
        {needed != null && neededAt != null && (
          <span className="absolute top-0 -translate-x-1/2 text-center" style={{ left: `${Math.min(70, Math.max(neededAt, 30))}%` }}>
            <span className="block text-muted">Needed by</span>
            <span className="block font-medium">{short(needed)}</span>
          </span>
        )}
        {needed == null && (
          <span className="absolute top-0 left-1/2 -translate-x-1/2 text-center">
            <span className="block text-muted">Needed by</span>
            <span className="block font-medium">Not on record</span>
          </span>
        )}
        <span className="absolute right-0 top-0 text-right">
          <span className="block text-muted">Today</span>
          <span className="block font-medium">{short(now)}</span>
        </span>
      </div>
    </div>
  );
}

function Figure({ label, value, unit, children }: { label: string; value: string; unit?: string; children?: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-[12px] leading-4 text-muted">{label}</div>
      <div className="mt-1 flex items-baseline gap-1.5 whitespace-nowrap">
        <span className="text-[22px] leading-7 font-semibold tracking-[-0.02em] tabular-nums">{value}</span>
        {unit && <span className="text-[13px] text-muted">{unit}</span>}
      </div>
      {children}
    </div>
  );
}

function DraftSheet({ draft, who }: { draft: SuggestionDraft; who: string | null }) {
  const [copied, setCopied] = useState(false);
  const to = draft.to.address ? (draft.to.name ? `${draft.to.name} <${draft.to.address}>` : draft.to.address) : null;
  return (
    <div className="overflow-hidden rounded-xl bg-bg shadow-lift" data-testid="suggestion-draft">
      <div className="flex items-center gap-3 border-b border-border px-4 py-2.5 text-[13px]">
        <span className="w-14 shrink-0 text-muted">To</span>
        <span data-testid="draft-to" className="min-w-0 flex-1 truncate">
          {to ?? (
            <span className="inline-flex items-center gap-1.5 text-muted" title={`No address on record for ${who ?? "the supplier"}`}>
              <OpenRing size={12} />
              No address on record
            </span>
          )}
        </span>
        <button
          type="button"
          onClick={() =>
            navigator.clipboard?.writeText(draftText(draft)).then(
              () => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1600);
              },
              () => undefined,
            )
          }
          className="-my-1 inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md px-2 text-[12px] text-muted hover:bg-hover hover:text-fg cursor-pointer"
          data-testid="copy-draft"
          title="Copy the draft"
        >
          {copied ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <div className="flex items-center gap-3 border-b border-border px-4 py-2.5 text-[13px]">
        <span className="w-14 shrink-0 text-muted">Subject</span>
        <span className="min-w-0 truncate font-medium">{draft.subject}</span>
      </div>
      <pre className="max-h-[240px] overflow-y-auto px-4 pt-3.5 pb-4 font-sans text-[13px] leading-[21px] whitespace-pre-wrap text-fg/90">{draft.body}</pre>
    </div>
  );
}

function stepDate(at: string | null | undefined) {
  if (!at) return "";
  return formatDate(at.slice(0, 10), { short: true });
}

/** How we know, as a trail: each step on its source system's tile, joined by a line. */
function Trail({ steps, verdict }: { steps: TrailStep[]; verdict?: string | null }) {
  const [all, setAll] = useState(false);
  const shown = all ? steps : steps.slice(0, 6);
  if (!steps.length && !verdict) return null;
  return (
    <section data-testid="detail-evidence">
      <SectionTitle aside={steps.length > 6 && <button type="button" onClick={() => setAll(!all)} className="text-[12px] font-medium text-muted hover:text-fg cursor-pointer">{all ? "Fewer" : `All ${steps.length}`}</button>}>
        How we know
      </SectionTitle>
      {verdict && <p className="mb-3 text-[13px] leading-5">{readable(verdict)}</p>}
      <ol className="relative">
        {shown.map((s, i) => (
          <li key={i} className="relative grid grid-cols-[18px_minmax(0,1fr)_auto] gap-x-3 pb-4 last:pb-0">
            {i < shown.length - 1 && <span className="absolute top-[22px] bottom-[2px] left-[8.5px] w-px bg-border-strong" aria-hidden />}
            <SystemTile system={s.source?.system} size={18} className="mt-px" />
            <div className="min-w-0">
              <div className="text-[13px] leading-5">{readable(s.title)}</div>
              {s.source && (
                <div className="text-[12px] leading-4">
                  <SourceChip source={s.source} />
                </div>
              )}
            </div>
            <span className="pt-0.5 text-[12px] leading-4 tabular-nums text-muted">{stepDate(s.at)}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

function Property({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[84px_minmax(0,1fr)] items-baseline gap-3 py-1">
      <dt className="text-[12px] text-muted">{label}</dt>
      <dd className="min-w-0 text-[13px] leading-5 break-words">{children}</dd>
    </div>
  );
}

function sourceKey(s: Source) {
  return `${s.handle ?? ""}${s.label ?? ""}`;
}

/** The next step as one instruction that names the supplier; other rules keep the server's words. */
export function stepWords(rule: string | null | undefined, text: string, who: string | null): string {
  const them = who ?? "the supplier";
  switch (rule) {
    case "no_confirmation":
      return `Ask ${them} to confirm a delivery date`;
    case "late_ask_supplier":
      return `Ask ${them} for a confirmed delivery date`;
    case "undated_ask_date":
      return `Ask ${them} for a delivery date and record it on the order`;
    default:
      return readable(text);
  }
}

const MORE: CardActionOffer["kind"][] = ["snooze", "dismiss"];

/** The card's actions: send first, then message, follow up and done, the rest under More. Each opens its preview; nothing happens before its confirm. */
function Actions({ card, state, onDone }: { card: ProactiveSuggestion; state?: ActionResult | null; onDone: (r: ActionResult) => void }) {
  const [open, setOpen] = useState<CardActionOffer | null>(null);
  const offers = card.actions ?? [];
  if (!offers.length) return null;
  const rank = (k: string) => ["send_email", "teams_message", "create_follow_up", "mark_done"].indexOf(k);
  const shown = offers.filter((o) => !MORE.includes(o.kind)).sort((a, b) => ((rank(a.kind) + 9) % 9) - ((rank(b.kind) + 9) % 9));
  const more = offers.filter((o) => MORE.includes(o.kind));
  // a phone has room for send and message; the rest go under More
  const narrow = shown.filter((o) => o.kind === "create_follow_up" || o.kind === "mark_done");
  return (
    <div className="mt-3 flex items-center gap-1" data-testid="card-actions">
      {shown.map((o) => (
        <button
          key={o.kind}
          type="button"
          onClick={() => setOpen(o)}
          className={cn(
            "h-8 shrink-0 rounded-lg px-3 text-[13px] cursor-pointer",
            narrow.includes(o) && "max-sm:hidden",
            o.kind === "send_email" ? "mr-1.5 bg-accent font-medium text-accent-fg shadow-sm hover:brightness-110" : "text-muted hover:bg-hover hover:text-fg",
          )}
          data-testid={`action-${o.kind}`}
        >
          {o.kind === "send_email" ? "Review and send…" : o.label}
        </button>
      ))}
      {more.length + narrow.length > 0 && (
        <Menu.Root>
          <Menu.Trigger className="ml-auto flex size-8 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg cursor-pointer" aria-label="More actions">
            <MoreHorizontal className="size-4" aria-hidden />
          </Menu.Trigger>
          <Menu.Portal>
            <Menu.Content align="end" sideOffset={4} className="z-50 min-w-36 rounded-xl bg-raised p-1.5 shadow-pop">
              {[...narrow, ...more].map((o) => (
                <Menu.Item key={o.kind} onSelect={() => setOpen(o)} className={cn("flex cursor-pointer items-center rounded-md px-2 py-1.5 text-row outline-none data-[highlighted]:bg-hover", narrow.includes(o) && "sm:hidden")} data-testid={`action-${o.kind}`}>
                  {o.label}
                </Menu.Item>
              ))}
            </Menu.Content>
          </Menu.Portal>
        </Menu.Root>
      )}
      {open && (
        <ActionDialog
          card={card}
          offer={open}
          state={state}
          onClose={() => setOpen(null)}
          onDone={(r) => {
            setOpen(null);
            onDone(r);
          }}
        />
      )}
    </div>
  );
}

/** What the header says under the title: how late, and whether the supplier has given a date. Words in ink; the glyph carries the colour. */
function Facts({ row, page }: { row: QueueRow; page: ObjectPage | null }) {
  const out: React.ReactNode[] = [];
  if (row.days != null && row.status === "late")
    out.push(
      <span key="late" className="inline-flex items-center gap-1.5">
        <AgeRing days={row.days} size={14} />
        {row.days} days late
      </span>,
    );
  else if (row.group === "po_undated")
    out.push(
      <span key="undated" className="inline-flex items-center gap-1.5">
        <OpenRing size={14} />
        No due date
      </span>,
    );
  else if (row.item) out.push(<span key="kind">{briefingKind(row.item.event_type, { amountSuspect: !!row.item.amount_suspect, fallbackLabel: row.item.label }).label}</span>);
  const eta = row.suggestion?.eta;
  const trailEta = page?.trail?.trails?.[0]?.eta?.state;
  if (eta?.answer === "yes" || trailEta === "confirmed")
    out.push(
      <span key="eta" className="inline-flex items-center gap-1.5">
        <DoneRing size={14} />
        Supplier confirmed{eta?.when ? ` ${formatDate(eta.when, { short: true })}` : ""}
      </span>,
    );
  else if (eta || trailEta || row.status === "late")
    out.push(
      <span key="eta" className="inline-flex items-center gap-1.5 text-muted">
        <OpenRing size={14} tone="neutral" />
        {eta?.answer === "conflicting" ? "Supplier dates disagree" : "No date from the supplier"}
      </span>,
    );
  if (!out.length) return null;
  return <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-[13px] leading-5">{out}</div>;
}

export function TodayDetail({
  row,
  state,
  onDone,
  className,
}: {
  row: QueueRow;
  state?: ActionResult | null;
  onDone: (r: ActionResult) => void;
  className?: string;
}) {
  const [page, setPage] = useState<ObjectPage | null>(null);
  const [failed, setFailed] = useState(false);
  const saved = useSaved();

  useEffect(() => {
    const c = new AbortController();
    setPage(null);
    setFailed(false);
    getObject(row.type, row.objectId, c.signal, true)
      .then((p) => {
        if (c.signal.aborted) return;
        // the briefing already read this PO's trail: shown at once, then the page's own
        const withTrail = !p.trail && row.item?.trail ? ({ ...p, trail: row.item.trail } as ObjectPage) : p;
        setPage(withTrail);
        // a page read whole already holds its trail: no second request
        if (!p.complete && /^(PurchaseOrder|SalesOrder)$/.test(row.type))
          getObjectSection("trail", row.type, row.objectId, c.signal)
            .then((s) => !c.signal.aborted && s.trail && setPage((cur) => (cur ? mergeSection(cur, s) : cur)))
            .catch(() => undefined);
      })
      .catch(() => !c.signal.aborted && setFailed(true));
    return () => c.abort();
  }, [row.type, row.objectId, row.item?.trail]);

  const head = page ? recordHead(page) : null;
  const s = row.suggestion;
  const trail = page?.trail?.trails?.[0] ?? row.item?.trail?.trails?.[0];
  const steps = (trail?.steps ?? []).filter((x) => x.kind !== "verdict");
  const supplierRef = (page?.related ?? []).find((r) => /^(Supplier|Vendor)$/.test(r.type));
  const who = row.counterpart ?? supplierRef?.label ?? null;
  const next = s?.action ?? row.item?.next_action ?? null;
  const action = next ? stepWords(next.rule, next.text, who) : null;
  const missing = (s?.missing ?? []).length ? (s?.missing ?? []) : (page?.not_found ?? []).map((n) => readable(n.what));
  const facts = (head?.keyFacts ?? []).filter((f) => f.value);
  const sources = [...(row.late?.sources ?? row.item?.sources ?? [])].filter((x, i, all) => all.findIndex((y) => sourceKey(y) === sourceKey(x)) === i);
  const related = (page?.related ?? []).filter((r) => !/^(Supplier|Vendor)$/.test(r.type));
  const timeline = page?.timeline ?? [];
  const typeName = typeLabel(row.type);
  const saveKey = `o:${row.type}/${row.objectId}`;
  const isSaved = saved.some((x) => x.key === saveKey);
  const ask = `/ask?q=${encodeURIComponent(`What's the latest on ${row.title}?`)}&object=${encodeURIComponent(row.objectId)}`;

  // the figures: total and received from the record; the dates from the order and its need
  const f = page ? factMap(page.facts) : null;
  const total = head?.stats.find((x) => x.label === "Total");
  const got = receivedOf(trail);
  const ordered = dayOf(f?.get("order_date")?.value ?? f?.get("document_date")?.value);
  const needDay = dayOf(row.late?.due_date ?? null) ?? dayOf(/needed-by date (\d{4}-\d{2}-\d{2})|requested-by date (\d{4}-\d{2}-\d{2})/.exec(trail?.verdict ?? "")?.slice(1).find(Boolean));
  const isPo = row.type === "PurchaseOrder";

  return (
    <div className={cn("flex min-h-0 flex-1 flex-col bg-bg", className)} data-testid="today-detail">
      <div className="hidden h-[52px] shrink-0 items-center gap-1 border-b border-border px-5 lg:flex">
        <span className="min-w-0 truncate text-[13px] text-muted">
          {typeName} <span className="px-0.5 text-faint">›</span> <span className="font-medium text-fg">{row.title}</span>
        </span>
        <div className="ml-auto flex items-center gap-0.5">
          <button
            type="button"
            aria-pressed={isSaved}
            onClick={() => toggleSaved({ key: saveKey, kind: "object", label: row.title, href: objectHref(row.type, row.objectId) })}
            className={cn("inline-flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-[13px] hover:bg-hover cursor-pointer", isSaved ? "text-fg" : "text-muted hover:text-fg")}
            title={isSaved ? "Saved" : "Save"}
          >
            <Bookmark className={cn("size-[15px]", isSaved && "fill-current")} strokeWidth={2} aria-hidden />
            {isSaved ? "Saved" : "Save"}
          </button>
          <Link href={ask} className="inline-flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-[13px] text-muted hover:bg-hover hover:text-fg">
            <MessageCircle className="size-[15px]" strokeWidth={2} aria-hidden />
            Ask
          </Link>
          <Link
            href={row.href}
            className="ml-1.5 inline-flex h-8 items-center gap-1 rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-3 text-[13px] font-medium text-fg hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)]"
            data-testid="open-record"
          >
            Open {isPo ? "PO" : typeName.toLowerCase()}
            <ArrowUpRight className="size-3.5" strokeWidth={2.2} aria-hidden />
          </Link>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="grid min-h-full xl:grid-cols-[minmax(0,1fr)_256px]">
          <div className="min-w-0 space-y-8 px-6 pt-6 pb-12 sm:px-8 sm:pt-7">
            <header className="flex gap-4">
              <Monogram name={who} size={48} />
              <div className="min-w-0">
                <div className="flex flex-wrap items-baseline gap-x-2.5">
                  <h2 className="text-[24px] leading-[30px] font-semibold tracking-[-0.025em]">{row.title}</h2>
                  {who &&
                    (supplierRef ? (
                      <Link href={objectHref(supplierRef.type, supplierRef.object_id)} className="text-[17px] leading-[22px] tracking-[-0.01em] text-muted hover:text-fg">
                        {who}
                      </Link>
                    ) : (
                      <span className="text-[17px] leading-[22px] tracking-[-0.01em] text-muted">{who}</span>
                    ))}
                </div>
                <Facts row={row} page={page} />
              </div>
            </header>

            {(page || !failed) && (
              <section className="space-y-6" data-testid="detail-stats">
                {page ? (
                  <div className="grid grid-cols-2 gap-6 sm:grid-cols-[auto_auto_1fr] sm:gap-10">
                    {total && <Figure label="Total" value={total.value} unit={total.caption} />}
                    {got && (
                      <Figure label="Received" value={got.lines ? got.got : `${got.got} of ${got.of}`} unit={got.lines ? `${got.lines} lines short` : got.uom}>
                        {got.pct != null && (
                          <div className="mt-2 flex items-center gap-2">
                            <span className="relative h-1 w-24 overflow-hidden rounded-full bg-[color-mix(in_srgb,var(--fg)_10%,transparent)]">
                              <span className="absolute inset-y-0 left-0 rounded-full bg-fg/70" style={{ width: `${Math.max(3, got.pct)}%` }} />
                            </span>
                            <span className="text-[12px] tabular-nums text-muted">{got.pct}%</span>
                          </div>
                        )}
                      </Figure>
                    )}
                  </div>
                ) : (
                  <div className="flex gap-10">
                    {[0, 1].map((i) => (
                      <div key={i} className="space-y-2">
                        <div className="skeleton h-3 w-12" />
                        <div className="skeleton h-6 w-28" />
                      </div>
                    ))}
                  </div>
                )}
                {isPo && page && <OrderLine ordered={ordered} needed={needDay} daysLate={row.status === "late" ? row.days : null} />}
              </section>
            )}

            {(action || s?.draft) && (
              <section data-testid="detail-next">
                <SectionTitle>Next step</SectionTitle>
                {action && <p className="mb-4 text-[16px] leading-[22px] font-medium tracking-[-0.01em]">{action}</p>}
                {s?.draft && <DraftSheet draft={s.draft} who={who} />}
                {s && <Actions card={s} state={state} onDone={onDone} />}
              </section>
            )}

            <Trail steps={steps} verdict={steps.length ? null : trail?.verdict} />

            {missing.length > 0 && (
              <section data-testid="suggestion-missing">
                <SectionTitle>Not on record</SectionTitle>
                <ul className="space-y-2">
                  {missing.map((m, i) => (
                    <li key={i} className="flex gap-3 text-[13px] leading-5">
                      <OpenRing size={14} tone="neutral" className="mt-[3px] ml-0.5" />
                      {m}
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {failed && (
              <p className="text-[13px] text-muted">
                The record didn&apos;t load.{" "}
                <Link href={row.href} className="text-fg underline underline-offset-2">
                  Open it
                </Link>
              </p>
            )}
          </div>

          <aside className="min-w-0 space-y-7 border-t border-border bg-[color-mix(in_srgb,var(--surface)_70%,var(--bg))] px-6 py-6 sm:px-8 xl:border-t-0 xl:border-l xl:px-6 xl:pt-7" aria-label="Details">
            <section>
              <SectionTitle>Details</SectionTitle>
              {page ? (
                <dl>
                  {facts.map((x) => (
                    <Property key={x.label} label={x.label}>
                      {x.label === "Supplier" && supplierRef ? (
                        <Link href={objectHref(supplierRef.type, supplierRef.object_id)} className="hover:underline">
                          {x.value}
                        </Link>
                      ) : (
                        x.value
                      )}
                    </Property>
                  ))}
                </dl>
              ) : (
                <div className="space-y-3 pt-1">
                  {[0, 1, 2, 3].map((i) => (
                    <div key={i} className="skeleton h-3.5" style={{ width: `${70 - i * 9}%` }} />
                  ))}
                </div>
              )}
            </section>

            {related.length > 0 && (
              <section>
                <SectionTitle>Linked</SectionTitle>
                <ul className="-mx-2">
                  {related.slice(0, 6).map((r) => (
                    <li key={r.object_id}>
                      <Link href={objectHref(r.type, r.object_id)} className="group flex h-8 items-center justify-between gap-2 rounded-lg px-2 text-[13px] hover:bg-hover">
                        <span className="truncate">{r.label && r.label !== typeLabel(r.type) && !/^(PO line|Receipt)$/.test(r.label) ? recordLabel(r.type, r.label) : typeLabel(r.type)}</span>
                        <ArrowUpRight className="size-3.5 shrink-0 text-faint group-hover:text-fg" aria-hidden />
                      </Link>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {sources.length > 0 && (
              <section>
                <SectionTitle>Sources</SectionTitle>
                <ul className="space-y-2">
                  {sources.slice(0, 5).map((src, i) => (
                    <li key={i} className="flex items-center gap-2.5 text-[13px]">
                      <SystemTile system={src.system} size={18} />
                      <SourceChip source={src} />
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {timeline.length > 0 && (
              <section>
                <SectionTitle>Activity</SectionTitle>
                <ol className="space-y-2">
                  {timeline.slice(0, 5).map((t, i) => (
                    <li key={i} className="flex items-baseline justify-between gap-3 text-[13px] leading-5">
                      <span className="min-w-0 truncate">{readable(t.title)}</span>
                      <span className="shrink-0 text-[12px] tabular-nums text-muted">{stepDate(t.at)}</span>
                    </li>
                  ))}
                </ol>
              </section>
            )}
          </aside>
        </div>
      </div>
    </div>
  );
}
