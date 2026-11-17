import { lateFigure } from "@/components/ui/glyphs";
import { cn } from "@/lib/cn";
import { formatMoney, recordLabel } from "@/lib/format";
import { addDecimal, lateTotals } from "@/lib/late";
import type { LatePo } from "@/lib/types";

/** Whole currency units for a glance line ("$18,400 CAD"); the rows keep the cents. */
function roundMoney(amount: string, currency: string): string {
  const neg = amount.startsWith("-");
  const [int, frac = ""] = (neg ? amount.slice(1) : amount).split(".");
  const whole = Number(frac[0] ?? "0") >= 5 ? addDecimal(int, "1") : int;
  return formatMoney(`${neg ? "-" : ""}${whole}`, currency).replace(/\.00(?=\D*$)/, "");
}

/**
 * My work at a glance: how many POs are late, the oldest, their value per currency (never
 * added across currencies), the other groups by count and the replies owed.
 */
export function Glance({ late, lateTotal, groups, replies }: { late: LatePo[]; lateTotal: number | null; groups: { type: string; label: string; count: number }[]; replies: number | null }) {
  const oldest = late.reduce<LatePo | null>((a, b) => ((b.days_late ?? -1) > (a?.days_late ?? -1) ? b : a), null);
  const money = lateTotals(late);
  const noValue = late.filter((p) => !p.amount || p.amount_suspect).length;
  const partial = lateTotal !== null && lateTotal > late.length;
  const others = groups.filter((g) => g.type !== "po_late" && g.count > 0);
  const cell = "inline-flex items-baseline gap-1.5 whitespace-nowrap";
  if (!late.length && !others.length && !replies) return null;
  return (
    <p className="flex flex-wrap gap-x-5 gap-y-1 border-b border-border px-5 py-2 text-[12px] leading-4 text-muted" data-testid="home-glance">
      {late.length > 0 && (
        <span className={cell}>
          Late <span className="num font-semibold text-fg">{lateTotal ?? late.length}</span>
        </span>
      )}
      {oldest?.days_late != null && (
        <span className={cell} title={[recordLabel("PurchaseOrder", oldest.label), oldest.supplier].filter(Boolean).join(", ")}>
          Oldest <span className={cn("num font-semibold", lateFigure(oldest.days_late))}>{oldest.days_late} d</span>
        </span>
      )}
      {money.length > 0 && (
        <span className={cell} title={partial ? `Value of the first ${late.length} late POs read` : `${noValue ? `${noValue} late PO${noValue === 1 ? "" : "s"} carry no trusted value` : "Every late PO carries a value"}`}>
          {partial ? `Value of first ${late.length}` : "Late value"}
          <span className="num font-semibold text-fg">{money.map((m) => roundMoney(m.amount, m.currency)).join(" + ")}</span>
          {noValue > 0 && <span className="text-faint">({noValue} without)</span>}
        </span>
      )}
      {others.map((g) => (
        <span key={g.type} className={cell}>
          {g.label} <span className="num font-semibold text-fg">{g.count}</span>
        </span>
      ))}
      {!!replies && (
        <span className={cell}>
          Replies owed <span className="num font-semibold text-fg">{replies}</span>
        </span>
      )}
    </p>
  );
}
