"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import { formatDate, formatMoney, objectHref, plural, recordLabel, safeHref } from "@/lib/format";
import { supplierCounts } from "@/lib/late";
import type { LatePo } from "@/lib/types";

/**
 * The late list, drawn (Tufte): one dot per late purchase order on a days-late axis, stacked
 * where they share a value, with reference rules at a week, two weeks and a month and the
 * band past a month shaded; then small multiples, one strip per supplier on the same scale,
 * that supplier's orders in ink over everyone else's as ghosts. Hover or focus names a dot in
 * the readout line; a click opens the order. Built only from the late list; no history is
 * invented.
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
  const refs = [7, 14, 30, 60, 90].filter((d) => d < top);
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
        <h2 className={embedded ? "text-eyebrow uppercase text-muted" : "text-section"}>How late</h2>
        <p className="text-meta text-muted">
          {plural(dated.length, "late order")}, {min === max ? `${max} days` : `${min} to ${max} days`} past their date
        </p>
      </div>
      <div ref={box} className="min-w-0">
        {width > 0 && (
          <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="block" role="img" aria-label={`Days late of ${dated.length} purchase orders, from ${min} to ${max}`} data-testid="late-strip">
            {/* past a month: Tufte's normal band, inverted */}
            {top > 30 && <rect x={x(30)} y={10} width={x(top) - x(30)} height={base - 10} className="fill-late-soft" opacity={0.45} />}
            {refs.map((d) => (
              <g key={d}>
                <line x1={x(d)} x2={x(d)} y1={10} y2={base} className="stroke-border-strong" strokeDasharray="2 3" />
                <text x={x(d) + 3} y={20} className="fill-faint text-[10px]">
                  {d === 7 ? "a week" : d === 14 ? "2 weeks" : d === 30 ? "a month" : `${d} d`}
                </text>
              </g>
            ))}
            {/* range frame: the axis spans only the data, its ends printed */}
            <line x1={x(min)} x2={x(max)} y1={base + 4} y2={base + 4} className="stroke-fg" />
            <text x={x(min)} y={base + 20} textAnchor={min === max ? "middle" : "start"} className="fill-muted text-[11px] tabular-nums">
              {min} d
            </text>
            {min !== max && (
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
          <span className="font-semibold tabular-nums text-late">{shown.days_late} days late</span>
          {shown.due_date && <span className="text-faint">needed {formatDate(shown.due_date, { short: true })}</span>}
          {shown.amount && <span className="ml-auto tabular-nums text-muted">{formatMoney(shown.amount.amount, shown.amount.currency)}</span>}
        </p>
      )}

      {suppliers.length > 1 && (
        <div className="mt-6" data-testid="late-multiples">
          <h3 className="text-eyebrow uppercase text-muted">Who is late</h3>
          <ul className="mt-2 grid grid-cols-1 gap-x-8 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
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
                    <Multiple items={dated} supplier={name} top={top} />
                  </button>
                ) : (
                  <Link href={`/late?supplier=${encodeURIComponent(name)}`} className="group block" data-testid="late-multiple">
                    <MultipleHead name={name} n={n} />
                    <Multiple items={dated} supplier={name} top={top} />
                  </Link>
                )}
              </li>
            ))}
          </ul>
          {others > 0 && <p className="mt-2 text-meta text-faint">and {plural(others, "order")} with other suppliers</p>}
        </div>
      )}
    </section>
  );
}

function MultipleHead({ name, n, on = false }: { name: string; n: number; on?: boolean }) {
  return (
    <span className="flex items-baseline justify-between gap-3 text-row">
      <span className={cn("min-w-0 truncate font-medium group-hover:text-accent group-hover:underline", on && "text-accent underline")}>{name}</span>
      <span className="shrink-0 text-meta tabular-nums text-muted">{n}</span>
    </span>
  );
}

/** One supplier's strip: its orders in ink over every late order as a ghost, on the shared scale. */
function Multiple({ items, supplier, top }: { items: LatePo[]; supplier: string; top: number }) {
  const [box, width] = useWidth<HTMLSpanElement>();
  const h = 18;
  const x = (d: number) => 3 + (d / top) * Math.max(0, width - 6);
  return (
    <span ref={box} className="mt-1 block">
      {width > 0 && (
        <svg width={width} height={h} viewBox={`0 0 ${width} ${h}`} className="block" aria-hidden>
          <line x1={0} x2={width} y1={h - 1} y2={h - 1} className="stroke-border" />
          {top > 30 && <rect x={x(30)} y={0} width={x(top) - x(30)} height={h - 1} className="fill-late-soft" opacity={0.35} />}
          {items.map((p) => (
            <rect
              key={p.object_id}
              x={x(p.days_late ?? 0) - 1}
              y={p.supplier === supplier ? 2 : 6}
              width={2}
              height={p.supplier === supplier ? h - 3 : h - 7}
              className={cn(p.supplier === supplier ? "fill-fg" : "fill-border-strong")}
              opacity={p.supplier === supplier ? 0.85 : 0.6}
            />
          ))}
        </svg>
      )}
    </span>
  );
}
