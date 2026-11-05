"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import { formatDate, formatMoney, objectHref, plural, recordLabel, safeHref } from "@/lib/format";
import { supplierCounts } from "@/lib/late";
import type { LatePo } from "@/lib/types";

/**
 * The late list drawn: one dot per late PO on a days-late axis, stacked where they share a
 * value, with rules at a week, two weeks and a month; then a count per supplier. Hover names a
 * dot in the readout line; a click opens the PO. Built only from the late list.
 */

const R = 3.5;
const STEP = 2 * R + 1.5;
const PAD_L = 8;
const PAD_R = 12;

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const fit = () => setW(Math.round(el.getBoundingClientRect().width));
    fit();
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(fit);
    ro?.observe(el);
    return () => ro?.disconnect();
  }, []);
  return [ref, w];
}

/** A domain end that reads well: the next multiple of a week (or a month past 90 days). */
export function niceMax(max: number): number {
  const step = max > 90 ? 30 : 7;
  return Math.max(step, Math.ceil((max + 1) / step) * step);
}

/**
 * Dots stacked upward where they collide (a greedy dodge), at most `maxLevels` high; a taller
 * stack continues in the next column beside it (right, then left), like a Wilkinson dot plot.
 * Returns each dot's level and its column shift (in dot steps), in input order.
 */
export function dodge(xs: number[], step = STEP, maxLevels = Infinity): { level: number; shift: number }[] {
  const out: { level: number; shift: number }[] = [];
  const placed: { x: number; l: number }[] = [];
  const free = (x: number, l: number) => !placed.some((p) => p.l === l && Math.abs(p.x - x) < step - 0.01);
  for (const x0 of xs) {
    let done = false;
    for (let k = 0; !done && k < 40; k++) {
      const shift = k === 0 ? 0 : k % 2 ? Math.ceil(k / 2) : -Math.ceil(k / 2);
      const x = x0 + shift * step;
      for (let l = 0; l < maxLevels; l++) {
        if (free(x, l)) {
          placed.push({ x, l });
          out.push({ level: l, shift });
          done = true;
          break;
        }
      }
    }
    if (!done) out.push({ level: 0, shift: 0 });
  }
  return out;
}

const hrefOf = (p: LatePo) => safeHref(p.open_url) ?? objectHref("PurchaseOrder", p.object_id);

export function LateShape({ items, embedded = false, onSupplier, picked }: { items: LatePo[]; embedded?: boolean; onSupplier?: (name: string | null) => void; picked?: string | null }) {
  // a dot's click goes through the readout's own link (client navigation, no router hook)
  const go = useRef<HTMLAnchorElement>(null);
  const [pending, setPending] = useState(false);
  const [box, width] = useWidth<HTMLDivElement>();
  const dated = useMemo(() => items.filter((p) => typeof p.days_late === "number").sort((a, b) => (a.days_late ?? 0) - (b.days_late ?? 0)), [items]);
  const [focus, setFocus] = useState<LatePo | null>(null);
  const max = dated.length ? (dated[dated.length - 1].days_late ?? 0) : 0;
  const min = dated.length ? (dated[0].days_late ?? 0) : 0;
  const top = niceMax(max);
  const plotW = Math.max(0, width - PAD_L - PAD_R);
  const x = (d: number) => PAD_L + (d / top) * plotW;
  const placed = useMemo(() => dodge(dated.map((p) => x(p.days_late ?? 0)), STEP, 6), [dated, plotW]); // eslint-disable-line react-hooks/exhaustive-deps
  const maxLevel = placed.length ? Math.max(...placed.map((p) => p.level)) : 0;
  const plotH = Math.max(40, (maxLevel + 1) * STEP + 14);
  const base = 18 + plotH;
  const height = base + 30;
  // reference rules whose labels would collide on a narrow screen are drawn without a label
  const refs = [7, 14, 30, 60, 90].filter((d) => d < top);
  const labelled = new Set<number>();
  let lastX = -Infinity;
  for (const d of refs) {
    const px = PAD_L + (d / top) * Math.max(0, width - PAD_L - PAD_R);
    if (px - lastX >= 46) {
      labelled.add(d);
      lastX = px;
    }
  }
  const shown = focus ?? dated[dated.length - 1] ?? null;
  const suppliers = useMemo(() => supplierCounts(dated).filter(([name]) => name).slice(0, 6), [dated]);
  const others = dated.length - suppliers.reduce((n, [, c]) => n + c, 0);

  useEffect(() => {
    if (!pending) return;
    setPending(false);
    go.current?.click();
  }, [pending]);

  if (dated.length === 0) return null;
  return (
    <section className={cn(!embedded && "border-t border-rule")} aria-label="How late, drawn" data-testid="late-shape">
      <div className={cn("flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1", embedded ? "pt-3.5 pb-1" : "min-h-11 py-2.5")}>
        <h2 className={embedded ? "text-meta font-semibold text-fg" : "text-section"}>Days late</h2>
        <p className="text-meta text-muted">
          {plural(dated.length, "PO")}, {min === max ? `${max} days` : `${min} to ${max} days`}
        </p>
      </div>
      <div ref={box} className="min-w-0">
        {width > 0 && (
          <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="block" role="img" aria-label={`Days late of ${dated.length} purchase orders, from ${min} to ${max}`} data-testid="late-strip">
            {refs.map((d) => (
              <g key={d}>
                <line x1={x(d)} x2={x(d)} y1={10} y2={base} className="stroke-border" />
                {labelled.has(d) && (
                  <text x={x(d) + 3} y={20} className="fill-faint text-[10px]">
                    {d === 7 ? "a week" : d === 14 ? "2 weeks" : d === 30 ? "a month" : `${d} d`}
                  </text>
                )}
              </g>
            ))}
            {/* range frame: the axis spans only the data, its ends printed */}
            <line x1={x(min)} x2={x(max)} y1={base + 4} y2={base + 4} className="stroke-fg" />
            <text x={x(min)} y={base + 20} textAnchor={min === max || x(max) - x(min) < 110 ? "middle" : "start"} className="fill-muted text-[11px] tabular-nums">
              {min === max ? `${min} d` : x(max) - x(min) < 110 ? `${min} to ${max} days late` : `${min} d`}
            </text>
            {min !== max && x(max) - x(min) >= 110 && (
              <text x={x(max)} y={base + 20} textAnchor="end" className="fill-muted text-[11px] tabular-nums">
                {max} days late
              </text>
            )}
            {dated.map((p, i) => {
              const cx = x(p.days_late ?? 0) + placed[i].shift * STEP;
              const cy = base - R - 2 - placed[i].level * STEP;
              const on = shown?.object_id === p.object_id;
              return (
                <g
                  key={p.object_id}
                  onMouseEnter={() => setFocus(p)}
                  onClick={() => {
                    setFocus(p);
                    setPending(true);
                  }}
                  className="cursor-pointer"
                  data-testid="late-dot"
                >
                  <circle cx={cx} cy={cy} r={R + 3} fill="transparent" />
                  <circle cx={cx} cy={cy} r={R} className={on ? "fill-late" : "fill-fg"} />
                  {on && <circle cx={cx} cy={cy} r={R + 3} className="fill-none stroke-late" strokeWidth={1.25} />}
                </g>
              );
            })}
          </svg>
        )}
      </div>
      {shown && (
        <p className="flex min-h-9 flex-wrap items-baseline gap-x-2 border-t border-border py-2 text-meta" aria-live="polite" data-testid="late-readout">
          <Link ref={go} href={hrefOf(shown)} className="font-medium text-fg hover:text-accent hover:underline">
            {recordLabel("PurchaseOrder", shown.label)}
          </Link>
          {shown.supplier && <span className="text-muted">{shown.supplier}</span>}
          <span className="tabular-nums text-late">{shown.days_late} days late</span>
          {shown.due_date && <span className="text-faint">needed {formatDate(shown.due_date, { short: true })}</span>}
          {shown.amount && <span className="ml-auto tabular-nums text-muted">{formatMoney(shown.amount.amount, shown.amount.currency)}</span>}
        </p>
      )}

      {suppliers.length > 1 && (
        <div className="mt-6" data-testid="late-multiples">
          <h3 className="text-meta font-semibold text-fg">By supplier</h3>
          <ul className="mt-1 grid grid-cols-1 gap-x-8 sm:grid-cols-2 lg:grid-cols-3">
            {suppliers.map(([name, n]) => (
              <li key={name}>
                {onSupplier ? (
                  <button
                    type="button"
                    onClick={() => onSupplier(picked === name ? null : name)}
                    aria-pressed={picked === name}
                    className={cn("group block w-full text-left cursor-pointer", picked && picked !== name && "opacity-45")}
                    data-testid="late-multiple"
                  >
                    <MultipleHead name={name} n={n} on={picked === name} />
                  </button>
                ) : (
                  <Link href={`/late?supplier=${encodeURIComponent(name)}`} className="group block" data-testid="late-multiple">
                    <MultipleHead name={name} n={n} />
                  </Link>
                )}
              </li>
            ))}
          </ul>
          {others > 0 && <p className="mt-1 text-meta text-muted">Other suppliers: {plural(others, "PO")}</p>}
        </div>
      )}
    </section>
  );
}

function MultipleHead({ name, n, on = false }: { name: string; n: number; on?: boolean }) {
  return (
    <span className="flex items-baseline justify-between gap-3 border-b border-border py-1.5 text-row">
      <span className={cn("min-w-0 truncate text-accent group-hover:underline", on && "font-semibold underline")}>{name}</span>
      <span className="shrink-0 tabular-nums text-fg">{n}</span>
    </span>
  );
}
