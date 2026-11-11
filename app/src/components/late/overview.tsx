"use client";

import Link from "next/link";
import { LateShape } from "@/components/late-shape";
import { OpenRing } from "@/components/ui/glyphs";
import { cn } from "@/lib/cn";
import { formatMoney, objectHref, recordLabel, safeHref } from "@/lib/format";
import { valueOf } from "@/lib/late";
import type { LatePo } from "@/lib/types";

function Figure({ label, glyph, value, unit, caption, testId }: { label: string; glyph?: React.ReactNode; value: string; unit?: string; caption?: React.ReactNode; testId?: string }) {
  return (
    <div className="min-w-0" data-testid={testId}>
      <div className="flex items-center gap-1.5 text-[12px] leading-4 text-muted">
        {glyph}
        {label}
      </div>
      <div className="mt-1 flex items-baseline gap-1.5 whitespace-nowrap">
        <span className="text-[22px] leading-7 font-semibold tracking-[-0.02em] num">{value}</span>
        {unit && <span className="text-[13px] text-muted">{unit}</span>}
      </div>
      {caption && <div className="mt-0.5 truncate text-[12px] leading-4 text-muted">{caption}</div>}
    </div>
  );
}

/** Each currency's total of the POs that carry a trusted value, largest first. */
function totals(items: LatePo[]): [string, number][] {
  const m = new Map<string, number>();
  for (const p of items) {
    const v = valueOf(p);
    if (v !== null) m.set(p.amount!.currency, (m.get(p.amount!.currency) ?? 0) + v);
  }
  return [...m.entries()].sort((a, b) => b[1] - a[1]);
}

/** What to do about one late PO, from what is on record about its supplier confirmation. */
export function nextStep(p: LatePo): string {
  switch (p.confirmation) {
    case "none_found":
      return "No supplier date on record: ask the supplier for one";
    case "found_unverified":
      return "A reply may carry the date: read it and confirm the date";
    case "needs_check":
      return "A confirmation names a number another order holds: check which order it is";
    case "confirmed":
      return "The confirmed date has passed: chase a new ship date";
    default:
      // the list doesn't say (the order's page decides): what the list does know, else open it
      return p.reason?.trim() ? `${p.reason.trim()}; open the order to see where it stands` : "Open the order to see where it stands";
  }
}

/** The late POs to act on first: longest late, then largest, each with its next step. */
function FirstSteps({ items, onPick }: { items: LatePo[]; onPick?: (po: LatePo) => void }) {
  const first = [...items]
    // longest late first; on a tie the larger value, compared only within one currency
    .sort((a, b) => (b.days_late ?? -1) - (a.days_late ?? -1) || (a.amount?.currency === b.amount?.currency ? (valueOf(b) ?? 0) - (valueOf(a) ?? 0) : 0))
    .slice(0, 6);
  if (first.length === 0) return null;
  return (
    <section data-testid="late-first">
      <h2 className="text-[12px] font-medium text-muted">Act on first</h2>
      <table className="mt-1 w-full text-left text-[13px]">
        <tbody className="divide-y divide-border">
          {first.map((p) => {
            const label = (
              <>
                <span className="font-medium">{recordLabel("PurchaseOrder", p.label)}</span>
                {p.supplier && <span className="text-muted"> {p.supplier}</span>}
              </>
            );
            return (
              <tr key={p.object_id} className="align-top">
                <td className="max-w-[240px] truncate py-1.5 pr-3">
                  {onPick ? (
                    <button type="button" onClick={() => onPick(p)} className="max-w-full truncate text-left hover:underline cursor-pointer">
                      {label}
                    </button>
                  ) : (
                    <Link href={safeHref(p.open_url) ?? objectHref("PurchaseOrder", p.object_id)} className="hover:underline">
                      {label}
                    </Link>
                  )}
                </td>
                <td className="whitespace-nowrap py-1.5 pr-3 text-right tabular-nums text-late">{typeof p.days_late === "number" ? `${p.days_late} d` : ""}</td>
                <td className="whitespace-nowrap py-1.5 pr-3 text-right tabular-nums text-muted max-sm:hidden">
                  {p.amount && valueOf(p) !== null ? formatMoney(p.amount.amount, p.amount.currency) : ""}
                </td>
                <td className="py-1.5 text-[12px] text-muted max-md:hidden">{nextStep(p)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

/** The late list at a glance: how many, the oldest, how many have no supplier date, their value; then the chart. */
export function LateOverview({
  items,
  total,
  picked,
  onSupplier,
  onPick,
  parts = "all",
  className,
}: {
  items: LatePo[];
  total: number;
  picked: string | null;
  onSupplier: (name: string | null) => void;
  onPick?: (po: LatePo) => void;
  /** a phone shows the figures above the list and the chart after it */
  parts?: "all" | "figures" | "chart";
  className?: string;
}) {
  if (parts === "chart") return items.length > 1 ? <LateShape items={items} picked={picked} onSupplier={onSupplier} onPick={onPick} className={className} /> : null;
  const oldest = items.reduce<LatePo | null>((o, p) => ((p.days_late ?? -1) > (o?.days_late ?? -1) ? p : o), null);
  // no supplier confirmation on any copy of the order (found-unverified, needs-check and
  // confirmed rows have one)
  const unconfirmed = items.filter((p) => p.confirmation === "none_found").length;
  const money = totals(items);
  return (
    <div className={cn("space-y-8", className)} data-testid="late-summary">
      <div className="grid grid-cols-2 gap-x-6 gap-y-5 @3xl:grid-cols-4 @3xl:gap-x-10">
        <Figure label="Late" value={String(total)} unit={total === 1 ? "PO" : "POs"} testId="late-count" />
        {oldest && typeof oldest.days_late === "number" && (
          <Figure
            label="Oldest"
            value={String(oldest.days_late)}
            unit={oldest.days_late === 1 ? "day" : "days"}
            caption={
              onPick ? (
                <button type="button" onClick={() => onPick(oldest)} className="max-w-full truncate hover:text-fg hover:underline cursor-pointer">
                  {recordLabel("PurchaseOrder", oldest.label)}
                  {oldest.supplier ? `, ${oldest.supplier}` : ""}
                </button>
              ) : (
                <Link href={safeHref(oldest.open_url) ?? objectHref("PurchaseOrder", oldest.object_id)} className="block max-w-full truncate hover:text-fg hover:underline">
                  {recordLabel("PurchaseOrder", oldest.label)}
                  {oldest.supplier ? `, ${oldest.supplier}` : ""}
                </Link>
              )
            }
            testId="late-oldest"
          />
        )}
        <Figure label="No supplier date" glyph={<OpenRing size={12} tone="neutral" />} value={String(unconfirmed)} unit={`of ${items.length}`} />
        {money.length > 0 && (
          <Figure
            label="Value"
            value={formatMoney(String(money[0][1].toFixed(2)), money[0][0]).replace(/\.\d\d(?=\D*$)/, "")}
            caption={money.length > 1 ? `and ${money.slice(1).map(([c, v]) => formatMoney(v.toFixed(2), c).replace(/\.\d\d(?=\D*$)/, "")).join(", ")}` : undefined}
          />
        )}
      </div>
      {parts === "all" && items.length > 1 && <LateShape items={items} picked={picked} onSupplier={onSupplier} onPick={onPick} />}
      {parts === "all" && <FirstSteps items={items} onPick={onPick} />}
    </div>
  );
}
