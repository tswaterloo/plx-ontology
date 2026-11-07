"use client";

import { ArrowUpRight, Bookmark, ChevronDown, MessageCircle } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { getActionStates, getObject, getObjectSection, getSuggestions, mergeSection, PAGE_SECTIONS } from "@/lib/api";
import { cn } from "@/lib/cn";
import { custodyOf } from "@/lib/custody";
import { ApiError, noticeKind, plainError } from "@/lib/errors";
import { formatDate, money, objectHref, plural, readable, recordLabel, safeHref, SYSTEM_LABEL, typeLabel } from "@/lib/format";
import { activityTitle, factMap, groupLinked, groupRelated, moneyText, productionSteps, receivedOf, recordHead, type RecordHead, type Thread } from "@/lib/record";
import { toggleSaved, useSaved } from "@/lib/saved";
import { lateness, recordState, type Shown } from "@/lib/status";
import type { ActionResult, Conflict, CustomerAmount, CustomerSummary, LandedCost, LateLine, LinkedItem, ObjectPage, ObjectRef, OrderRow, Person, ProactiveSuggestion, Profitability, SectionName, Source, TimelineEvent, Tone } from "@/lib/types";
import { conflictSentence, ConflictCard, FreshnessLabel, HiddenNotice, missingText, SourceLine } from "./answer-parts";
import { CostBreakdown, MarginHeadline, NoMargin, ProfitabilityView, SageMargin } from "./cost-breakdown";
import { CustodyRail } from "./custody-rail";
import { EtaCard } from "./eta-confirmed";
import { EvidenceTrailCard, trailNextAction } from "./evidence-trail";
import { ProposalCards } from "./proposal-cards";
import { DraftSheet, SuggestionActions } from "./record/next-step";
import { Bars, dayOf, Fact, Figure, Glyph, LinkRow, MoreButton, OrderLine, Progress, Property, SectionTitle } from "./record/parts";
import { EvidenceDrawer, SourceChip, SourceChips } from "./source-chip";
import { stepWords } from "./today-detail";
import { DoneRing, OpenRing, StatusGlyph, SystemTile } from "./ui/glyphs";
import { Toolbar, ToolbarButton } from "./ui/toolbar";
import { LoadProblem, StaleNote } from "./ui/notice";
import { Monogram } from "./ui/monogram";
import { StatusBadge, StatusWord } from "./ui/status-badge";

/** Status words the API uses, mapped to a tone (kept for callers outside the record page). */
export function toneOf(state: string): Tone {
  const s = recordState(state);
  return s === "late" ? "bad" : s === "risk" ? "warn" : s === "ok" ? "good" : "neutral";
}

function refHref(r: ObjectRef): string {
  return safeHref(r.open_url) ?? objectHref(r.type, r.object_id);
}

function useElapsed(on: boolean): number {
  const [s, setS] = useState(0);
  useEffect(() => {
    if (!on) return;
    setS(0);
    const t0 = Date.now();
    const id = setInterval(() => setS(Math.round((Date.now() - t0) / 1000)), 1000);
    return () => clearInterval(id);
  }, [on]);
  return s;
}

const INSPECTOR = "bg-[color-mix(in_srgb,var(--surface)_70%,var(--bg))]";

/** The page's frame: the toolbar over a main column and the inspector beside it (under it below xl). */
function Frame({ crumb, actions, main, inspector }: { crumb: ReactNode; actions?: ReactNode; main: ReactNode; inspector?: ReactNode }) {
  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="record-page">
      <Toolbar title={crumb} actions={actions} />
      <div className={cn("grid flex-1", inspector && "xl:grid-cols-[minmax(0,1fr)_272px]")}>
        <div className="min-w-0 space-y-9 px-5 pt-6 pb-12 sm:px-8 sm:pt-7">{main}</div>
        {inspector && (
          <aside
            className={cn("grid min-w-0 content-start gap-x-10 gap-y-7 border-t border-border px-5 py-6 sm:grid-cols-2 sm:px-8 xl:grid-cols-1 xl:border-t-0 xl:border-l xl:px-6 xl:pt-7", INSPECTOR)}
            aria-label="Details"
          >
            {inspector}
          </aside>
        )}
      </div>
    </div>
  );
}

/** "Purchase order › PO 106001", in the toolbar's quiet size. */
function Crumb({ type, title }: { type: string; title?: string | null }) {
  return (
    <span className="text-[13px] font-normal tracking-normal">
      <span className={cn("text-muted", title && "max-sm:hidden")}>{typeLabel(type)}</span>
      {title && (
        <>
          <span className="px-1.5 text-faint max-sm:hidden" aria-hidden>
            ›
          </span>
          <span className="font-medium text-fg">{title}</span>
        </>
      )}
    </span>
  );
}

/** The page's shape while it gathers: the header from the link's label at once, then a step line. */
function ObjectSkeleton({ type, label }: { type: string; label?: string | null }) {
  const t = useElapsed(true);
  const step = t < 8 ? "Reading Business Central…" : t < 20 ? "Searching mail and files…" : "Linking receipts and emails…";
  return (
    <Frame
      crumb={<Crumb type={type} title={label} />}
      main={
        <div className="space-y-9" role="status" aria-label="Loading">
          <header className="flex gap-4">
            <div className="skeleton size-12 shrink-0 rounded-[13px]" />
            <div className="min-w-0 space-y-2 pt-0.5">
              {label ? <h1 className="text-[24px] leading-[30px] font-semibold tracking-[-0.025em]">{label}</h1> : <div className="skeleton h-7 w-56" />}
              <p className="text-[13px] leading-5 text-muted" data-pending="">
                {t < 3 ? "Loading the record…" : step}
              </p>
            </div>
          </header>
          <div className="flex gap-10">
            {[0, 1, 2].map((i) => (
              <div key={i} className="space-y-2">
                <div className="skeleton h-3 w-14" />
                <div className="skeleton h-6 w-28" />
              </div>
            ))}
          </div>
          <Bars n={4} className="max-w-xl" />
          {t >= 20 && <p className="text-[12px] text-muted">Records with a lot of mail take up to a minute.</p>}
        </div>
      }
      inspector={
        <section>
          <SectionTitle>Details</SectionTitle>
          <Bars n={4} />
        </section>
      }
    />
  );
}

/** Lines of the order, late first. A line the API sends without an item reads "Line n", never the PO's own label. */
export function LinesTable({ lines, title = "Lines" }: { lines: LateLine[]; title?: string }) {
  if (lines.length === 0) return null;
  const sorted = [...lines].sort((a, b) => (b.days_late ?? -1) - (a.days_late ?? -1));
  const name = (l: LateLine, i: number) => (l.item ? l.item : /^\d+$/.test(l.line.trim()) ? `Line ${l.line.trim()}` : `Line ${i + 1}`);
  const shownOf = (l: LateLine, phone = false) => lateness(typeof l.days_late === "number" && l.days_late > 0 ? "late" : !phone && l.days_late === 0 ? "on_time" : null, l.days_late);
  return (
    <section data-testid="late-lines">
      <SectionTitle aside={<span className="text-[12px] text-muted">{plural(lines.length, "line")}</span>}>{title}</SectionTitle>
      <table className="hidden w-full text-[13px] sm:table">
        <caption className="sr-only">{title}, late first</caption>
        <thead>
          <tr className="h-8 border-b border-border text-left text-[12px] text-muted">
            <th scope="col" className="pr-3 font-normal">Item</th>
            <th scope="col" className="pr-3 text-right font-normal">Ordered</th>
            <th scope="col" className="pl-6 font-normal">Needed by</th>
            <th scope="col" className="text-right font-normal">Status</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((l, i) => {
            const st = shownOf(l);
            return (
              <tr key={i} className="h-10 border-b border-border">
                <td className="pr-3 font-medium">
                  {name(l, i)} {l.sources && l.sources.length > 0 && <SourceChips sources={l.sources} max={1} />}
                </td>
                <td className="whitespace-nowrap pr-3 text-right tabular-nums">{l.quantity ?? ""}</td>
                <td className="whitespace-nowrap pl-6 tabular-nums">{l.due_date ? formatDate(l.due_date) : ""}</td>
                <td className="text-right">{st ? <StatusWord shown={st} /> : null}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <ul className="divide-y divide-border border-y border-border sm:hidden">
        {sorted.map((l, i) => {
          const st = shownOf(l, true);
          return (
            <li key={i} className="py-2.5">
              <div className="flex items-center justify-between gap-2 text-[13px] font-medium">
                <span className="min-w-0 truncate">{name(l, i)}</span>
                {st && <StatusWord shown={st} />}
              </div>
              <p className="text-[12px] text-muted">{[l.quantity ? `Ordered ${l.quantity}` : null, l.due_date ? `Needed ${formatDate(l.due_date, { short: true })}` : null].filter(Boolean).join(" · ") || "No quantity or date"}</p>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** A supplier's purchase orders: late first (from the late rows), then the rest, two columns on a wide screen. */
function SupplierOrders({ obj }: { obj: ObjectPage }) {
  const [all, setAll] = useState(false);
  const pos = (obj.related ?? []).filter((r) => r.type === "PurchaseOrder");
  const lateLabels = new Map<string, LateLine>();
  for (const l of obj.late_lines ?? []) {
    const m = /PO number:\s*([\w-]+)/.exec(l.line);
    if (m) lateLabels.set(m[1], l);
  }
  const days = /Late by (\d+) days/i.exec(obj.status?.text ?? "")?.[1];
  const rows = pos.map((p) => ({ p, late: lateLabels.get(p.label) ?? null })).sort((a, b) => Number(!!b.late) - Number(!!a.late));
  if (rows.length === 0) return null;
  const cap = rows.length > 18 ? 12 : rows.length;
  const shown = all ? rows : rows.slice(0, cap);
  return (
    <section data-testid="supplier-orders">
      <SectionTitle aside={rows.length > cap && <MoreButton onClick={() => setAll((v) => !v)} expanded={all}>{all ? "Fewer" : `All ${rows.length}`}</MoreButton>}>Purchase orders</SectionTitle>
      <ul className="-mx-3 grid grid-cols-2 gap-x-2 sm:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-5">
        {shown.map(({ p, late }) => {
          const d = late?.days_late ?? (days && lateLabels.size === 1 ? Number(days) : null);
          return (
            <li key={p.object_id}>
              <Link href={refHref(p)} className="group flex h-10 items-center gap-3 rounded-[10px] px-3 text-[13px] hover:bg-hover">
                <span className="min-w-0 flex-1 truncate font-medium">{recordLabel(p.type, p.label)}</span>
                {late && (
                  <span className="flex shrink-0 items-center gap-1.5 text-[12px] tabular-nums">
                    {d != null ? `${d}d` : "Late"}
                    <Glyph status="late" days={d ?? 90} size={13} />
                  </span>
                )}
                <ArrowUpRight className="size-3.5 shrink-0 text-faint opacity-0 group-hover:opacity-100" aria-hidden />
              </Link>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** A customer's orders as rows: number, date, status in ink with its shape, amount at the right. */
/** An amount as a number, for the rows' bars: "4505.00" -> 4505. */
const amountOf = (m: OrderRow["amount"]) => (m ? Math.abs(Number(String(m.amount).replace(/[^\d.-]/g, ""))) || 0 : 0);

function OrderTable({ title, rows, testId }: { title: string | null; rows: OrderRow[]; testId: string }) {
  if (rows.length === 0) return null;
  const top = Math.max(...rows.map((r) => amountOf(r.amount)), 1);
  return (
    <div data-testid={testId}>
      {title && <h3 className="mb-1 text-[12px] leading-4 text-muted">{title}</h3>}
      <ul className="-mx-3">
        {rows.map((r) => (
          <li key={`${r.object_id}-${r.label}`}>
            <Link href={safeHref(r.open_url) ?? objectHref("SalesOrder", r.object_id)} className="grid h-10 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-6 rounded-[10px] px-3 text-[13px] hover:bg-hover sm:grid-cols-[132px_72px_minmax(0,1fr)_auto]">
              <span className="min-w-0 truncate font-semibold">{r.label}</span>
              <span className="tabular-nums text-muted max-sm:hidden">{r.date ? formatDate(r.date, { short: true }) : ""}</span>
              <span className="flex min-w-0 items-center gap-4 max-sm:hidden">
                {r.status && <StatusWord status={recordState(r.status)} label={r.status} />}
                {r.amount && (
                  <span className="relative h-1.5 w-full max-w-[220px] overflow-hidden rounded-full bg-[color-mix(in_srgb,var(--fg)_7%,transparent)]" aria-hidden>
                    <span className="absolute inset-y-0 left-0 rounded-full bg-[color-mix(in_srgb,var(--fg)_38%,transparent)]" style={{ width: `${Math.max(3, (amountOf(r.amount) / top) * 100)}%` }} />
                  </span>
                )}
              </span>
              <span className="text-right tabular-nums">{r.amount ? money(r.amount) : ""}</span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** A customer's open and recent orders, and what is not on record about them. */
function CustomerOrders({ c, fallbackOrders }: { c: CustomerSummary; fallbackOrders: ObjectRef[] }) {
  const orders = (c.open_list ?? []).length + (c.recent_orders ?? []).length;
  if (orders === 0 && fallbackOrders.length === 0 && (c.not_established ?? []).length === 0) return null;
  return (
    <section className="space-y-3" data-testid="customer">
      <SectionTitle
        className="mb-0"
        aside={
          typeof c.orders_total === "number" ? (
            <span className="text-[12px] text-muted" data-testid="orders-total">{plural(c.orders_total, "order")}</span>
          ) : typeof c.orders_at_least === "number" ? (
            <span className="text-[12px] text-muted" data-testid="orders-total">{c.orders_at_least}+ orders</span>
          ) : null
        }
      >
        {(c.open_list ?? []).length && !(c.recent_orders ?? []).length ? "Open orders" : !(c.open_list ?? []).length && (c.recent_orders ?? []).length ? "Recent orders" : "Orders"}
      </SectionTitle>
      <OrderTable title={(c.recent_orders ?? []).length ? "Open" : null} rows={c.open_list ?? []} testId="open-orders" />
      <OrderTable title={(c.open_list ?? []).length ? "Recent" : null} rows={c.recent_orders ?? []} testId="recent-orders" />
      {orders === 0 && fallbackOrders.length > 0 && (
        <ul className="-mx-2">
          {fallbackOrders.map((o) => (
            <li key={o.object_id}>
              <LinkRow href={refHref(o)}>{o.label}</LinkRow>
            </li>
          ))}
        </ul>
      )}
      {(c.not_established ?? []).length > 0 && <Missing items={c.not_established!.map((n) => readable(n))} />}
    </section>
  );
}

/** Things not on record: an open ring each, the words in ink. */
function Missing({ items, testId }: { items: string[]; testId?: string }) {
  return (
    <ul className="space-y-1.5" data-testid={testId}>
      {items.map((m, i) => (
        <li key={i} className="flex gap-2.5 text-[13px] leading-5">
          <OpenRing size={14} tone="neutral" className="mt-[3px]" />
          <span className="min-w-0">{m}</span>
        </li>
      ))}
    </ul>
  );
}

/** Activity in the inspector: newest first, repeats collapsed, eight then "All". */
function Activity({ events }: { events: TimelineEvent[] }) {
  const [all, setAll] = useState(false);
  if (events.length === 0) return null;
  // newest first, a run of the same title on one day said once with its count
  const sorted = [...events].sort((a, b) => (b.at ?? "").localeCompare(a.at ?? ""));
  const rows: { e: TimelineEvent; count: number }[] = [];
  for (const e of sorted) {
    const prev = rows[rows.length - 1];
    if (prev && prev.e.title === e.title && (prev.e.at ?? "").slice(0, 10) === (e.at ?? "").slice(0, 10)) prev.count += 1;
    else rows.push({ e, count: 1 });
  }
  const shown = all ? rows : rows.slice(0, 8);
  return (
    <section data-testid="timeline">
      <SectionTitle aside={rows.length > 8 && <MoreButton onClick={() => setAll((v) => !v)} expanded={all}>{all ? "Fewer" : `All ${events.length}`}</MoreButton>}>Activity</SectionTitle>
      <ol className="-mx-2 space-y-px">
        {shown.map(({ e, count }, i) => (
          <ActivityRow key={i} e={e} count={count} />
        ))}
      </ol>
    </section>
  );
}

function ActivityRow({ e, count }: { e: TimelineEvent; count: number }) {
  const [open, setOpen] = useState(false);
  const title = activityTitle(e.title, count);
  const body = (
    <>
      <SystemTile system={e.system} size={16} className="mt-[2px]" />
      <span className="min-w-0 flex-1">
        <span className="block text-[13px] leading-5">{title}</span>
        {e.summary && count === 1 && <span className="block truncate text-[12px] leading-4 text-muted">{readable(e.summary)}</span>}
        <span className="sr-only">{SYSTEM_LABEL[e.system] ?? e.system}</span>
      </span>
      <span className="shrink-0 pt-0.5 text-[12px] leading-4 tabular-nums text-muted">{e.at ? formatDate(e.at, { short: true }).replace(/ \d{4}$/, "") : ""}</span>
    </>
  );
  return (
    <li>
      {e.source && count === 1 ? (
        <>
          <button type="button" onClick={() => setOpen(true)} className="flex w-full items-start gap-2.5 rounded-lg px-2 py-1 text-left hover:bg-hover cursor-pointer">
            {body}
          </button>
          {open && <EvidenceDrawer source={e.source} open={open} onOpenChange={setOpen} />}
        </>
      ) : (
        <div className="flex items-start gap-2.5 px-2 py-1">{body}</div>
      )}
    </li>
  );
}

const LINK_SYSTEM = { email: "Email", teams: "Teams", document: "File", record: "BC", task: "Portal" } as const;
const LINK_TAG = { email: "Email", teams: "Teams", document: "File", record: "Record", task: "Task" } as const;

function ThreadRow({ t }: { t: Thread }) {
  const [open, setOpen] = useState<LinkedItem | null>(null);
  const [expanded, setExpanded] = useState(false);
  const tag = LINK_TAG[t.kind] ?? "Message";
  const first = t.items[0];
  const subject = /^email message$/i.test(t.subject) ? "(no subject)" : t.subject;
  return (
    <li>
      <button
        type="button"
        onClick={() => (t.count > 1 ? setExpanded((v) => !v) : setOpen(first))}
        className="flex w-full items-start gap-3 rounded-[10px] px-3 py-2 text-left hover:bg-hover cursor-pointer"
        aria-expanded={t.count > 1 ? expanded : undefined}
      >
        <SystemTile system={first?.source?.system ?? LINK_SYSTEM[t.kind]} size={20} className="mt-px" />
        <span className="min-w-0 flex-1">
          <span className="line-clamp-2 text-[13px] leading-5 font-medium">{subject}</span>
          <span className="block text-[12px] leading-4 text-muted">
            {tag}
            {t.count > 1 ? `, ${t.count} messages` : ""}
          </span>
        </span>
        {t.latest && <span className="shrink-0 pt-0.5 text-[12px] leading-4 tabular-nums text-muted">{formatDate(t.latest, { short: true })}</span>}
        {t.count > 1 && <ChevronDown className={cn("mt-0.5 size-4 shrink-0 text-faint transition-transform", expanded && "rotate-180")} aria-hidden />}
      </button>
      {expanded && (
        <ul className="mb-1 ml-[44px] space-y-px border-l border-border pl-2">
          {t.items.map((l, i) => (
            <li key={i}>
              <button type="button" onClick={() => setOpen(l)} className="flex w-full gap-3 rounded-md px-2 py-1 text-left text-[13px] hover:bg-hover cursor-pointer">
                <span className="w-14 shrink-0 tabular-nums text-muted">{l.at ? formatDate(l.at, { short: true }).replace(/ \d{4}$/, "") : "Undated"}</span>
                <span className="min-w-0 truncate">{l.label}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {open && <EvidenceDrawer source={open.source} open={!!open} onOpenChange={(o) => !o && setOpen(null)} />}
    </li>
  );
}

function Linked({ items }: { items: LinkedItem[] }) {
  const [all, setAll] = useState(false);
  if (items.length === 0) return null;
  const threads = groupLinked(items);
  const shown = all ? threads : threads.slice(0, 8);
  return (
    <section data-testid="linked">
      <SectionTitle aside={threads.length > 8 ? <MoreButton onClick={() => setAll((v) => !v)} expanded={all}>{all ? "Fewer" : `All ${threads.length}`}</MoreButton> : <span className="text-[12px] text-muted">{items.length}</span>}>
        Messages and documents
      </SectionTitle>
      <ul className="-mx-3 grid gap-x-4 2xl:grid-cols-2">
        {shown.map((t) => (
          <ThreadRow key={`${t.kind}-${t.subject}`} t={t} />
        ))}
      </ul>
    </section>
  );
}

function People({ people }: { people: Person[] }) {
  if (people.length === 0) return null;
  return (
    <section data-testid="people">
      <SectionTitle>People</SectionTitle>
      <ul className="space-y-2.5">
        {people.map((p, i) => (
          <li key={i} className="flex items-center gap-2.5">
            <Monogram name={p.name} size={26} className="rounded-full" />
            <span className="min-w-0">
              <span className="block truncate text-[13px] leading-[18px] font-medium">{p.name}</span>
              <span className="block truncate text-[12px] leading-4 text-muted">{[p.role, p.organisation].filter(Boolean).join(" · ")}</span>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function RelatedCard({ refs }: { refs: ObjectRef[] }) {
  // a group of rows named only by their type ("PO line", "Receipt") says nothing: left out
  const groups = groupRelated(refs).filter((g) => g.items.length > 0);
  if (groups.length === 0) return null;
  return (
    <section data-testid="related">
      <SectionTitle>Linked</SectionTitle>
      <div className="space-y-3">
        {groups.map((g) => (
          <div key={g.type}>
            <p className="mb-0.5 text-[12px] text-muted">
              {g.label}
              {g.items.reduce((n, x) => n + x.times, 0) + g.unnamed > 1 && <span className="ml-1.5 tabular-nums text-faint">{g.items.reduce((n, x) => n + x.times, 0) + g.unnamed}</span>}
            </p>
            <ul className="-mx-2">
              {g.items.slice(0, 6).map((r) => (
                <li key={r.object_id}>
                  <LinkRow href={refHref(r)} aside={r.times > 1 ? <span className="text-[12px] text-muted">×{r.times}</span> : undefined}>
                    {recordLabel(r.type, readable(r.label))}
                  </LinkRow>
                </li>
              ))}
              {g.items.length > 6 && <li className="px-2 text-[12px] text-muted">and {g.items.length - 6} more</li>}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}

function LandedCostCard({ lc }: { lc: LandedCost }) {
  const lines = lc.lines ?? [];
  return (
    <section data-testid="landed-cost">
      <SectionTitle aside={<StatusBadge status={lc.status === "computed" ? "ok" : "risk"} label={lc.status === "computed" ? "Complete" : "Partial"} />}>Landed cost</SectionTitle>
      <dl className="max-w-xl text-[13px]">
        {lines.map((l, i) => (
          <div key={i} className="flex h-9 items-center justify-between gap-3 border-b border-border">
            <dt className="min-w-0 truncate text-muted">{l.label}</dt>
            <dd className="shrink-0 tabular-nums">{money(l.amount)}</dd>
          </div>
        ))}
        {lc.total ? (
          <div className="flex h-10 items-center justify-between gap-3 font-semibold">
            <dt>Total</dt>
            <dd className="tabular-nums">{money(lc.total)}</dd>
          </div>
        ) : (
          <p className="flex items-start gap-2.5 pt-2.5 leading-5">
            <OpenRing size={14} tone="neutral" className="mt-[3px]" />
            <span>Landed cost isn&apos;t known yet{(lc.missing ?? []).length ? `: ${lc.missing!.join("; ")}` : "."}</span>
          </p>
        )}
        {lc.per_unit && (
          <div className="flex items-center justify-between gap-3 text-muted">
            <dt>Per unit</dt>
            <dd className="tabular-nums">{money(lc.per_unit)}</dd>
          </div>
        )}
      </dl>
    </section>
  );
}

/** Conflicts said in the header with their shape; opened, one plain row each. */
function ConflictsToggle({ conflicts, open, onToggle }: { conflicts: Conflict[]; open: boolean; onToggle: () => void }) {
  if (conflicts.length === 0) return null;
  return (
    <button type="button" onClick={onToggle} aria-expanded={open} className="inline-flex items-center gap-1.5 rounded-md hover:underline cursor-pointer" data-testid="conflicts-badge">
      <StatusGlyph status="suspect" size={14} />
      {plural(conflicts.length, "data conflict")}
      <ChevronDown className={cn("size-3.5 text-muted transition-transform", open && "rotate-180")} aria-hidden />
    </button>
  );
}

function ConflictRows({ conflicts }: { conflicts: Conflict[] }) {
  return (
    <ul className="space-y-2 rounded-xl bg-surface px-4 py-3 text-[13px] leading-5" data-testid="conflict-rows">
      {conflicts.map((c, i) => (
        <li key={i} className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <StatusGlyph status="suspect" size={13} />
          <span>{conflictSentence(c)}</span>
          <SourceChips sources={c.values.map((v) => v.source)} />
          {c.values.some((v) => v.trusted) && <span className="text-[12px] text-muted">Using the {c.values.find((v) => v.trusted)!.source.system === "BC" ? "Business Central" : "AR"} value.</span>}
        </li>
      ))}
    </ul>
  );
}

/**
 * The next step: the instruction as one line that names who, the drafted message as a mail sheet
 * with the card's actions when the proactive engine drafted one, else who to ask and their last
 * message. The record's own drafts sit under it, to edit and open in Outlook.
 */
function NextStep({
  obj,
  head,
  card,
  cardState,
  onDone,
  pending,
}: {
  obj: ObjectPage;
  head: RecordHead;
  card: ProactiveSuggestion | null;
  cardState?: ActionResult | null;
  onDone: (r: ActionResult) => void;
  pending: boolean;
}) {
  const [formOpen, setFormOpen] = useState(false);
  const next = trailNextAction(obj.trail);
  const proposals = obj.proposals ?? [];
  const contact = next?.contact?.replace(/\s*<[^>]+>/, "") ?? null;
  const who = card?.draft?.to.name ?? contact ?? head.counterpart?.label ?? null;
  const rule = card?.action?.rule ?? next?.rule;
  const text = card?.action?.text ?? next?.text;
  if (!text && !card?.draft && proposals.length === 0) return pending ? <Bars n={2} className="max-w-md" /> : null;
  const action = text ? stepWords(rule, text, head.counterpart?.label ?? who) : null;
  const drafts = <ProposalCards proposals={proposals} />;
  return (
    <section id="chase" className="scroll-mt-20" aria-label="Next step" data-testid="who-to-chase">
      <SectionTitle>Next step</SectionTitle>
      {action && <p className="max-w-[72ch] text-[16px] leading-[22px] font-medium tracking-[-0.01em]">{action}</p>}
      {!card?.draft && next && (
        <p className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-[13px] leading-5">
          {who && (
            <span className="inline-flex items-center gap-1.5">
              {contact ? <Monogram name={contact} size={16} className="rounded-full" /> : <OpenRing size={13} />}
              {contact ?? <span className="text-muted">No contact on record for {who}</span>}
            </span>
          )}
          {next.contact_source ? (
            <span className="text-muted">
              <SourceLine source={next.contact_source} prefix="Last message from" />
            </span>
          ) : (
            who && <span className="text-muted">No message from {who} on record</span>
          )}
        </p>
      )}
      {card?.draft && (
        <div className="mt-4">
          <DraftSheet draft={card.draft} who={who} />
          <SuggestionActions card={card} state={cardState} onDone={onDone} />
        </div>
      )}
      {proposals.length > 0 &&
        (card?.draft ? (
          <div className="mt-4">
            <button type="button" onClick={() => setFormOpen((v) => !v)} aria-expanded={formOpen} className="inline-flex items-center gap-1 text-[12px] font-medium text-muted hover:text-fg cursor-pointer">
              Edit and open in Outlook
              <ChevronDown className={cn("size-3.5 transition-transform", formOpen && "rotate-180")} aria-hidden />
            </button>
            <div className={cn("mt-3", !formOpen && "hidden")}>{drafts}</div>
          </div>
        ) : (
          <div className={cn(action && "mt-4")}>{drafts}</div>
        ))}
    </section>
  );
}

/** An assembly's production steps on one line: done steps closed with a tick, missing ones left open. */
function AoSteps({ obj }: { obj: ObjectPage }) {
  const steps = productionSteps(obj);
  if (steps.length === 0) return null;
  return (
    <section data-testid="production-steps">
      <SectionTitle>Production steps</SectionTitle>
      <ol className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:gap-0">
        {steps.map((s, i) => (
          <li key={s.label} className="flex items-center gap-3 sm:min-w-[132px]">
            <span className="flex items-start gap-2.5">
              {s.done ? <DoneRing size={16} className="mt-px" /> : <OpenRing size={16} className="mt-px" />}
              <span>
                <span className="block text-[13px] leading-5 font-medium">{s.label}</span>
                <span className="block text-[12px] leading-4 tabular-nums text-muted">{s.done ? (s.at ? formatDate(s.at, { short: true }) : "Done") : "Not on record"}</span>
              </span>
            </span>
            {i < steps.length - 1 && <span className="hidden h-px w-6 bg-border-strong sm:block" aria-hidden />}
          </li>
        ))}
      </ol>
    </section>
  );
}

/** The margin and its why: an order's own margin, verdict and why first (its line breakdown
 * below), or why it has none ("No valid cost in BC"); a customer's profitability; a customer
 * whose figures come from Sage says they are before credit notes, with no margin. */
function MarginCard({ p, order, objectId, revenue }: { p: Profitability | null | undefined; order: boolean; objectId: string; revenue: CustomerAmount[] }) {
  const own = order && p?.breakdown && p.breakdown.order.object_id === objectId ? p.breakdown : null;
  const ownRow = order && !own ? (p?.orders ?? []).find((o) => o.order.object_id === objectId && !o.margin) : undefined;
  if (!p && !revenue.some((r) => r.system === "Sage")) return null;
  return (
    <section data-testid="margin">
      <SectionTitle aside={p && <span className="text-[12px] text-muted">{p.margin_basis ?? "on item cost only"}</span>}>{order ? "Margin" : "Profitability"}</SectionTitle>
      <div className="space-y-6">
        {!p ? (
          <SageMargin amounts={revenue} />
        ) : own ? (
          <>
            <MarginHeadline b={own} />
            <CostBreakdown breakdown={own} />
            <p className="text-[12px] leading-4 text-muted">
              Margins are {p.margin_basis ?? "on item cost only"}. {p.margin_note}
            </p>
          </>
        ) : ownRow ? (
          <NoMargin order={ownRow} />
        ) : (
          <ProfitabilityView p={p} />
        )}
      </div>
    </section>
  );
}

const SECTION_WORDS: Record<SectionName, string> = { late: "late lines", people: "people", landed_cost: "landed cost", trail: "evidence", margin: "margin" };

/** "lateness, people and how we know": the sections named in reading order. */
export function sectionWords(names: ReadonlySet<SectionName>): string {
  const words = PAGE_SECTIONS.filter((n) => names.has(n)).map((n) => SECTION_WORDS[n]);
  return words.length < 2 ? (words[0] ?? "") : `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}

/**
 * A section while it is read (its own call after the quick first read): a few quiet bars in
 * its place, named for screen readers, until it lands.
 */
function SectionSkeleton({ name, title, lines = 0 }: { name: SectionName; title: string; lines?: number }) {
  return (
    <div data-testid={`section-loading-${name}`}>
      <p className="sr-only">Checking {title.toLowerCase()}</p>
      {lines > 0 && (
        <>
          <SectionTitle>{title}</SectionTitle>
          <Bars n={lines} className="max-w-lg" />
        </>
      )}
    </div>
  );
}

/** Every source the record's facts cite, once each. */
function factSources(obj: ObjectPage): Source[] {
  const seen = new Set<string>();
  const out: Source[] = [];
  for (const s of (obj.facts ?? []).flatMap((f) => f.sources ?? [])) {
    const key = s.handle ?? s.open_url ?? `${s.system}${s.label}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(s);
  }
  return out;
}

/** "2 customers share this name. Showing the Portal record." with a link to each other one. */
function SameName({ obj }: { obj: ObjectPage }) {
  const others = obj.customer?.same_name ?? [];
  if (others.length === 0) return null;
  // the system the record's own id names ("Customer:portal.app_customer:…")
  const binding = obj.object_id.split(":")[1] ?? "";
  const system = binding.startsWith("bc.") ? SYSTEM_LABEL.BC : binding.startsWith("portal.") ? SYSTEM_LABEL.Portal : null;
  return (
    <p className="mt-1.5 text-[12px] leading-4 text-muted" data-testid="same-name">
      {others.length + 1} customers share this name.{system ? ` Showing the ${system} record.` : ""}{" "}
      {others.map((r, i) => (
        <span key={r.object_id}>
          {i > 0 ? " · " : ""}
          <Link href={refHref(r)} className="text-fg underline decoration-border-strong underline-offset-2 hover:decoration-fg">
            {r.label}
          </Link>
        </span>
      ))}
    </p>
  );
}

/** A sales order's own figures, read from its facts: total, ordered, requested. */
function salesFigures(obj: ObjectPage) {
  const f = factMap(obj.facts);
  const get = (...keys: string[]) => keys.map((k) => f.get(k)?.value).find((v) => v && v.trim());
  const currency = get("currency", "currency_code");
  const total = moneyText(get("total_amount_including_tax", "total_amount_excluding_tax", "total_amount", "amount_including_vat", "amount", "total"), currency);
  return {
    total,
    ordered: dayOf(get("order_date", "document_date")),
    needed: dayOf(get("requested_delivery_date", "promised_delivery_date", "shipment_date", "requested_date")),
  };
}

/**
 * A record's page. `missing`: shown instead of the not-found notice when the record
 * resolves to nothing for this reader (a friendly URL's chooser).
 */
export function ObjectView({ type, id, missing }: { type: string; id: string; missing?: ReactNode }) {
  const params = useSearchParams();
  const routeLabel = params?.get("label");
  const [obj, setObj] = useState<ObjectPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [busy, setBusy] = useState(false);
  const [conflictsOpen, setConflictsOpen] = useState(false);
  // The sections still being read, and those that could not be read.
  const [pending, setPending] = useState<ReadonlySet<SectionName>>(new Set());
  const [sectionStale, setSectionStale] = useState<string | null>(null);
  const [failed, setFailed] = useState<ReadonlySet<SectionName>>(new Set());
  // the proactive engine's card for this record, when it drafted one, and its action's state
  const [card, setCard] = useState<ProactiveSuggestion | null>(null);
  const [cardState, setCardState] = useState<ActionResult | null>(null);
  const saved = useSaved();
  const load = useCallback(() => {
    // The quick first read (the record, its timeline and links) shows at once; each slow
    // section (lateness, people, landed cost, how we know, margin) is its own call after it, a
    // skeleton in its place until it lands.
    const c = new AbortController();
    setError(null);
    setNotFound(false);
    setObj(null);
    setPending(new Set());
    setFailed(new Set());
    setSectionStale(null);
    getObject(type, id, c.signal, true)
      .then((first) => {
        if (c.signal.aborted) return;
        setObj(first);
        // a page read whole (kept with every section) asks for nothing more; else each
        // section is asked for once the quick read names the record (the server started
        // them beside the quick read)
        if (first.complete !== false) return;
        // the margin is a customer's or a sales order's only
        const names = PAGE_SECTIONS.filter((n) => n !== "margin" || first.type === "Customer" || first.type === "SalesOrder");
        setPending(new Set(names));
        for (const name of names) {
          getObjectSection(name, first.type, first.object_id, c.signal)
            .then((sec) => {
if (c.signal.aborted) return;
              if (sec.stale_since) setSectionStale((t) => (!t || sec.stale_since! < t ? sec.stale_since! : t));
              setObj((o) => (o ? mergeSection(o, sec) : o));
            })
            .catch(() => {
              if (!c.signal.aborted) setFailed((f) => new Set(f).add(name));
            })
            .finally(() => {
              if (c.signal.aborted) return;
              setPending((p) => {
                const n = new Set(p);
                n.delete(name);
                return n;
              });
            });
        }
      })
      .catch((e) => {
        if (c.signal.aborted) return;
        if (e instanceof ApiError && e.status === 404) setNotFound(true);
        setBusy(noticeKind(e) === "busy");
        const msg = plainError(e);
        setError(/took too long|^Timed out/.test(msg) ? `This ${typeLabel(type).toLowerCase()} timed out. Try again in a minute.` : msg);
      });
    return () => c.abort();
  }, [type, id]);
  useEffect(() => load(), [load]);

  // the card for this record (an order or a customer), read beside the record
  const objectId = obj?.object_id;
  const objType = obj?.type;
  useEffect(() => {
    setCard(null);
    setCardState(null);
    if (!objectId || !/^(PurchaseOrder|SalesOrder|Customer)$/.test(objType ?? "")) return;
    const c = new AbortController();
    // the cards and their action states are read at once, not one after the other
    const states = getActionStates(c.signal);
    states.catch(() => undefined);
    getSuggestions(c.signal)
      .then((s) => {
        if (c.signal.aborted || s?.status !== "read") return;
        const mine = (s.items ?? []).find((x) => x.subject?.object_id === objectId) ?? null;
        setCard(mine);
        if (mine)
          states
            .then((r) => !c.signal.aborted && setCardState(r.cards?.[mine.id] ?? null))
            .catch(() => undefined);
      })
      .catch(() => undefined);
    return () => c.abort();
  }, [objectId, objType]);

  // "Chase →" lands on the card
  useEffect(() => {
    if (obj && params?.get("focus") === "chase") document.getElementById("chase")?.scrollIntoView({ block: "start" });
  }, [obj, params]);

  const key = `o:${type}/${id}`;
  const isSaved = saved.some((s) => s.key === key);

  if (error && !obj && notFound && missing)
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <Toolbar title={<Crumb type={type} />} />
        <div className="px-5 pt-6 pb-12 sm:px-8 sm:pt-7">{missing}</div>
      </div>
    );
  if (error && !obj)
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <Toolbar title={<Crumb type={type} title={routeLabel ?? (id.includes(":") ? null : recordLabel(type, id))} />} />
        <div className="max-w-2xl px-5 pt-6 pb-12 sm:px-8 sm:pt-7">
          <LoadProblem message={error} busy={busy} onRetry={load} />
        </div>
      </div>
    );
  if (!obj)
    // a friendly path's reference ("106001", "DIVE11") names the record while it loads
    return <ObjectSkeleton type={type} label={routeLabel ?? (id.includes(":") ? null : recordLabel(type, id))} />;

  const head = recordHead(obj);
  // an order's life drawn as one line (PO, sales order, assembly); none for a customer or supplier
  const custody = /^(PurchaseOrder|SalesOrder|AssemblyOrder|ManufacturingOrder)$/.test(obj.type) ? custodyOf(obj) : null;
  const isPo = obj.type === "PurchaseOrder";
  const isCustomer = obj.type === "Customer";
  const isSalesOrder = obj.type === "SalesOrder";
  const isSupplier = obj.type === "Supplier" || obj.type === "Vendor";
  const isAo = obj.type === "AssemblyOrder" || obj.type === "ManufacturingOrder";
  const isParty = isCustomer || isSupplier;
  const conflicts = obj.conflicts ?? [];
  const salesOrders = (obj.related ?? []).filter((r) => r.type === "SalesOrder");
  const customerRef = isSalesOrder ? ((obj.related ?? []).find((r) => r.type === "Customer") ?? null) : null;
  const counterpart = head.counterpart ?? customerRef;
  const otherRelated = (obj.related ?? []).filter(
    (r) => !(isCustomer && r.type === "SalesOrder") && !(isSupplier && r.type === "PurchaseOrder") && r.object_id !== (counterpart as ObjectRef | null)?.object_id,
  );
  const lines = isSupplier ? [] : (obj.late_lines ?? []).filter((l) => !/^PO number:/i.test(l.line));
  const sources = factSources(obj);
  const generic = !isPo && !isParty && !isAo && !isSalesOrder;
  // a minor record (an order line, a product) shows its numbers as figures, the rest as details
  const numeric = (v: string) => /^-?\$?[\d,]+(?:\.\d+)?(?:\s[A-Z]{3})?$/.test(v.trim());
  const factFigures = generic ? head.keyFacts.filter((k) => k.value && numeric(k.value)).slice(0, 5) : [];
  const facts = head.keyFacts.filter((k) => k.value && !factFigures.includes(k));
  const trail = obj.trail?.trails?.[0];
  const trailPending = pending.has("trail");

  // the header's status line: each status its shape and words in ink
  const badges: Shown[] = head.badges.slice(0, 2);
  // a record with one status and a sentence says the sentence beside the status's shape
  const merged = !isPo && !isParty && !isAo && !!head.lead && badges.length === 1 && !head.leadStatus ? { status: badges[0].status, label: head.lead } : null;
  const leadShown = !merged && head.lead && (head.leadStatus === "suspect" || (badges.length === 0 && head.stats.length === 0)) && !badges.some((b) => b.label.trim().toLowerCase() === head.lead!.trim().toLowerCase());

  // the figures: a purchase order's total and receipt over its order line; a sales order's
  // total and dates; every other record its head's figures
  const f = factMap(obj.facts);
  const got = isPo ? receivedOf(trail) : null;
  const total = head.stats.find((s) => s.label === "Total");
  const so = isSalesOrder ? salesFigures(obj) : null;
  const ordered = isPo ? dayOf(f.get("order_date")?.value ?? f.get("document_date")?.value) : (so?.ordered ?? null);
  const neededIso = isPo ? neededOf(obj) : null;
  const needed = isPo ? dayOf(neededIso) : (so?.needed ?? null);
  // a supplier whose lateness was not established is not said to have none late
  const unknownLate = isSupplier && obj.status?.state === "not_established" && !(obj.late_lines ?? []).length;
  const otherStats = isPo ? head.stats.filter((s) => !/^(Total|Received|Needed by|Ordered)$/.test(s.label)) : head.stats.filter((s) => !(unknownLate && s.label === "Late"));
  const revenue = isCustomer ? (obj.customer?.revenue ?? []) : [];

  // a supplier is known by its code under its name
  const subline = head.subline ?? (isSupplier ? (head.keyFacts.find((k) => k.label === "Code")?.value || null) : null);
  const askQ = isAo ? `Why did ${head.askLabel} fall short?` : `What's the latest on ${head.askLabel}?`;
  const actions = (
    <>
      <ToolbarButton aria-pressed={isSaved} onClick={() => toggleSaved({ key, kind: "object", label: head.askLabel, href: objectHref(type, id) })} className={cn(isSaved && "text-fg")} title={isSaved ? "Saved" : "Save"}>
        <Bookmark className={cn(isSaved && "fill-current")} strokeWidth={2} aria-hidden />
        <span className="max-sm:sr-only">{isSaved ? "Saved" : "Save"}</span>
      </ToolbarButton>
      <Link
        href={`/ask?q=${encodeURIComponent(askQ)}&object=${encodeURIComponent(obj.object_id)}`}
        className="ml-1 inline-flex h-8 items-center gap-1.5 rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-3 text-[13px] font-medium text-fg hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)] [&_svg]:size-[15px]"
        title={isAo ? "Ask why it fell short" : `Ask about ${head.askLabel}`}
      >
        <MessageCircle strokeWidth={2} aria-hidden />
        <span className="max-sm:sr-only">{isAo ? "Ask why" : "Ask"}</span>
      </Link>
    </>
  );

  const header = (
    <>
    <StaleNote since={[obj.stale_since, sectionStale].filter((t): t is string => !!t).sort()[0]} className="mb-3" />
    <header className="flex gap-4 sm:gap-5">
      <Monogram name={isParty ? obj.label : (counterpart?.label ?? null)} size={isParty ? 56 : 48} />
      <div className="min-w-0 flex-1">
        <h1 className="flex flex-wrap items-baseline gap-x-2.5 break-words" data-testid="object-title">
          <span className={cn("font-semibold tracking-[-0.025em]", isParty ? "text-[26px] leading-[32px]" : "text-[24px] leading-[30px]")}>{head.title}</span>
          {counterpart && (
            <span className="text-[17px] leading-[22px] font-normal tracking-[-0.01em] text-muted">
              <span className="sr-only">· </span>
              {"object_id" in counterpart && counterpart.object_id ? (
                <Link href={refHref(counterpart as ObjectRef)} className="hover:text-fg">
                  {counterpart.label}
                </Link>
              ) : (
                counterpart.label
              )}
            </span>
          )}
        </h1>
        {(subline || (isCustomer && (obj.customer?.also_in ?? []).length > 0)) && (
          <p className="mt-0.5 text-[13px] leading-5 text-muted">
            {subline}
            {isCustomer &&
              (obj.customer?.also_in ?? []).map((r) => (
                <span key={r.object_id}>
                  {subline ? " · " : ""}also recorded as{" "}
                  <Link href={refHref(r)} className="text-fg underline decoration-border-strong underline-offset-2 hover:decoration-fg">
                    {r.label}
                  </Link>
                </span>
              ))}
          </p>
        )}
        {merged ? (
          <p className="mt-2 flex max-w-[72ch] items-start gap-2 text-[13px] leading-5" data-testid="object-status">
            <Glyph status={merged.status} days={null} size={14} />
            <span className="-mt-[3px]">{merged.label}</span>
          </p>
        ) : (badges.length > 0 || (isCustomer && conflicts.length > 0)) && (
          <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-[13px] leading-5" data-testid="object-status">
            {badges.map((b) => (
              <Fact key={b.label} shown={b} />
            ))}
            {isCustomer && <ConflictsToggle conflicts={conflicts} open={conflictsOpen} onToggle={() => setConflictsOpen((v) => !v)} />}
          </div>
        )}
        {leadShown && (
          <p className="mt-2 flex max-w-[72ch] items-start gap-2 text-[13px] leading-5" data-testid="object-lead">
            {head.leadStatus && <StatusGlyph status={head.leadStatus} size={14} className="mt-[3px]" />}
            <span className={cn(!head.leadStatus && "text-muted")}>{head.lead}</span>
          </p>
        )}
        {isCustomer && <SameName obj={obj} />}
        {pending.size > 0 && (
          <p className="mt-2 inline-flex items-center gap-1.5 text-[12px] leading-4 text-muted" role="status" data-testid="checking" data-pending="">
            <span className="size-1.5 animate-breathe rounded-full bg-[var(--dot-neutral)]" aria-hidden />
            Loading {sectionWords(pending)}…
          </p>
        )}
        {pending.size === 0 && failed.size > 0 && (
          <p className="mt-2 text-[12px] leading-4 text-muted" role="status" data-testid="checks-failed">
            Couldn&apos;t load {sectionWords(failed)}.{" "}
            <button type="button" onClick={load} className="font-medium text-fg underline decoration-border-strong underline-offset-2 cursor-pointer">
              Try again
            </button>
          </p>
        )}
      </div>
    </header>
    </>
  );

  const hasFigures = factFigures.length > 0 || !!total || !!got || ordered != null || needed != null || !!so?.total || otherStats.length > 0 || revenue.length > 0;
  const figures = hasFigures && (
    <section className="space-y-6" data-testid="detail-stats">
      <div className="flex flex-wrap gap-x-12 gap-y-5">
        {(isPo ? total : null) && <Figure label="Total" value={total!.value} unit={total!.caption} />}
        {so?.total && <Figure label="Total" value={so.total.replace(/\s+[A-Z]{3}$/, "")} unit={/[A-Z]{3}$/.exec(so.total)?.[0]} />}
        {so && so.needed == null && so.ordered != null && <Figure label="Ordered" value={formatDate(new Date(so.ordered).toISOString().slice(0, 10))} />}
        {got && (
          <Figure label="Received" value={got.lines ? got.got : `${got.got} of ${got.of}`} unit={got.lines ? `${got.lines} lines short` : got.uom}>
            {got.pct != null && <Progress pct={got.pct} />}
          </Figure>
        )}
        {isPo && trailPending && !got && (
          <div className="space-y-2">
            <div className="skeleton h-3 w-14" />
            <div className="skeleton h-6 w-24" />
          </div>
        )}
        {factFigures.map((k) => (
          <Figure key={k.label} label={k.label} value={grouped(k.value.replace(/\s+[A-Z]{3}$/, ""))} unit={/\s([A-Z]{3})$/.exec(k.value)?.[1]} />
        ))}
        {otherStats.map((s) => {
          const m = /^(.*?)\s+([A-Z]{3})$/.exec(s.value);
          return <Figure key={s.label} label={s.label} value={m ? m[1] : s.value} unit={m ? m[2] : undefined} status={s.status} days={s.status === "late" ? 90 : null} source={s.source}>{s.caption && <p className="mt-0.5 max-w-[220px] truncate text-[12px] leading-4 text-muted">{s.caption}</p>}</Figure>;
        })}
        {revenue.map((r, i) => (
          <div key={i} data-testid={i === 0 ? "revenue" : undefined}>
            <Figure label={r.label} value={money(r.money).replace(/\s+[A-Z]{3}$/, "")} unit={r.money?.currency}>
              <p className="mt-0.5 max-w-[280px] truncate text-[12px] leading-4 text-muted">{readable(r.period)}</p>
            </Figure>
          </div>
        ))}
      </div>
      {((isPo && (ordered != null || needed != null) && !trailPending) || (isSalesOrder && needed != null)) && (
        <div>
          <OrderLine ordered={ordered} needed={needed} daysLate={isPo && trail?.state === "late" ? trail.days_late : null} />
        </div>
      )}
      {isPo && trailPending && <div className="skeleton h-[6px] rounded-full" />}
    </section>
  );

  const notOnRecord = (obj.not_found ?? []).map((n) => {
    const t = missingText(n, obj.label);
    return t ? t.charAt(0).toUpperCase() + t.slice(1) : t;
  });

  const main = (
    <>
      {header}
      {isCustomer && conflictsOpen && <ConflictRows conflicts={conflicts} />}
      {figures}
      {(isPo || isAo || isSalesOrder || isCustomer || (obj.proposals ?? []).length > 0) && (
        <NextStep obj={obj} head={head} card={card} cardState={cardState} onDone={setCardState} pending={trailPending && (isPo || isAo)} />
      )}
      {!isCustomer && conflicts.length > 0 && (
        <div className="space-y-3">
          {conflicts.map((c, i) => (
            <ConflictCard key={i} conflict={c} />
          ))}
        </div>
      )}
      {isPo && (trailPending ? <SectionSkeleton name="trail" title="Supplier confirmation" lines={0} /> : <EtaCard eta={trail?.eta} />)}
      {isAo && <AoSteps obj={obj} />}
      {isSupplier && <SupplierOrders obj={obj} />}
      {obj.customer && <CustomerOrders c={obj.customer} fallbackOrders={salesOrders} />}
      {(isCustomer || isSalesOrder) &&
        (pending.has("margin") ? <SectionSkeleton name="margin" title={isCustomer ? "Profitability" : "Margin"} lines={3} /> : <MarginCard p={obj.profitability} order={isSalesOrder} objectId={obj.object_id} revenue={obj.customer?.revenue ?? []} />)}
      {isPo && pending.has("late") ? <SectionSkeleton name="late" title="Lines" lines={2} /> : <LinesTable lines={lines} />}
      {isPo && trailPending ? (
        <SectionSkeleton name="trail" title="How we know" lines={3} />
      ) : (
        <div>
          <EvidenceTrailCard trail={obj.trail} inPage showNext={!(isPo || isAo)} showEta={!isPo} />
        </div>
      )}
      {custody && (
        <div>
          <CustodyRail custody={custody} />
        </div>
      )}
      {pending.has("landed_cost") ? (
        isPo && <SectionSkeleton name="landed_cost" title="Landed cost" lines={0} />
      ) : (
        obj.landed_cost && obj.landed_cost.status !== "not_established" && <LandedCostCard lc={obj.landed_cost} />
      )}
      <Linked items={obj.linked ?? []} />
      {notOnRecord.length > 0 && (
        <section data-testid="not-found">
          <SectionTitle>Not on record</SectionTitle>
          <Missing items={notOnRecord} />
        </section>
      )}
      <HiddenNotice hidden={obj.hidden} />
    </>
  );

  const inspector = (
    <>
      {facts.length > 0 && (
      <section data-testid="summary">
        <SectionTitle>Details</SectionTitle>
          <dl>
            {facts.map((k) => (
              <Property key={k.label} label={k.label}>
                {k.status && k.status !== "neutral" ? (
                  <span>
                    <StatusGlyph status={k.status} size={12} className="mr-1.5 inline-block align-[-1px]" />
                    {k.value.replace(/\s*⚠$/, "")}
                  </span>
                ) : k.label === "Supplier" && counterpart && "object_id" in counterpart && counterpart.object_id ? (
                  <Link href={refHref(counterpart as ObjectRef)} className="hover:underline">
                    {k.value}
                  </Link>
                ) : (
                  k.value
                )}
              </Property>
            ))}
          </dl>
      </section>
      )}
      {pending.has("people") ? <SectionSkeleton name="people" title="People" lines={2} /> : <People people={obj.people ?? []} />}
      <RelatedCard refs={otherRelated} />
      {sources.length > 0 && (
        <section>
          <SectionTitle>Sources</SectionTitle>
          <ul className="space-y-2">
            {sources.slice(0, 5).map((src, i) => (
              <li key={i} className="flex items-center gap-2.5 text-[13px]">
                <SystemTile system={src.system} size={18} />
                <span className="min-w-0 truncate">
                  <SourceChip source={src} record={head.title} />
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      <Activity events={obj.timeline ?? []} />
      <div className="text-[12px]">
        <FreshnessLabel freshness={obj.freshness} />
      </div>
    </>
  );

  return <Frame crumb={<Crumb type={obj.type} title={head.title} />} actions={actions} main={main} inspector={inspector} />;
}

/** "2505" -> "2,505"; anything that is not a bare number as given. */
function grouped(v: string): string {
  const t = v.trim();
  if (!/^-?\d+(?:\.\d+)?$/.test(t)) return v;
  return Number(t).toLocaleString("en-CA", { maximumFractionDigits: 4 });
}

/** The needed-by date as the trail states it (its due-date step's, else its verdict's). */
function neededOf(obj: ObjectPage): string | null {
  const t = obj.trail?.trails?.[0];
  for (const s of t?.steps ?? []) {
    if (s.kind === "due_date") {
      const m = /(\d{4}-\d{2}-\d{2})/.exec(s.title);
      if (m) return m[1];
    }
  }
  const m = /needed-by date (\d{4}-\d{2}-\d{2})|requested-by date (\d{4}-\d{2}-\d{2})/.exec(t?.verdict ?? "");
  return m ? (m[1] ?? m[2]) : null;
}
