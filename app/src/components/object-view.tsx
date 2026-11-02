"use client";

import { ArrowRight, Bookmark, BookmarkCheck, Check, CheckSquare, ChevronDown, Database, FileText, Loader2, Mail, MessageSquare, MessagesSquare } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { getObject } from "@/lib/api";
import { cn } from "@/lib/cn";
import { ApiError, plainError } from "@/lib/errors";
import { formatDate, money, objectHref, plural, readable, recordLabel, safeHref, SYSTEM_LABEL, typeLabel } from "@/lib/format";
import { groupLinked, groupRelated, groupTimeline, productionSteps, recordHead, type RecordHead, type StatSpec, type Thread } from "@/lib/record";
import { lateness, recordState, STATUS_CLASS } from "@/lib/status";
import { toggleSaved, useSaved } from "@/lib/saved";
import type { Conflict, CustomerSummary, LandedCost, LateLine, LinkedItem, ObjectPage, ObjectRef, OrderRow, Person, Source, TimelineEvent, Tone } from "@/lib/types";
import { conflictSentence, ConflictCard, FreshnessLabel, HiddenNotice, NotFoundSection, SourceLine } from "./answer-parts";
import { EvidenceTrailCard, trailNextAction } from "./evidence-trail";
import { EvidenceDrawer, SourceChips } from "./source-chip";
import { Card, CardBody, CardHeader, CardTitle } from "./ui/card";
import { KeyFacts } from "./ui/key-facts";
import { ErrorNotice } from "./ui/notice";
import { Skeleton, SkeletonStats } from "./ui/skeleton";
import { Stat, StatRow } from "./ui/stat";
import { StatusBadge } from "./ui/status-badge";

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

/** The page's shape while it gathers: the header from the link's label at once, then a step line. */
function ObjectSkeleton({ type, label }: { type: string; label?: string | null }) {
  const t = useElapsed(true);
  const step = t < 8 ? "Reading Business Central…" : t < 20 ? "Checking the supplier's mail and files…" : "Building the trail…";
  return (
    <div className="space-y-6" role="status" aria-label="Loading">
      <div className="space-y-2">
        <p className="text-eyebrow uppercase text-muted">{typeLabel(type)}</p>
        {label ? <h1 className="text-title">{label}</h1> : <Skeleton className="h-8 w-2/3" />}
        {t >= 3 && <p className="text-meta text-muted">{step}</p>}
        {t >= 20 && <p className="text-meta text-muted">This record takes up to a minute to gather while we check mail and files. You can leave this page; it keeps loading.</p>}
      </div>
      <SkeletonStats />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Skeleton className="h-40 rounded-xl lg:col-span-2" />
        <Skeleton className="h-40 rounded-xl" />
      </div>
    </div>
  );
}

function StatTile({ s }: { s: StatSpec }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Stat label={s.label} value={s.value} caption={s.caption} status={s.status} onClick={s.source ? () => setOpen(true) : undefined} />
      {open && s.source && <EvidenceDrawer source={s.source} open={open} onOpenChange={setOpen} />}
    </>
  );
}

/** Lines of the order, late first. A line the API sends without an item reads "Line n", never the PO's own label. */
export function LinesTable({ lines, title = "Lines" }: { lines: LateLine[]; title?: string }) {
  if (lines.length === 0) return null;
  const sorted = [...lines].sort((a, b) => (b.days_late ?? -1) - (a.days_late ?? -1));
  const name = (l: LateLine, i: number) => (l.item ? l.item : /^\d+$/.test(l.line.trim()) ? `Line ${l.line.trim()}` : `Line ${i + 1}`);
  return (
    <Card data-testid="late-lines">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <span>{plural(lines.length, "line")}</span>
      </CardHeader>
      <CardBody className="px-0 sm:px-0">
        <table className="hidden w-full text-row sm:table">
          <caption className="sr-only">{title}, late first</caption>
          <thead>
            <tr className="h-9 border-b border-border text-left text-label text-muted">
              <th scope="col" className="px-5">Item</th>
              <th scope="col" className="text-right">Ordered</th>
              <th scope="col" className="pl-6">Needed by</th>
              <th scope="col" className="px-5 text-right">Status</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((l, i) => {
              const st = lateness(typeof l.days_late === "number" && l.days_late > 0 ? "late" : l.days_late === 0 ? "on_time" : null, l.days_late);
              return (
                <tr key={i} className="h-11 border-b border-border last:border-0">
                  <td className="px-5 font-medium">
                    {name(l, i)} {l.sources && l.sources.length > 0 && <SourceChips sources={l.sources} max={1} />}
                  </td>
                  <td className="whitespace-nowrap text-right tabular-nums">{l.quantity ?? "—"}</td>
                  <td className="whitespace-nowrap pl-6 tabular-nums">{l.due_date ? formatDate(l.due_date) : "—"}</td>
                  <td className="px-5 text-right">{st ? <StatusBadge shown={st} /> : <span className="text-muted">—</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <ul className="divide-y divide-border sm:hidden">
          {sorted.map((l, i) => {
            const st = lateness(typeof l.days_late === "number" && l.days_late > 0 ? "late" : null, l.days_late);
            return (
              <li key={i} className="px-4 py-3">
                <div className="flex items-center justify-between gap-2 text-row font-medium">
                  <span className="min-w-0 truncate">{name(l, i)}</span>
                  {st && <StatusBadge shown={st} />}
                </div>
                <p className="text-meta text-muted">{[l.quantity ? `Ordered ${l.quantity}` : null, l.due_date ? `Needed ${formatDate(l.due_date, { short: true })}` : null].filter(Boolean).join(" · ") || "No quantity or date on record"}</p>
              </li>
            );
          })}
        </ul>
      </CardBody>
    </Card>
  );
}

/** A supplier's purchase orders: late first (from the late rows), then the rest. */
function SupplierOrders({ obj }: { obj: ObjectPage }) {
  const [all, setAll] = useState(false);
  const pos = (obj.related ?? []).filter((r) => r.type === "PurchaseOrder");
  const lateLabels = new Map<string, LateLine>();
  for (const l of obj.late_lines ?? []) {
    const m = /PO number:\s*([\w-]+)/.exec(l.line);
    if (m) lateLabels.set(m[1], l);
  }
  const days = /Late by (\d+) days/i.exec(obj.status?.text ?? "")?.[1];
  const rows = pos.map((p) => ({ p, late: lateLabels.has(p.label) })).sort((a, b) => Number(b.late) - Number(a.late));
  if (rows.length === 0) return null;
  const shown = all ? rows : rows.slice(0, 8);
  return (
    <Card data-testid="supplier-orders">
      <CardHeader>
        <CardTitle>Purchase orders (late first)</CardTitle>
        <span>{rows.length}</span>
      </CardHeader>
      <CardBody className="px-0 sm:px-0">
        <ul className="divide-y divide-border">
          {shown.map(({ p, late }) => (
            <li key={p.object_id}>
              <Link href={refHref(p)} className="flex min-h-11 items-center gap-3 px-4 py-2 text-row hover:bg-surface-2 sm:px-5">
                <span className="min-w-0 flex-1 truncate font-medium">{recordLabel(p.type, p.label)}</span>
                {late && <StatusBadge status="late" label={days && lateLabels.size === 1 ? `${days} days late` : "Late"} />}
                <ArrowRight className="size-4 shrink-0 text-muted" aria-hidden />
              </Link>
            </li>
          ))}
        </ul>
        {rows.length > 8 && (
          <button type="button" onClick={() => setAll((v) => !v)} className="mx-5 mt-2 text-meta font-medium text-accent hover:underline cursor-pointer">
            {all ? "Show fewer" : `Show all ${rows.length}`}
          </button>
        )}
      </CardBody>
    </Card>
  );
}

function OrderTable({ title, rows, testId }: { title: string; rows: OrderRow[]; testId: string }) {
  if (rows.length === 0) return null;
  return (
    <div data-testid={testId}>
      <p className="mb-1 px-4 text-label uppercase text-muted sm:px-5">{title}</p>
      <ul className="divide-y divide-border">
        {rows.map((r) => (
          <li key={`${r.object_id}-${r.label}`} className="flex min-h-11 flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-row sm:px-5">
            <Link href={safeHref(r.open_url) ?? objectHref("SalesOrder", r.object_id)} className="font-medium hover:underline">
              {r.label}
            </Link>
            {r.date && <span className="tabular-nums text-muted">{formatDate(r.date)}</span>}
            {r.status && <StatusBadge status={recordState(r.status)} label={r.status} />}
            {r.amount && <span className="ml-auto tabular-nums">{money(r.amount)}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** A customer's revenue per period and currency, its open and recent orders, contacts and other records. */
function CustomerCard({ c, fallbackOrders }: { c: CustomerSummary; fallbackOrders: ObjectRef[] }) {
  const orders = (c.open_list ?? []).length + (c.recent_orders ?? []).length;
  return (
    <Card data-testid="customer">
      <CardHeader>
        <CardTitle>Orders</CardTitle>
        {typeof c.orders_total === "number" ? (
          <span data-testid="orders-total">{plural(c.orders_total, "order")}</span>
        ) : typeof c.orders_at_least === "number" ? (
          <span data-testid="orders-total">{c.orders_at_least}+ orders</span>
        ) : null}
      </CardHeader>
      <CardBody className="space-y-4 px-0 sm:px-0">
        {(c.revenue ?? []).length > 0 && (
          <div className="grid grid-cols-1 gap-3 px-4 sm:grid-cols-2 sm:px-5" data-testid="revenue">
            {c.revenue!.map((r, i) => (
              <div key={i} className="rounded-lg border border-border p-3">
                <p className="text-meta text-muted">
                  {r.label} · {readable(r.period)}
                </p>
                <p className="text-section tabular-nums">{money(r.money)}</p>
                <p className="text-meta text-muted">
                  {plural(r.count, "record")} in {SYSTEM_LABEL[r.system] ?? r.system} <SourceChips sources={r.sources} max={1} />
                </p>
              </div>
            ))}
          </div>
        )}
        <OrderTable title="Open orders" rows={c.open_list ?? []} testId="open-orders" />
        <OrderTable title="Recent orders" rows={c.recent_orders ?? []} testId="recent-orders" />
        {orders === 0 && fallbackOrders.length > 0 && (
          <ul className="divide-y divide-border">
            {fallbackOrders.map((o) => (
              <li key={o.object_id}>
                <Link href={refHref(o)} className="flex min-h-11 items-center px-4 py-2 text-row font-medium hover:bg-surface-2 sm:px-5">
                  {o.label}
                </Link>
              </li>
            ))}
          </ul>
        )}
        {(c.not_established ?? []).length > 0 && (
          <ul className="space-y-0.5 px-4 text-meta text-muted sm:px-5">
            {c.not_established!.map((n, i) => (
              <li key={i}>{readable(n)}</li>
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}

/** Activity by month, repeats collapsed, ten rows then "Show all". */
function Activity({ events }: { events: TimelineEvent[] }) {
  const [all, setAll] = useState(false);
  if (events.length === 0) return null;
  const months = groupTimeline(events);
  let budget = all ? Infinity : 10;
  return (
    <Card data-testid="timeline">
      <CardHeader>
        <CardTitle>Activity</CardTitle>
        <span>{plural(events.length, "event")}</span>
      </CardHeader>
      <CardBody className="space-y-4">
        {months.map((m) => {
          if (budget <= 0) return null;
          const rows = m.rows.slice(0, budget);
          budget -= rows.length;
          return (
            <div key={m.month}>
              <h3 className="mb-1 text-label uppercase text-muted">{m.month}</h3>
              <ul className="space-y-1">
                {rows.map(({ event: e, count }, i) => (
                  <ActivityRow key={i} e={e} count={count} />
                ))}
              </ul>
            </div>
          );
        })}
        {months.reduce((n, m) => n + m.rows.length, 0) > 10 && (
          <button type="button" onClick={() => setAll((v) => !v)} className="text-meta font-medium text-accent hover:underline cursor-pointer">
            {all ? "Show less" : "Show all activity"}
          </button>
        )}
      </CardBody>
    </Card>
  );
}

function ActivityRow({ e, count }: { e: TimelineEvent; count: number }) {
  const [open, setOpen] = useState(false);
  const title = count > 1 ? `${/^([^:]+):/.exec(e.title)?.[1] ?? readable(e.title)} ×${count}` : readable(e.title);
  const body = (
    <>
      <span className="w-14 shrink-0 text-meta tabular-nums text-muted">{e.at ? formatDate(e.at, { short: true }).replace(/ \d{4}$/, "") : ""}</span>
      <span className="min-w-0 flex-1">
        <span className="text-row">{title}</span>
        <span className="ml-2 text-meta text-muted">{SYSTEM_LABEL[e.system] ?? e.system}</span>
        {e.summary && count === 1 && <span className="block text-meta text-muted">{readable(e.summary)}</span>}
      </span>
    </>
  );
  return (
    <li>
      {e.source && count === 1 ? (
        <>
          <button type="button" onClick={() => setOpen(true)} className="-mx-2 flex w-[calc(100%+1rem)] items-baseline gap-3 rounded-lg px-2 py-1 text-left hover:bg-surface-2 cursor-pointer">
            {body}
          </button>
          {open && <EvidenceDrawer source={e.source} open={open} onOpenChange={setOpen} />}
        </>
      ) : (
        <div className="flex items-baseline gap-3 py-1">{body}</div>
      )}
    </li>
  );
}

const LINK_ICON = { email: Mail, teams: MessagesSquare, document: FileText, record: Database, task: CheckSquare } as const;

function ThreadRow({ t }: { t: Thread }) {
  const [open, setOpen] = useState<LinkedItem | null>(null);
  const [expanded, setExpanded] = useState(false);
  const Icon = LINK_ICON[t.kind] ?? MessageSquare;
  const first = t.items[0];
  return (
    <li className="py-2">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-muted">
          <Icon className="size-4" aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <button type="button" onClick={() => (t.count > 1 ? setExpanded((v) => !v) : setOpen(first))} className="text-left text-row font-medium hover:underline cursor-pointer" aria-expanded={t.count > 1 ? expanded : undefined}>
            <span className="line-clamp-2">{/^email message$/i.test(t.subject) ? "Email (no subject captured)" : t.subject}</span>
          </button>
          <p className="text-meta text-muted">
            {[t.count > 1 ? `${t.count} messages` : null, t.latest ? `latest ${formatDate(t.latest, { short: true })}` : null].filter(Boolean).join(" · ")}
          </p>
          {expanded && (
            <ul className="mt-1 space-y-0.5 border-l border-border pl-3">
              {t.items.map((l, i) => (
                <li key={i}>
                  <button type="button" onClick={() => setOpen(l)} className="text-left text-meta hover:underline cursor-pointer">
                    {l.at ? formatDate(l.at, { short: true }) : "Undated"} · {l.label}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
      {open && <EvidenceDrawer source={open.source} open={!!open} onOpenChange={(o) => !o && setOpen(null)} />}
    </li>
  );
}

function Linked({ items }: { items: LinkedItem[] }) {
  if (items.length === 0) return null;
  const threads = groupLinked(items);
  return (
    <Card data-testid="linked">
      <CardHeader>
        <CardTitle>Messages and documents</CardTitle>
        <span>{items.length}</span>
      </CardHeader>
      <CardBody>
        <ul className="divide-y divide-border">
          {threads.map((t) => (
            <ThreadRow key={`${t.kind}-${t.subject}`} t={t} />
          ))}
        </ul>
      </CardBody>
    </Card>
  );
}

function People({ people }: { people: Person[] }) {
  if (people.length === 0) return null;
  return (
    <Card data-testid="people">
      <CardHeader>
        <CardTitle>People</CardTitle>
      </CardHeader>
      <CardBody>
        <ul className="space-y-3">
          {people.map((p, i) => (
            <li key={i} className="flex items-center gap-3">
              <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-accent-soft text-label text-accent" aria-hidden>
                {p.name
                  .split(/[\s@.]+/)
                  .map((x) => x[0])
                  .slice(0, 2)
                  .join("")
                  .toUpperCase()}
              </span>
              <span className="min-w-0">
                <span className="block truncate text-row font-medium">{p.name}</span>
                <span className="block truncate text-meta text-muted">{[p.role, p.organisation].filter(Boolean).join(" · ")}</span>
              </span>
            </li>
          ))}
        </ul>
      </CardBody>
    </Card>
  );
}

function RelatedCard({ refs }: { refs: ObjectRef[] }) {
  // a group of rows named only by their type ("PO line", "Receipt") says nothing: left out
  const groups = groupRelated(refs).filter((g) => g.items.length > 0);
  if (groups.length === 0) return null;
  return (
    <Card data-testid="related">
      <CardHeader>
        <CardTitle>Related</CardTitle>
      </CardHeader>
      <CardBody className="space-y-3">
        {groups.map((g) => (
          <div key={g.type}>
            <p className="text-label uppercase text-muted">
              {g.label} <span className="tabular-nums">{g.items.reduce((n, x) => n + x.times, 0) + g.unnamed}</span>
            </p>
            {g.items.length > 0 ? (
              <ul className="-mx-2">
                {g.items.slice(0, 6).map((r) => (
                  <li key={r.object_id}>
                    <Link href={refHref(r)} className="flex items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-row hover:bg-surface-2">
                      <span className="truncate">{recordLabel(r.type, readable(r.label))}</span>
                      {r.times > 1 && <span className="shrink-0 text-meta text-muted">×{r.times}</span>}
                    </Link>
                  </li>
                ))}
                {g.items.length > 6 && <li className="px-2 text-meta text-muted">and {g.items.length - 6} more</li>}
              </ul>
            ) : null}
          </div>
        ))}
      </CardBody>
    </Card>
  );
}

function LandedCostCard({ lc }: { lc: LandedCost }) {
  const lines = lc.lines ?? [];
  return (
    <Card data-testid="landed-cost">
      <CardHeader>
        <CardTitle>Landed cost</CardTitle>
        <StatusBadge status={lc.status === "computed" ? "ok" : "risk"} label={lc.status === "computed" ? "Complete" : "Partial"} />
      </CardHeader>
      <CardBody>
        <dl className="space-y-2 text-row">
          {lines.map((l, i) => (
            <div key={i} className="flex items-start justify-between gap-3">
              <dt className="min-w-0 text-muted">{l.label}</dt>
              <dd className="shrink-0 tabular-nums">{money(l.amount)}</dd>
            </div>
          ))}
          {lc.total ? (
            <div className="flex items-center justify-between gap-3 border-t border-border pt-2 font-semibold">
              <dt>Total</dt>
              <dd className="tabular-nums">{money(lc.total)}</dd>
            </div>
          ) : (
            <p className="border-t border-border pt-2 text-muted">Landed cost isn&apos;t known yet{(lc.missing ?? []).length ? `: ${lc.missing!.join("; ")}` : "."}</p>
          )}
          {lc.per_unit && (
            <div className="flex items-center justify-between gap-3 text-muted">
              <dt>Per unit</dt>
              <dd className="tabular-nums">{money(lc.per_unit)}</dd>
            </div>
          )}
        </dl>
      </CardBody>
    </Card>
  );
}

/** Conflicts collapsed into one violet badge; opened, one plain row each. */
function ConflictsBadge({ conflicts, open, onToggle }: { conflicts: Conflict[]; open: boolean; onToggle: () => void }) {
  if (conflicts.length === 0) return null;
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      className={cn("inline-flex h-[22px] items-center gap-1 rounded-md border px-2 text-label cursor-pointer", STATUS_CLASS.suspect.soft, STATUS_CLASS.suspect.fg, STATUS_CLASS.suspect.line)}
      data-testid="conflicts-badge"
    >
      {plural(conflicts.length, "data conflict")} <ChevronDown className={cn("size-3.5", open && "rotate-180")} aria-hidden />
    </button>
  );
}

function ConflictRows({ conflicts }: { conflicts: Conflict[] }) {
  return (
    <ul className="space-y-1.5 rounded-xl border border-suspect-line bg-suspect-soft/60 p-4 text-row" data-testid="conflict-rows">
      {conflicts.map((c, i) => (
        <li key={i} className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span>{conflictSentence(c)}</span>
          <SourceChips sources={c.values.map((v) => v.source)} />
          {c.values.some((v) => v.trusted) && <span className="text-meta text-muted">We use the {c.values.find((v) => v.trusted)!.source.system === "BC" ? "Business Central" : "AR"} record for this field.</span>}
        </li>
      ))}
    </ul>
  );
}

/** "Who to chase": the next step from the trail, with who, what, the last message and that nothing is sent. */
function WhoToChase({ obj, head }: { obj: ObjectPage; head: RecordHead }) {
  const next = trailNextAction(obj.trail);
  if (!next) return null;
  const who = next.contact?.replace(/\s*<[^>]+>/, "") ?? head.counterpart?.label ?? null;
  return (
    <section id="chase" className="scroll-mt-20 rounded-xl border border-border bg-surface p-4 sm:p-5" aria-label="Who to chase" data-testid="who-to-chase">
      <h2 className="text-section">Who to chase</h2>
      {who && (
        <p className="mt-2 text-row font-medium">
          {who}
          {!next.contact && <span className="font-normal text-muted"> · no contact on record</span>}
        </p>
      )}
      <p className="mt-1 text-row">{readable(next.text)}</p>
      {next.contact_source ? (
        <SourceLine source={next.contact_source} prefix="Last message from" />
      ) : (
        who && <p className="mt-1 text-meta text-muted">Last message from {who}: none found</p>
      )}
      <p className="mt-2 text-meta text-muted">A suggestion only; nothing is sent.</p>
    </section>
  );
}

function AoSteps({ obj }: { obj: ObjectPage }) {
  const steps = productionSteps(obj);
  if (steps.length === 0) return null;
  return (
    <Card data-testid="production-steps">
      <CardHeader>
        <CardTitle>Production steps</CardTitle>
      </CardHeader>
      <CardBody>
        <ol className="flex flex-col gap-2 lg:flex-row lg:flex-wrap lg:gap-x-5">
          {steps.map((s) => (
            <li key={s.label} className="flex items-center gap-2 text-row">
              {s.done ? (
                <Check className="size-4 text-ok" aria-label="Done" />
              ) : (
                <span className="flex size-4 items-center justify-center text-risk" aria-label="Not on record">
                  ?
                </span>
              )}
              <span className={cn(!s.done && "text-risk")}>{s.label}</span>
              {s.at && <span className="text-meta tabular-nums text-muted">{formatDate(s.at, { short: true })}</span>}
            </li>
          ))}
        </ol>
      </CardBody>
    </Card>
  );
}

/** The page's own source for the key-facts line: the first fact's. */
function pageSource(obj: ObjectPage): Source | undefined {
  return (obj.facts ?? []).find((f) => f.sources.length)?.sources[0];
}

/** "2 customers share this name. Showing the Portal record." with a link to each other one. */
function SameName({ obj }: { obj: ObjectPage }) {
  const others = obj.customer?.same_name ?? [];
  if (others.length === 0) return null;
  // the system the record's own id names ("Customer:portal.app_customer:…")
  const binding = obj.object_id.split(":")[1] ?? "";
  const system = binding.startsWith("bc.") ? SYSTEM_LABEL.BC : binding.startsWith("portal.") ? SYSTEM_LABEL.Portal : null;
  return (
    <p className="text-meta text-muted" data-testid="same-name">
      {others.length + 1} customers share this name.{system ? ` Showing the ${system} record.` : ""}{" "}
      {others.map((r, i) => (
        <span key={r.object_id}>
          {i > 0 ? " · " : ""}
          <Link href={refHref(r)} className="text-accent hover:underline">
            {r.label} →
          </Link>
        </span>
      ))}
    </p>
  );
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
  const [conflictsOpen, setConflictsOpen] = useState(false);
  const saved = useSaved();
  const load = useCallback(() => {
    // A quick first read (the record, its timeline and links) shows at once; the full
    // read (lateness, landed cost, people, how we know) replaces it when ready.
    const c = new AbortController();
    setError(null);
    setNotFound(false);
    setObj(null);
    let full = false;
    getObject(type, id, c.signal, true)
      .then((first) => {
        if (!full && !c.signal.aborted) setObj(first);
        if (first.complete !== false) full = true;
      })
      .catch(() => undefined);
    getObject(type, id, c.signal)
      .then((page) => {
        full = true;
        setObj(page);
      })
      .catch((e) => {
        if (c.signal.aborted) return;
        if (e instanceof ApiError && e.status === 404) setNotFound(true);
        const msg = plainError(e);
        setError(/took too long/.test(msg) ? `This ${typeLabel(type).toLowerCase()} is taking too long to gather. Try again in a minute, or ask about it instead.` : msg);
      });
    return () => c.abort();
  }, [type, id]);
  useEffect(() => load(), [load]);

  // "Chase →" lands on the card
  useEffect(() => {
    if (obj && params?.get("focus") === "chase") document.getElementById("chase")?.scrollIntoView({ block: "start" });
  }, [obj, params]);

  const key = `o:${type}/${id}`;
  const isSaved = saved.some((s) => s.key === key);

  if (error && !obj && notFound && missing)
    return <div className="mx-auto w-full max-w-[1200px] px-4 py-6 sm:px-6 xl:px-8">{missing}</div>;
  if (error && !obj)
    return (
      <div className="mx-auto w-full max-w-[1200px] px-4 py-6 sm:px-6 xl:px-8">
        <ErrorNotice message={error} onRetry={load} />
      </div>
    );
  if (!obj)
    return (
      <div className="mx-auto w-full max-w-[1200px] px-4 py-6 sm:px-6 xl:px-8">
        <ObjectSkeleton type={type} label={routeLabel} />
      </div>
    );

  const head = recordHead(obj);
  const isPo = obj.type === "PurchaseOrder";
  const isCustomer = obj.type === "Customer";
  const isSupplier = obj.type === "Supplier" || obj.type === "Vendor";
  const isAo = obj.type === "AssemblyOrder" || obj.type === "ManufacturingOrder";
  const conflicts = obj.conflicts ?? [];
  const src = pageSource(obj);
  const salesOrders = (obj.related ?? []).filter((r) => r.type === "SalesOrder");
  const otherRelated = (obj.related ?? []).filter((r) => !(isCustomer && r.type === "SalesOrder") && !(isSupplier && r.type === "PurchaseOrder") && r.object_id !== (head.counterpart as ObjectRef | null)?.object_id);
  const counterpart = head.counterpart;
  const lines = isSupplier ? [] : (obj.late_lines ?? []).filter((l) => !/^PO number:/i.test(l.line));

  return (
    <div className="mx-auto w-full max-w-[1200px] px-4 py-6 sm:px-6 xl:px-8">
      <div className="space-y-6 animate-fade-in">
        <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0 space-y-1.5">
            <p className="text-eyebrow uppercase text-muted">{typeLabel(obj.type)}</p>
            <h1 className="break-words text-title" data-testid="object-title">
              {head.title}
              {counterpart && (
                <>
                  <span className="text-muted"> · </span>
                  {"open_url" in counterpart && counterpart.open_url ? (
                    <Link href={refHref(counterpart as ObjectRef)} className="hover:underline">
                      {counterpart.label}
                    </Link>
                  ) : (
                    counterpart.label
                  )}
                </>
              )}
            </h1>
            {(head.subline || (isCustomer && (obj.customer?.also_in ?? []).length > 0)) && (
              <p className="text-meta text-muted">
                {head.subline}
                {isCustomer &&
                  (obj.customer?.also_in ?? []).map((r) => (
                    <span key={r.object_id}>
                      {head.subline ? " · " : ""}also recorded as{" "}
                      <Link href={refHref(r)} className="text-accent hover:underline">
                        {r.label} →
                      </Link>
                    </span>
                  ))}
              </p>
            )}
            {isCustomer && <SameName obj={obj} />}
            {head.lead && (
              <p className={cn("max-w-[760px] text-answer", head.leadStatus ? STATUS_CLASS[head.leadStatus].fg : "text-fg")} data-testid="object-lead">
                {head.lead}
              </p>
            )}
            {(head.badges.length > 0 || (isCustomer && conflicts.length > 0)) && (
              <div className="flex flex-wrap items-center gap-2 pt-1" data-testid="object-status">
                {head.badges.slice(0, 2).map((b) => (
                  <StatusBadge key={b.label} shown={b} />
                ))}
                {isCustomer && <ConflictsBadge conflicts={conflicts} open={conflictsOpen} onToggle={() => setConflictsOpen((v) => !v)} />}
              </div>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              aria-pressed={isSaved}
              onClick={() => toggleSaved({ key, kind: "object", label: head.askLabel, href: objectHref(type, id) })}
              className={cn("inline-flex h-9 items-center gap-1.5 rounded-lg border border-control bg-surface px-3 text-row hover:bg-surface-2 cursor-pointer", isSaved && "text-accent")}
            >
              {isSaved ? <BookmarkCheck className="size-4" aria-hidden /> : <Bookmark className="size-4" aria-hidden />}
              {isSaved ? "Saved" : "Save"}
            </button>
            <Link
              href={`/ask?q=${encodeURIComponent(isAo ? `Why did ${head.askLabel} fall short?` : `What's the latest on ${head.askLabel}?`)}&object=${encodeURIComponent(obj.object_id)}`}
              className="inline-flex h-9 items-center rounded-lg bg-accent px-3 text-row font-medium text-accent-fg hover:opacity-90"
            >
              {isAo ? "Ask why it fell short" : `Ask about ${head.askLabel}`}
            </Link>
          </div>
        </header>

        {obj.complete === false && (
          <p className="flex items-center gap-2 text-meta text-muted" role="status" data-testid="checking">
            <Loader2 className="size-4 animate-spin" aria-hidden /> Checking lateness, people and how we know…
          </p>
        )}
        {isCustomer && conflictsOpen && <ConflictRows conflicts={conflicts} />}

        {head.stats.length > 0 && (
          <StatRow>
            {head.stats.slice(0, 4).map((s) => (
              <StatTile key={s.label} s={s} />
            ))}
          </StatRow>
        )}

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <div className="min-w-0 space-y-4 lg:col-span-2">
            {(isPo || isAo) && <WhoToChase obj={obj} head={head} />}
            {!isCustomer && conflicts.map((c, i) => <ConflictCard key={i} conflict={c} />)}
            {isAo && <AoSteps obj={obj} />}
            {isSupplier && <SupplierOrders obj={obj} />}
            {obj.customer && <CustomerCard c={obj.customer} fallbackOrders={salesOrders} />}
            <LinesTable lines={lines} />
            <EvidenceTrailCard trail={obj.trail} inPage showNext={!(isPo || isAo)} />
            {obj.landed_cost && obj.landed_cost.status !== "not_established" && <LandedCostCard lc={obj.landed_cost} />}
            <Linked items={obj.linked ?? []} />
            <Activity events={obj.timeline ?? []} />
          </div>
          <div className="min-w-0 space-y-4">
            <Card data-testid="summary">
              <CardHeader>
                <CardTitle>Key facts</CardTitle>
              </CardHeader>
              <CardBody>
                <KeyFacts
                  facts={head.keyFacts.map((k) => ({
                    label: k.label,
                    value: k.value ? k.status ? <span className={STATUS_CLASS[k.status].fg}>{k.value}</span> : k.value : "",
                  }))}
                  source={src ? <SourceLine source={src} record={head.title} /> : undefined}
                />
              </CardBody>
            </Card>
            <People people={obj.people ?? []} />
            <RelatedCard refs={otherRelated} />
          </div>
        </div>
        <NotFoundSection items={obj.not_found} subject={obj.label} />
        <HiddenNotice hidden={obj.hidden} />
        <FreshnessLabel freshness={obj.freshness} />
      </div>
    </div>
  );
}
