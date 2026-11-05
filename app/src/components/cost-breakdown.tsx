"use client";

import Link from "next/link";
import { cn } from "@/lib/cn";
import { money, objectHref, plural, safeHref } from "@/lib/format";
import type { CostBreakdownView, CostSource, CustomerAmount, ObjectRef, OrderMargin, ProductMargin, Profitability, Source } from "@/lib/types";
import { EtaPill } from "./eta-confirmed";
import { NextActionCard } from "./evidence-trail";
import { SourceChips } from "./source-chip";
import { Stat, StatRow } from "./ui/stat";

const KIND: Record<CostSource["kind"], string> = {
  item_ledger: "Booked cost (item ledger)",
  sage_cost_of_sales: "Cost of sales (Sage)",
  credit_memo: "Credit memo",
  return: "Return",
  material_po: "Purchase order",
  quote: "Quote",
};

/** Test and staging orders (UAT-…, TEST-…) never sit in a ranking. */
const TEST_ORDER = /^(?:UAT|TEST|STG|STAGING)[-_]/i;

function refHref(r: ObjectRef): string {
  return safeHref(r.open_url) ?? objectHref(r.type, r.object_id);
}

const isLoss = (m: { amount: string } | null | undefined) => !!m && m.amount.trim().startsWith("-");

function Signed({ m }: { m: { amount: string; currency: string } | null | undefined }) {
  if (!m) return <span className="text-muted">not stated</span>;
  return <span className={cn("font-medium tabular-nums", isLoss(m) ? "text-late" : "text-ok")}>{money(m)}</span>;
}

/** "partial: worst of the 3 most recent orders; …" -> "Partial: worst of …" */
function sentence(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** Orders worst margin first; orders without a margin collapse under one line with their reasons. */
export function OrderRanking({ orders, note, more = 0, inPeriod }: { orders: OrderMargin[]; note?: string | null; more?: number; inPeriod?: number }) {
  const real = orders.filter((o) => !TEST_ORDER.test(o.order.label));
  if (real.length === 0) return null;
  const withMargin = real.filter((o) => o.margin);
  const without = real.filter((o) => !o.margin);
  const partial = !!note && note.startsWith("partial");
  return (
    <div className="space-y-2">
      <table className="w-full text-row" data-testid="order-ranking">
        <caption className="mb-2 text-left text-section">
          Orders, worst margin first
          {typeof inPeriod === "number" && inPeriod > 0 && <span className="font-normal text-muted"> · {real.length} of {inPeriod} checked</span>}
          {partial && <span className="mt-0.5 block text-meta font-normal text-muted">{sentence(note!)}</span>}
        </caption>
        <thead className="text-eyebrow uppercase text-muted">
          <tr className="h-8 border-y border-border-strong bg-bg-subtle">
            <th scope="col" className="px-2 py-2 text-left">Order</th>
            <th scope="col" className="py-2 text-right">Revenue</th>
            <th scope="col" className="py-2 text-right">Item cost</th>
            <th scope="col" className="py-2 text-right">Margin*</th>
          </tr>
        </thead>
        <tbody>
          {withMargin.map((o) => (
            <tr key={o.order.object_id} className="border-b border-border last:border-0">
              <td className="py-2">
                <Link href={refHref(o.order)} className="font-medium hover:underline">
                  {o.order.label}
                </Link>
              </td>
              <td className="py-2 text-right tabular-nums">{money(o.revenue)}</td>
              <td className="py-2 text-right tabular-nums">{money(o.booked_cost)}</td>
              <td className="py-2 text-right">
                <Signed m={o.margin} />
              </td>
            </tr>
          ))}
          {without.length > 0 && (
            <tr>
              <td colSpan={4} className="py-2">
                <details>
                  <summary className="cursor-pointer text-meta text-muted hover:text-fg">
                    {plural(without.length, "order")} {without.length === 1 ? "has" : "have"} no margin yet
                  </summary>
                  <ul className="mt-1 space-y-0.5 text-meta" data-testid="no-margin">
                    {without.map((o) => (
                      <li key={o.order.object_id}>
                        <Link href={refHref(o.order)} className="font-medium hover:underline">
                          {o.order.label}
                        </Link>
                        <span className="text-muted">: {o.gap ?? "not stated"}</span>
                      </li>
                    ))}
                  </ul>
                </details>
              </td>
            </tr>
          )}
        </tbody>
        {more > 0 && (
          <tfoot>
            <tr>
              <td colSpan={4} className="pt-1 text-meta text-muted" data-testid="orders-more">
                and {more} more
              </td>
            </tr>
          </tfoot>
        )}
      </table>
      <p className="text-meta text-muted">*Item cost only. Freight, labour and overhead aren&apos;t included.</p>
    </div>
  );
}

/** Margin per item over the checked orders, worst first; a partial row says so. */
export function ProductRanking({ products }: { products: ProductMargin[] }) {
  if (products.length < 2) return null;
  return (
    <table className="w-full text-row" data-testid="product-ranking">
      <caption className="mb-2 text-left text-section">By product, worst margin first</caption>
      <thead className="text-eyebrow uppercase text-muted">
        <tr className="h-8 border-y border-border-strong bg-bg-subtle">
          <th scope="col" className="px-2 py-2 text-left">Item</th>
          <th scope="col" className="py-2 text-right">Qty</th>
          <th scope="col" className="py-2 text-right">Unit cost</th>
          <th scope="col" className="py-2 text-right">Margin*</th>
        </tr>
      </thead>
      <tbody>
        {products.map((p, i) => (
          <tr key={i} className="border-b border-border last:border-0">
            <td className="py-2">
              {p.item}
              <span className="text-meta text-muted"> · {plural(p.orders, "order")}{p.complete ? "" : " · checked orders only"}</span>
              <SourceChips sources={p.sources} />
            </td>
            <td className="py-2 text-right tabular-nums">{p.quantity}{p.unit ? ` ${p.unit}` : ""}</td>
            <td className="py-2 text-right tabular-nums text-muted">{p.unit_cost_range ?? "—"}</td>
            <td className="py-2 text-right">
              <Signed m={p.margin} />
              {p.margin_pct && <span className="block text-meta text-muted">{p.margin_pct}%</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** One order's cost breakdown: its lines as a table, where the cost came from, the drivers. */
export function CostBreakdown({ breakdown }: { breakdown: CostBreakdownView }) {
  const b = breakdown;
  const loss = isLoss(b.margin);
  const abs = b.margin ? money({ ...b.margin, amount: b.margin.amount.replace(/^-/, "") }) : null;
  const lines = b.lines ?? [];
  return (
    <section className="space-y-3 border-t border-border pt-3" data-testid="cost-breakdown">
      <h3 className="text-section">
        {loss ? "Why " : "How "}
        <Link href={refHref(b.order)} className="hover:underline">
          {b.order.label}
        </Link>{" "}
        {abs ? (loss ? `lost ${abs}` : `made ${abs}`) : loss ? "lost money" : "made money"}
      </h3>
      {lines.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-row" aria-label="Order lines">
            <thead className="text-eyebrow uppercase text-muted">
              <tr className="h-8 border-y border-border-strong bg-bg-subtle">
                <th scope="col" className="px-1 py-1.5 text-left">Item</th>
                <th scope="col" className="px-1 py-1.5 text-right">Qty</th>
                <th scope="col" className="px-1 py-1.5 text-right">Revenue</th>
                <th scope="col" className="px-1 py-1.5 text-right">Cost</th>
                <th scope="col" className="px-1 py-1.5 text-right">Margin*</th>
                {lines.length > 1 && <th scope="col" className="px-1 py-1.5 text-right">Share of cost</th>}
              </tr>
            </thead>
            <tbody>
              {lines.map((l, i) => (
                <tr key={i} className="border-b border-border align-top last:border-0">
                  <td className="px-1 py-1.5">
                    <span className="font-medium">{l.label}</span> <SourceChips sources={l.sources} max={1} />
                  </td>
                  <td className="whitespace-nowrap px-1 py-1.5 text-right tabular-nums">{l.quantity ? `${l.quantity} ${l.unit ?? ""}`.trim() : "—"}</td>
                  <td className="whitespace-nowrap px-1 py-1.5 text-right tabular-nums">{money(l.revenue) || "—"}</td>
                  <td className="whitespace-nowrap px-1 py-1.5 text-right tabular-nums">{money(l.booked_cost) || "—"}</td>
                  <td className="whitespace-nowrap px-1 py-1.5 text-right">
                    <Signed m={l.margin} />
                  </td>
                  {lines.length > 1 && <td className="px-1 py-1.5 text-right tabular-nums text-muted">{l.share_of_cost_pct ? `${l.share_of_cost_pct}%` : "—"}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {(b.cost_sources ?? []).length > 0 && (
        <ul className="space-y-1 border-t border-border pt-2 text-row" aria-label="Where the cost came from">
          {(b.cost_sources ?? []).map((c, i) => (
            <li key={i} className="flex flex-wrap items-baseline gap-x-1.5">
              <span className="font-medium">{KIND[c.kind] ?? c.kind}</span>
              <span className="text-muted">·</span>
              <span>{c.label}</span>
              {c.detail && <span className="text-muted">({c.detail})</span>}
              {c.amount && (
                <>
                  <span className="text-muted">·</span>
                  <span className="tabular-nums">{money(c.amount)}</span>
                </>
              )}
              {c.per_unit && <span className="tabular-nums text-muted">· {c.per_unit}</span>}
              <SourceChips sources={c.sources} />
            </li>
          ))}
        </ul>
      )}
      {(b.drivers ?? []).length > 0 && (
        <details className="border-t border-border pt-2 text-row">
          <summary className="cursor-pointer text-meta text-muted hover:text-fg">What drove the margin, in full</summary>
          <ul className="mt-1 space-y-1" aria-label="What drove the margin">
            {(b.drivers ?? []).map((d, i) => (
              <li key={i}>{d}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

/** A customer's profitability: four numbers, the ranking, the worst order's breakdown and the
 * suggested next step (a suggestion only: nothing is written). */
export function ProfitabilityView({ p }: { p: Profitability }) {
  const counted = p.orders_counted;
  const inPeriod = p.orders_in_period;
  return (
    <div className="space-y-4" data-testid="profitability">
      <CustomerVerdict p={p} />
      <StatRow>
        <Stat label="Revenue" value={money(p.revenue) || "—"} caption={p.period} />
        <Stat label="Item cost" value={money(p.booked_cost) || "—"} caption="booked in Business Central" />
        <Stat label="Margin" value={money(p.margin) || "—"} status={p.margin ? (isLoss(p.margin) ? "late" : "ok") : undefined} caption={p.margin_pct ? `${p.margin_pct}% of revenue` : undefined} />
        <Stat
          label="Orders with a margin"
          value={`${counted} of ${inPeriod}${p.orders_in_period_exact === false ? "+" : ""}`}
          status={counted < inPeriod ? "risk" : undefined}
          caption={counted < inPeriod ? "the rest have no cost yet" : "every order counted"}
        />
      </StatRow>
      <OrderRanking orders={p.orders ?? []} note={p.ranking} more={p.orders_more ?? 0} inPeriod={inPeriod} />
      {p.test_records_hidden && (
        <p className="text-meta text-muted" data-testid="tests-hidden">
          {p.test_records_hidden} (test and staging orders are not counted)
        </p>
      )}
      {p.margin_note && (
        <p className="text-meta text-muted" data-testid="margin-basis">
          Margins are {p.margin_basis ?? "on item cost only"}. {p.margin_note}
        </p>
      )}
      <ProductRanking products={p.products ?? []} />
      {p.breakdown && <CostBreakdown breakdown={p.breakdown} />}
      {p.next_action && <NextActionCard action={p.next_action} title="Suggested next step" />}
    </div>
  );
}

/** Gap codes that mean Business Central holds no valid item cost for the order. */
const NO_VALID_COST = new Set(["COST_NOT_VALUED", "COST_NOT_YET_ACTUAL"]);

/** Every source a breakdown cites, once each (lines first, then where the cost came from). */
function breakdownSources(b: CostBreakdownView): Source[] {
  const seen = new Set<string>();
  const out: Source[] = [];
  for (const s of [...(b.lines ?? []).flatMap((l) => l.sources ?? []), ...(b.cost_sources ?? []).flatMap((c) => c.sources ?? [])]) {
    const key = s.handle ?? s.open_url ?? s.label;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(s);
  }
  return out;
}

/** The margin in one line: the amount, the verdict and why in one or two plain sentences, its sources. */
export function MarginHeadline({ b, scope }: { b: CostBreakdownView; scope?: string }) {
  const loss = isLoss(b.margin);
  const why = (b.why ?? []).length > 0 ? b.why! : (b.drivers ?? []).slice(0, 1);
  const sources = breakdownSources(b);
  return (
    <section className="space-y-2" data-testid="margin-headline">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className={cn("text-stat tabular-nums", b.margin ? (loss ? "text-late" : "text-ok") : "text-muted")}>{b.margin ? money(b.margin) : "Not stated"}</span>
        {b.margin_pct && <span className="text-row tabular-nums text-muted">{b.margin_pct}% of revenue</span>}
        <EtaPill shown={b.margin ? (loss ? { status: "late", label: "Losing money" } : { status: "ok", label: "Profitable" }) : { status: "neutral", label: "No margin stated" }} testId="margin-verdict" />
        {scope && <span className="text-meta text-muted">{scope}</span>}
      </div>
      {why.length > 0 && (
        <div className="space-y-0.5 text-body" data-testid="margin-why">
          {why.map((w, i) => (
            <p key={i}>{w}</p>
          ))}
        </div>
      )}
      {sources.length > 0 && (
        <p className="flex flex-wrap items-center gap-1.5 text-meta text-muted" data-testid="margin-sources">
          Sources <SourceChips sources={sources} max={4} />
        </p>
      )}
    </section>
  );
}

/** An order with no margin: "No valid cost in BC" when Business Central holds no valid item cost, else why in words. */
export function NoMargin({ order }: { order: OrderMargin }) {
  const noCost = !!order.gap_code && NO_VALID_COST.has(order.gap_code);
  return (
    <section className="space-y-1.5" data-testid="margin-none">
      <EtaPill shown={noCost ? { status: "suspect", label: "No valid cost in BC" } : { status: "neutral", label: "No margin yet" }} testId="margin-verdict" />
      <p className="text-body">
        {noCost
          ? `Business Central holds no valid item cost for ${order.order.label}, so its margin isn't stated. Finance can value the item's cost there.`
          : `No margin is stated for ${order.order.label}: ${order.gap ?? "not stated"}.`}
      </p>
    </section>
  );
}

/** A customer whose figures are Sage's and who has no margin section: invoiced revenue before credit notes, no margin stated. */
export function SageMargin({ amounts }: { amounts: CustomerAmount[] }) {
  const sage = amounts.filter((a) => a.system === "Sage" && /^invoiced/i.test(a.label));
  if (sage.length === 0) return null;
  return (
    <section className="space-y-1.5" data-testid="margin-sage">
      <EtaPill shown={{ status: "neutral", label: "Sage, before credit notes" }} testId="margin-verdict" />
      <p className="text-body">
        {sage.map((a) => `${a.label.replace(/ \(.*\)$/, "")} ${money(a.money)}`).join("; ")} in Sage, {sage[0].period}. That is invoiced revenue before credit notes; no margin is stated for it here.
      </p>
      <p className="flex flex-wrap items-center gap-1.5 text-meta text-muted">
        Sources <SourceChips sources={sage.flatMap((a) => a.sources ?? [])} max={3} />
      </p>
    </section>
  );
}

/** A customer's verdict in one line, and why its worst order made or lost what it did. */
function CustomerVerdict({ p }: { p: Profitability }) {
  const b = p.breakdown;
  const shown =
    p.verdict === "profitable"
      ? { status: "ok" as const, label: `Profitable over ${p.period}` }
      : p.verdict === "not_profitable"
        ? { status: "late" as const, label: `Losing money over ${p.period}` }
        : { status: "neutral" as const, label: "Not stated: not every order has a margin" };
  const why = b ? ((b.why ?? []).length > 0 ? b.why! : (b.drivers ?? []).slice(0, 1)) : [];
  return (
    <section className="space-y-1.5" data-testid="customer-verdict">
      <EtaPill shown={shown} testId="margin-verdict" />
      {b && (
        <div className="space-y-0.5 text-body" data-testid="margin-why">
          <p>
            {isLoss(b.margin) ? "Worst order" : "Lowest margin"}:{" "}
            <Link href={refHref(b.order)} className="font-medium hover:underline">
              {b.order.label}
            </Link>{" "}
            <Signed m={b.margin} />.
          </p>
          {why.map((w, i) => (
            <p key={i}>{w}</p>
          ))}
        </div>
      )}
    </section>
  );
}
