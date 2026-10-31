"use client";

import { Lightbulb, TrendingDown, TrendingUp } from "lucide-react";
import Link from "next/link";
import { cn } from "@/lib/cn";
import { money, objectHref, safeHref } from "@/lib/format";
import type { CostBreakdownView, CostSource, ObjectRef, OrderMargin, Profitability } from "@/lib/types";
import { SourceChip } from "./source-chip";

const KIND: Record<CostSource["kind"], string> = {
  item_ledger: "Booked cost (item ledger)",
  credit_memo: "Credit memo",
  return: "Return",
  material_po: "Purchase order",
  quote: "Quote",
};

function refHref(r: ObjectRef): string {
  return safeHref(r.open_url) ?? objectHref(r.type, r.object_id);
}

function Signed({ m }: { m: { amount: string; currency: string } | null | undefined }) {
  if (!m) return <span className="text-muted">not stated</span>;
  const loss = m.amount.trim().startsWith("-");
  return <span className={cn("tabular-nums font-medium", loss ? "text-bad" : "text-good")}>{money(m)}</span>;
}

/** The orders of the period, worst margin first; an order with no margin says why. */
export function OrderRanking({ orders, note, more = 0 }: { orders: OrderMargin[]; note?: string | null; more?: number }) {
  if (orders.length === 0) return null;
  return (
    <table className="w-full text-sm" data-testid="order-ranking">
      <caption className="mb-1 text-left text-xs font-semibold uppercase tracking-wide text-muted">
        Orders, worst margin first{note && note.startsWith("partial") ? ` (${note})` : ""}
      </caption>
      <thead className="text-left text-xs text-muted">
        <tr>
          <th className="py-1 font-medium">Order</th>
          <th className="py-1 font-medium">Revenue</th>
          <th className="py-1 font-medium">Cost</th>
          <th className="py-1 font-medium">Margin</th>
        </tr>
      </thead>
      <tbody>
        {orders.map((o) => (
          <tr key={o.order.object_id} className="border-t border-border">
            <td className="py-1.5">
              <Link href={refHref(o.order)} className="font-medium hover:underline">
                {o.order.label}
              </Link>
            </td>
            {o.margin ? (
              <>
                <td className="py-1.5 tabular-nums">{money(o.revenue)}</td>
                <td className="py-1.5 tabular-nums">{money(o.booked_cost)}</td>
                <td className="py-1.5">
                  <Signed m={o.margin} />
                </td>
              </>
            ) : (
              <td colSpan={3} className="py-1.5 text-muted">
                No margin: {o.gap ?? "not stated"}
              </td>
            )}
          </tr>
        ))}
      </tbody>
      {more > 0 && (
        <tfoot>
          <tr>
            <td colSpan={4} className="py-1.5 text-muted" data-testid="orders-more">
              and {more} more
            </td>
          </tr>
        </tfoot>
      )}
    </table>
  );
}

/** One order's cost breakdown: its lines, where the cost came from, and the drivers. */
export function CostBreakdown({ breakdown }: { breakdown: CostBreakdownView }) {
  const b = breakdown;
  const loss = b.margin?.amount.trim().startsWith("-");
  return (
    <section className="space-y-3 rounded-xl border border-border bg-surface p-4" data-testid="cost-breakdown">
      <h3 className="flex flex-wrap items-center gap-2 text-sm font-semibold">
        {loss ? <TrendingDown className="size-4 text-bad" aria-hidden /> : <TrendingUp className="size-4 text-good" aria-hidden />}
        Why <Link href={refHref(b.order)} className="hover:underline">{b.order.label}</Link> {loss ? "lost money" : "made what it made"}
        <span className="font-normal text-muted">
          revenue {money(b.revenue) || "not stated"} · cost {money(b.booked_cost) || "not stated"} · margin <Signed m={b.margin} />
        </span>
      </h3>
      {(b.lines ?? []).length > 0 && (
        <ul className="space-y-1.5 text-sm" aria-label="Order lines">
          {(b.lines ?? []).map((l, i) => (
            <li key={i} className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-medium">{l.label}</span>
              {l.quantity && <span className="text-muted">{l.quantity} {l.unit ?? ""}</span>}
              <span>revenue {money(l.revenue) || "not stated"}, cost {money(l.booked_cost) || "not stated"}, margin</span>
              <Signed m={l.margin} />
              {l.share_of_cost_pct && <span className="text-muted">({l.share_of_cost_pct}% of the cost)</span>}
              <span className="inline-flex flex-wrap gap-1">
                {(l.sources ?? []).map((s, j) => (
                  <SourceChip key={j} source={s} />
                ))}
              </span>
            </li>
          ))}
        </ul>
      )}
      {(b.cost_sources ?? []).length > 0 && (
        <ul className="space-y-1 border-t border-border pt-2 text-sm" aria-label="Where the cost came from">
          {(b.cost_sources ?? []).map((c, i) => (
            <li key={i} className="flex flex-wrap items-baseline gap-x-2">
              <span className="text-xs font-semibold uppercase tracking-wide text-muted">{KIND[c.kind] ?? c.kind}</span>
              <span>{c.label}</span>
              {c.detail && <span className="text-muted">{c.detail}</span>}
              {c.amount && <span className="tabular-nums">{money(c.amount)}</span>}
              {c.per_unit && <span className="tabular-nums">{c.per_unit}</span>}
              <span className="inline-flex flex-wrap gap-1">
                {(c.sources ?? []).map((s, j) => (
                  <SourceChip key={j} source={s} />
                ))}
              </span>
            </li>
          ))}
        </ul>
      )}
      {(b.drivers ?? []).length > 0 && (
        <ul className="space-y-1 border-t border-border pt-2 text-sm" aria-label="What drove the margin">
          {(b.drivers ?? []).map((d, i) => (
            <li key={i}>{d}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** A customer's profitability: totals, the ranking, the worst order's breakdown and the
 * suggested next step (a suggestion only: nothing is written). */
export function ProfitabilityView({ p }: { p: Profitability }) {
  return (
    <div className="space-y-3" data-testid="profitability">
      <OrderRanking orders={p.orders ?? []} note={p.ranking} more={p.orders_more ?? 0} />
      {p.breakdown && <CostBreakdown breakdown={p.breakdown} />}
      {p.next_action && (
        <p className="flex items-start gap-2 rounded-xl border border-accent/30 bg-accent-soft/50 p-3 text-sm" data-testid="next-action">
          <Lightbulb className="mt-0.5 size-4 shrink-0 text-accent" aria-hidden />
          <span>
            <span className="font-medium">Suggested next step: </span>
            {p.next_action.text} <span className="text-muted">(a suggestion; nothing was changed)</span>
          </span>
        </p>
      )}
    </div>
  );
}
