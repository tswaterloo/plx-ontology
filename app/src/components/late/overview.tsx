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
    </div>
  );
}
