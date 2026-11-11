"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import { formatDate, formatMoney, objectHref, plural, recordLabel, safeHref } from "@/lib/format";
import type { LatePo } from "@/lib/types";
import { lateFigure } from "./ui/glyphs";

/**
 * The late list drawn: one dot per late PO on a days-late axis, stacked where they share a
 * value, coloured by how late (up to a month, to three months, past that), with rules at a
 * week, two weeks and a month; then each supplier's late POs as a bar split the same way.
 * Hover names a dot in the readout line; a click opens the PO.
 */

const R = 4;
const STEP = 2 * R + 1.5;
const PAD_L = 8;
const PAD_R = 12;

/** The three age bands, the colour each is drawn in, and the day-filter band it matches. */
export const AGE_BANDS = [
  { id: "d30", label: "Up to 30 days", short: "≤ 30 d", colour: "var(--dot-risk)", cls: "bg-dot-risk" },
  { id: "d90", label: "31 to 90 days", short: "31–90 d", colour: "color-mix(in srgb, var(--dot-risk) 40%, var(--dot-late))", cls: "bg-[color-mix(in_srgb,var(--dot-risk)_40%,var(--dot-late))]" },
  { id: "dmore", label: "Over 90 days", short: "> 90 d", colour: "var(--dot-late)", cls: "bg-dot-late" },
] as const;

export function bandOf(days: number): 0 | 1 | 2 {
  return days <= 30 ? 0 : days <= 90 ? 1 : 2;
}

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

function Title({ children, aside }: { children: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
      <h3 className="text-[12px] font-medium leading-4 text-muted">{children}</h3>
      {aside}
    </div>
  );
}

function Legend() {
  return (
    <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] leading-4 text-muted">
      {AGE_BANDS.map((b) => (
        <span key={b.id} className="inline-flex items-center gap-1.5">
          <span className={cn("size-2 rounded-full", b.cls)} aria-hidden />
          {b.short}
        </span>
      ))}
    </span>
  );
}

export function LateShape({
  items,
  embedded = false,
  onSupplier,
  picked,
  onPick,
  className,
}: {
  items: LatePo[];
  embedded?: boolean;
  onSupplier?: (name: string | null) => void;
  picked?: string | null;
  /** a dot or the readout opens the PO here instead of navigating */
  onPick?: (po: LatePo) => void;
  className?: string;
}) {
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
  const placed = useMemo(() => dodge(dated.map((p) => x(p.days_late ?? 0)), STEP, 7), [dated, plotW]); // eslint-disable-line react-hooks/exhaustive-deps
  const maxLevel = placed.length ? Math.max(...placed.map((p) => p.level)) : 0;
  const plotH = Math.max(44, (maxLevel + 1) * STEP + 16);
  const base = 18 + plotH;
  const height = base + 24;
  // reference rules whose labels would collide on a narrow screen are drawn without a label
  const refs = [7, 14, 30, 60, 90, 120, 150].filter((d) => d < top);
  const labelled = new Set<number>();
  let lastX = -Infinity;
  for (const d of refs) {
    const px = PAD_L + (d / top) * plotW;
    if (px - lastX >= 52) {
      labelled.add(d);
      lastX = px;
    }
  }
  const ticks = [0, ...refs.filter((d) => labelled.has(d)), top];
  const shown = focus ?? dated[dated.length - 1] ?? null;
  const suppliers = useMemo(() => {
    const m = new Map<string, [number, number, number]>();
    for (const p of dated) {
      if (!p.supplier) continue;
      const row = m.get(p.supplier) ?? [0, 0, 0];
      row[bandOf(p.days_late ?? 0)]++;
      m.set(p.supplier, row);
    }
    const sum = (r: number[]) => r[0] + r[1] + r[2];
    return [...m.entries()].sort((a, b) => sum(b[1]) - sum(a[1]) || b[1][2] - a[1][2] || a[0].localeCompare(b[0])).slice(0, 8);
  }, [dated]);
  const most = suppliers.length ? Math.max(...suppliers.map(([, r]) => r[0] + r[1] + r[2])) : 1;
  const others = dated.length - suppliers.reduce((n, [, r]) => n + r[0] + r[1] + r[2], 0);

  useEffect(() => {
    if (!pending) return;
    setPending(false);
    if (onPick && shown) onPick(shown);
    else go.current?.click();
  }, [pending, onPick, shown]);

  if (dated.length === 0) return null;
  return (
    <section className={cn("space-y-8", className)} aria-label="How late, drawn" data-testid="late-shape" data-embedded={embedded || undefined}>
      <div>
        <Title aside={<Legend />}>Days late</Title>
        <div ref={box} className="min-w-0">
          {width > 0 && (
            <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="block overflow-visible" role="img" aria-label={`Days late of ${dated.length} purchase orders, from ${min} to ${max}`} data-testid="late-strip">
              {refs.map((d) => (
                <g key={d}>
                  <line x1={x(d)} x2={x(d)} y1={14} y2={base} stroke="var(--border)" strokeDasharray="2 3" />
                  {labelled.has(d) && (
                    <text x={x(d) + 4} y={22} fill="var(--fg-faint)" fontSize={11}>
                      {d === 7 ? "a week" : d === 14 ? "2 weeks" : d === 30 ? "a month" : `${d / 30} months`}
                    </text>
                  )}
                </g>
              ))}
              <line x1={PAD_L} x2={width - PAD_R} y1={base + 1} y2={base + 1} stroke="var(--border-strong)" />
              {ticks.map((d, i) => (
                <text key={`t${d}`} x={x(d)} y={base + 17} textAnchor={i === 0 ? "start" : i === ticks.length - 1 ? "end" : "middle"} fill="var(--fg-muted)" fontSize={11} className="tabular-nums">
                  {i === ticks.length - 1 ? `${d} days` : d}
                </text>
              ))}
              {dated.map((p, i) => {
                const cx = x(p.days_late ?? 0) + placed[i].shift * STEP;
                const cy = base - R - 3 - placed[i].level * STEP;
                const on = shown?.object_id === p.object_id;
                const dim = picked != null && (p.supplier ?? "") !== picked;
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
                    <circle cx={cx} cy={cy} r={R} style={{ fill: AGE_BANDS[bandOf(p.days_late ?? 0)].colour }} opacity={dim ? 0.22 : 1} />
                    {on && <circle cx={cx} cy={cy} r={R + 2.75} fill="none" stroke="var(--fg)" strokeWidth={1.5} />}
                  </g>
                );
              })}
            </svg>
          )}
        </div>
        {shown && (
          <p className="mt-3 flex min-h-8 min-w-0 items-center gap-2.5 rounded-lg bg-[color-mix(in_srgb,var(--fg)_4%,transparent)] px-2.5 py-1.5 text-[13px] leading-5" aria-live="polite" data-testid="late-readout">
            <Link
              ref={go}
              href={hrefOf(shown)}
              onClick={(e) => {
                if (!onPick || e.metaKey || e.ctrlKey || e.shiftKey) return;
                e.preventDefault();
                onPick(shown);
              }}
              className="shrink-0 font-semibold hover:underline"
            >
              {recordLabel("PurchaseOrder", shown.label)}
            </Link>
            {shown.supplier && <span className="min-w-0 truncate text-muted">{shown.supplier}</span>}
            <span className={cn("ml-auto inline-flex shrink-0 items-center gap-1.5 tabular-nums", lateFigure(shown.days_late))}>
              {shown.days_late} days late
            </span>
            {shown.due_date && <span className="hidden shrink-0 text-muted sm:inline">needed {formatDate(shown.due_date, { short: true })}</span>}
            {shown.amount && !shown.amount_suspect && <span className="hidden shrink-0 num text-muted md:inline">{formatMoney(shown.amount.amount, shown.amount.currency)}</span>}
          </p>
        )}
      </div>

      {suppliers.length > 1 && (
        <div data-testid="late-multiples">
          <Title aside={others > 0 ? <span className="text-[12px] text-faint">Others: {plural(others, "PO")}</span> : undefined}>By supplier</Title>
          <ul className="-mx-2 space-y-px">
            {suppliers.map(([name, r]) => {
              const n = r[0] + r[1] + r[2];
              const head = (
                <span className="grid h-8 grid-cols-[minmax(0,9.5rem)_minmax(0,1fr)_2rem] items-center gap-2.5 px-2 text-[13px]">
                  <span className={cn("truncate", picked === name && "font-semibold")}>{name}</span>
                  <span className="flex h-2 min-w-0 gap-px" aria-hidden>
                    {r.map((c, b) =>
                      c ? <span key={b} className={cn("h-full first:rounded-l-full last:rounded-r-full", AGE_BANDS[b].cls)} style={{ width: `${(c / most) * 100}%` }} /> : null,
                    )}
                  </span>
                  <span className="text-right num">{n}</span>
                </span>
              );
              return (
                <li key={name}>
                  {onSupplier ? (
                    <button
                      type="button"
                      onClick={() => onSupplier(picked === name ? null : name)}
                      aria-pressed={picked === name}
                      className={cn("block w-full rounded-lg text-left hover:bg-hover cursor-pointer", picked === name && "bg-hover", picked && picked !== name && "opacity-50")}
                      data-testid="late-multiple"
                    >
                      {head}
                    </button>
                  ) : (
                    <Link href={`/late?supplier=${encodeURIComponent(name)}`} className="block rounded-lg hover:bg-hover" data-testid="late-multiple">
                      {head}
                    </Link>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </section>
  );
}
