"use client";

// The one chart renderer: every kind of the chart spec (operator_api/charts.py) drawn as
// plain SVG (tables and KPI tiles as HTML) from the values the server drew from a query's
// rows. Hover shows exact values; colours come from one palette for light and dark.

import { useTheme } from "next-themes";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import type { Answer } from "@/lib/types";
import { cn } from "@/lib/cn";
import {
  INK, MOVE, THRESHOLD, clip, colour, conversion, extent, full, human, inkForHex, inkOn, ramp, short, thresholdColour, ticks, waterfall, type Theme,
  dateLabel, axisLabels,
} from "./scale";

export type AskChart = NonNullable<Answer["charts"]>[number];
export interface ChartReference {
  value: number;
  label?: string;
  semantic?: boolean;
}
type Series = AskChart["series"][number];
type Tip = { x: number; y: number; title: string; rows: { colour?: string; name: string; value: string }[] } | null;

const PCT_NAME = /(?:^|_)(?:pct|percent|percentage|rate|score|sla|attainment|on_time)(?:$|_)/i;

/** Infer a default 80% target reference for single-series 0..100 percentage/score bar charts when none is passed. */
function resolveReference(
  chart: AskChart,
  left: Series[],
  stacked: boolean,
  reference?: ChartReference,
  threshold?: number,
): ChartReference | undefined {
  if (reference) return reference;
  if (threshold !== undefined && Number.isFinite(threshold)) {
    return { value: threshold, label: short(threshold), semantic: true };
  }
  if (stacked || left.length !== 1 || chart.kind !== "bar") return undefined;
  const s = left[0];
  if (!PCT_NAME.test(s.name)) return undefined;
  const vals = s.values.filter((v): v is number => v !== null && v !== undefined);
  if (vals.length < 2) return undefined;
  const max = Math.max(...vals);
  const min = Math.min(...vals);
  if (min >= 0 && max > 1 && max <= 100) {
    return { value: 80, label: "80%", semantic: true };
  }
  return undefined;
}

const FONT = "var(--font-sans), Inter, 'Segoe UI', Helvetica, Arial, sans-serif";
const W = 640;
const H = 290;
// the longest slanted x label shown whole (a person's name without colliding)
const TILT_CHARS = 22;

export function useChartTheme(): Theme {
  const { resolvedTheme } = useTheme();
  return resolvedTheme === "dark" ? "dark" : "light";
}

/** The hover box: exact values at the point under the pointer. */
function Tooltip({ tip }: { tip: Tip; theme: Theme }) {
  if (!tip) return null;
  return (
    <div
      role="tooltip"
      className="pointer-events-none absolute z-10 min-w-[152px] max-w-[280px] rounded-lg border border-border-strong bg-raised px-3 py-2 text-[12px] text-fg shadow-lg backdrop-blur-md tabular-nums"
      style={{ left: tip.x, top: tip.y }}
    >
      <div className="mb-1 font-medium text-fg">{tip.title}</div>
      {tip.rows.map((r, i) => (
        <div key={i} className="flex items-center gap-2 py-0.5 tabular-nums">
          {r.colour && (
            <svg width={8} height={8} className="shrink-0" aria-hidden>
              <rect width={8} height={8} rx={2.5} fill={r.colour} />
            </svg>
          )}
          <span className="flex-1 truncate text-muted">{r.name}</span>
          <span className="font-medium text-fg">{r.value}</span>
        </div>
      ))}
    </div>
  );
}

/** Pointer position inside the chart's box, for the tooltip. */
function useTip() {
  const box = useRef<HTMLDivElement>(null);
  const [tip, setTip] = useState<Tip>(null);
  const show = (e: React.MouseEvent, title: string, rows: NonNullable<Tip>["rows"]) => {
    const r = box.current?.getBoundingClientRect();
    if (!r) return;
    const x = e.clientX - r.left;
    setTip({ x: x > r.width - 200 ? x - 170 : x + 14, y: Math.max(0, e.clientY - r.top - 10), title, rows });
  };
  return { box, tip, show, hide: () => setTip(null) };
}

function Svg({ chart, width = W, height = H, children }: { chart: AskChart; width?: number; height?: number; children: ReactNode }) {
  return (
    <svg
      width={width}
      viewBox={`0 0 ${width} ${height}`}
      className="h-auto max-w-full"
      role="img"
      aria-label={chart.title}
      fontFamily={FONT}
      data-chart-svg=""
    >
      {children}
    </svg>
  );
}

// ---------------------------------------------------------------------- cartesian

type Plot = {
  chart: AskChart;
  labels: string[];
  series: Series[];
  theme: Theme;
  width?: number;
  height?: number;
  range?: [number, number];
  compact?: boolean;
  reference?: ChartReference;
  threshold?: number;
  onTip: ReturnType<typeof useTip>;
};

function Cartesian({ chart, labels: given0, series, theme, width = W, height: given = H, range, compact, reference, threshold, onTip }: Plot) {
  const uid = useId().replace(/:/g, "");
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const ink = INK[theme];
  const labels = axisLabels(given0);
  const spec = chart.spec ?? undefined;
  const kind = chart.kind;
  const stacked = !!spec?.stacked;
  const horizontal = !!spec?.horizontal;
  const left = series.filter((s) => s.axis !== "right");
  const right = series.filter((s) => s.axis === "right");
  const refLine = resolveReference(chart, left, stacked, reference, threshold);
  const steps = kind === "waterfall" ? waterfall(left[0]?.values ?? []) : null;
  const names = steps ? [...labels, "Total"] : labels;
  const n = Math.max(1, names.length);
  const [rawLo, rawHi] = range ?? (steps ? [Math.min(0, ...steps.map((s) => Math.min(s.from, s.to))), Math.max(0, ...steps.map((s) => Math.max(s.from, s.to)))] : extent(left, stacked || (kind === "area" && stacked)));
  const lo0 = refLine ? Math.min(rawLo, refLine.value) : rawLo;
  const hi0 = refLine ? Math.max(rawHi, refLine.value) : rawHi;
  const hasVerticalBarLabels = !horizontal && !compact && !steps && !stacked && kind !== "line" && kind !== "area" && left.length === 1 && n <= 16;
  const hiForTicks = hasVerticalBarLabels && !range && hi0 > 0 ? hi0 * 1.18 : hi0;
  const axis = ticks(lo0, hiForTicks, compact ? 3 : 5);
  const [lo, hi] = [axis[0], axis[axis.length - 1]];
  const longest = Math.max(0, ...names.map((l) => l.length));
  const rotate = !horizontal && (n > 8 || longest * n * 6.2 > width - 80);
  // a slanted label reads whole up to TILT_CHARS (a person's full name); the bottom margin is
  // what the longest one needs, and the left one keeps every label inside the chart
  const tilt = compact ? 12 : TILT_CHARS;
  const charPx = compact ? 6.6 : 7.4;
  const slant = Math.min(longest, tilt) * charPx * Math.SQRT1_2;
  const padT = hasVerticalBarLabels ? 22 : 10;
  const padR = right.length ? 46 : horizontal ? 36 : 16;
  const hMaxChars = compact ? 18 : 26;
  let padL = horizontal ? Math.min(Math.floor(width * 0.4), 18 + Math.min(longest, hMaxChars) * 7.6) : compact ? 34 : 52;
  const hLabelChars = horizontal ? Math.max(10, Math.floor((padL - 14) / 7.6)) : 24;
  if (rotate && !horizontal) {
    for (let i = 0; i < n; i++) {
      const reach = Math.min((names[i] ?? "").length, tilt) * charPx * Math.SQRT1_2;
      const frac = (i + 0.5) / n;
      if (frac < 0.9) {
        const need = (10 + reach - (width - padR) * frac) / (1 - frac);
        if (need > padL) padL = Math.ceil(need);
      }
    }
    padL = Math.min(padL, Math.floor(width * 0.35));
  }
  const padB = horizontal ? 24 : rotate ? Math.ceil(Math.min(slant, 155) + 28) : 24;
  const pw = width - padL - padR;
  // the slanted names take their room below the plot, not out of it
  const height = given + (rotate ? Math.max(0, padB - (compact ? 40 : 64)) : 0);
  const ph = height - padB - padT;
  const along = horizontal ? ph : pw;
  const across = horizontal ? pw : ph;
  const band = along / n;
  const pos = (v: number) => ((v - lo) / (hi - lo || 1)) * across;
  const at = (v: number) => padT + ph - pos(v);
  const zero = pos(Math.min(Math.max(0, lo), hi));
  // slanted labels need ~18 px apart along the axis to stay clear of each other
  const every = Math.max(1, rotate && !compact ? Math.ceil(18 / band) : (horizontal ? Math.ceil(13 / band) : Math.ceil(n / (compact ? 6 : 16))));
  const rAxis = right.length ? ticks(...extent(right), compact ? 3 : 5) : null;
  const rpos = (v: number) => (rAxis ? ((v - rAxis[0]) / (rAxis[rAxis.length - 1] - rAxis[0] || 1)) * ph : 0);

  const rect = (i: number, a: number, b: number, fill: string, slot = 0, slots = 1, key = "") => {
    // thicker, punchier Genie-grade bars while keeping a single bar from becoming a slab
    const inner = kind === "histogram" ? band * 0.98 : Math.min(band * 0.80, 56 * slots);
    const t = inner / slots;
    const start = band * i + (band - inner) / 2 + t * slot;
    const [p, q] = [pos(Math.min(a, b)), pos(Math.max(a, b))];
    // crisp 4px radius on plain bars; stacked and histogram stay square
    const square = stacked || kind === "histogram";
    const fillOpacity = hoverIdx !== null && hoverIdx !== i ? 0.55 : 1;
    return horizontal ? (
      <rect
        key={key}
        className="chart-bar-h transition-opacity duration-150"
        rx={square ? undefined : 4}
        ry={square ? undefined : 4}
        x={padL + p}
        y={padT + start}
        width={Math.max(q - p, 0.5)}
        height={Math.max(t - (slots > 1 ? 1 : 0), 2)}
        fill={fill}
        fillOpacity={fillOpacity}
      />
    ) : (
      <rect
        key={key}
        className="chart-bar transition-opacity duration-150"
        rx={square ? undefined : 4}
        ry={square ? undefined : 4}
        x={padL + start}
        y={padT + ph - q}
        width={Math.max(t - (slots > 1 ? 1 : 0), 2)}
        height={Math.max(q - p, 0.5)}
        fill={fill}
        fillOpacity={fillOpacity}
      />
    );
  };

  const defs: ReactNode[] = [];
  const marks: ReactNode[] = [];
  if (steps) {
    const move = MOVE[theme];
    steps.forEach((s, i) => {
      marks.push(rect(i, s.from, s.to, s.total ? move.total : s.to >= s.from ? move.up : move.down, 0, 1, `w${i}`));
      if (i < steps.length - 1) {
        const y = padT + ph - pos(s.to);
        marks.push(<line key={`c${i}`} x1={padL + band * (i + 0.88)} x2={padL + band * (i + 1.12)} y1={y} y2={y} stroke={ink.axis} strokeWidth={1} />);
      }
    });
  } else if (kind === "line" || kind === "area") {
    const base = new Array(names.length).fill(0);
    left.forEach((s, si) => {
      const c = colour(si, theme);
      const pts = s.values.map((v, i) => {
        if (v === null || v === undefined) return null;
        const top = stacked ? base[i] + v : v;
        const bottom = stacked ? base[i] : Math.min(Math.max(0, lo), hi);
        if (stacked) base[i] += v;
        return { x: padL + band * (i + 0.5), y: padT + ph - pos(top), y0: padT + ph - pos(bottom) };
      }).filter((p): p is { x: number; y: number; y0: number } => p !== null);
      if (pts.length > 1) {
        const gradId = `${uid}-area-${si}`;
        defs.push(
          <linearGradient key={gradId} id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={c} stopOpacity={kind === "area" ? (stacked ? 0.45 : 0.28) : 0.2} />
            <stop offset="100%" stopColor={c} stopOpacity={stacked ? 0.08 : 0} />
          </linearGradient>,
        );
        const d = `M${pts.map((p) => `${p.x},${p.y}`).join("L")}L${[...pts].reverse().map((p) => `${p.x},${p.y0}`).join("L")}Z`;
        marks.push(<path key={`a${si}`} d={d} fill={`url(#${gradId})`} stroke="none" className="pointer-events-none" />);
      }
      marks.push(<polyline key={`l${si}`} className="chart-line" pathLength={1} fill="none" stroke={c} strokeWidth={compact ? 1.8 : 2.5} strokeLinecap="round" strokeLinejoin="round" points={pts.map((p) => `${p.x},${p.y}`).join(" ")} />);
      if (pts.length <= 40) pts.forEach((p, i) => marks.push(<circle key={`p${si}-${i}`} className="chart-value" cx={p.x} cy={p.y} r={compact ? 2 : (i === pts.length - 1 ? 4.5 : 3)} fill={c} stroke={!compact ? ink.ground : undefined} strokeWidth={!compact ? 1.5 : undefined} />));
      // the latest point is marked larger, with its value, on a single-series line
      const last = pts[pts.length - 1];
      const lastV = [...s.values].reverse().find((v) => v !== null && v !== undefined);
      if (!compact && !stacked && left.length === 1 && last && lastV !== undefined && lastV !== null && last.y > padT + 14) {
        marks.push(<text key={`lv${si}`} className="chart-value" x={Math.min(last.x, padL + pw - 14)} y={last.y - 10} textAnchor="middle" fontSize={12} fontWeight={600} fill={ink.text}>{short(lastV)}</text>);
      }
    });
  } else {
    const pos0 = new Array(names.length).fill(0);
    const neg0 = new Array(names.length).fill(0);
    const useSemantic = Boolean(refLine?.semantic && !stacked && left.length === 1);
    left.forEach((s, si) => {
      const seriesColor = colour(si, theme);
      s.values.forEach((v, i) => {
        if (v === null || v === undefined) return;
        const c = useSemantic && refLine ? thresholdColour(v, refLine.value, theme) : seriesColor;
        if (stacked) {
          const base = v >= 0 ? pos0 : neg0;
          marks.push(rect(i, base[i], base[i] + v, c, 0, 1, `b${si}-${i}`));
          base[i] += v;
        } else {
          marks.push(rect(i, 0, v, c, si, left.length, `b${si}-${i}`));
          // crisp bold value labels: rendered inside wide horizontal bars (high-contrast ink) or cleanly above vertical bars
          const room = across - pos(v);
          const barSpan = Math.abs(pos(v) - zero);
          if (!compact && left.length === 1 && n <= 16 && v >= 0) {
            const mid = band * i + band / 2;
            const labelText = short(v);
            if (horizontal) {
              const inside = barSpan >= Math.max(46, labelText.length * 8 + 16) && band * 0.80 >= 15;
              if (inside) {
                marks.push(
                  <text key={`v${i}`} className="chart-value tabular-nums" x={padL + pos(v) - 8} y={padT + mid + 4} textAnchor="end" fontSize={11.5} fontWeight={700} fill={inkForHex(c)}>
                    {labelText}
                  </text>,
                );
              } else if (room > 26) {
                marks.push(
                  <text key={`v${i}`} className="chart-value tabular-nums" x={padL + pos(v) + 6} y={padT + mid + 4} textAnchor="start" fontSize={11.5} fontWeight={600} fill={ink.text}>
                    {labelText}
                  </text>,
                );
              }
            } else if (room + padT > 18) {
              marks.push(
                <text key={`v${i}`} className="chart-value tabular-nums" x={padL + mid} y={Math.max(12, padT + ph - pos(v) - 6)} textAnchor="middle" fontSize={11.5} fontWeight={600} fill={ink.text}>
                  {labelText}
                </text>,
              );
            }
          }
        }
      });
    });
  }
  right.forEach((s, k) => {
    const c = colour(left.length + k, theme);
    const pts = s.values.map((v, i) => (v === null || v === undefined ? null : `${padL + band * (i + 0.5)},${padT + ph - rpos(v)}`)).filter(Boolean);
    marks.push(<polyline key={`r${k}`} className="chart-line" pathLength={1} fill="none" stroke={c} strokeWidth={2.5} strokeLinecap="round" strokeLinejoin="round" points={pts.join(" ")} />);
    s.values.forEach((v, i) => v !== null && v !== undefined && marks.push(<circle key={`rp${k}-${i}`} cx={padL + band * (i + 0.5)} cy={padT + ph - rpos(v)} r={3.2} fill={ink.ground} stroke={c} strokeWidth={2} />));
  });

  // Optional dashed reference / threshold line with clean badge (uses <path> to stay cleanly distinct from gridlines)
  let referenceMark: ReactNode = null;
  if (refLine && !compact && refLine.value >= lo && refLine.value <= hi) {
    const refColor = refLine.semantic ? THRESHOLD[theme].ok : ink.muted;
    const badgeText = refLine.label ?? short(refLine.value);
    const badgeW = Math.max(34, badgeText.length * 6.8 + 12);
    if (horizontal) {
      const rx = padL + pos(refLine.value);
      const bx = Math.min(Math.max(padL + 2, rx - badgeW / 2), padL + pw - badgeW);
      referenceMark = (
        <g data-reference-line="">
          <path d={`M ${rx} ${padT} L ${rx} ${padT + ph}`} fill="none" stroke={refColor} strokeWidth={1.5} strokeDasharray="5 4" strokeOpacity={0.9} />
          <rect x={bx} y={1} width={badgeW} height={16} rx={4} fill={ink.ground} stroke={refColor} strokeWidth={1} />
          <text x={bx + badgeW / 2} y={12} textAnchor="middle" fontSize={10} fontWeight={600} fill={refColor} className="tabular-nums">
            {badgeText}
          </text>
        </g>
      );
    } else {
      const ry = at(refLine.value);
      referenceMark = (
        <g data-reference-line="">
          <path d={`M ${padL} ${ry} L ${padL + pw} ${ry}`} fill="none" stroke={refColor} strokeWidth={1.5} strokeDasharray="5 4" strokeOpacity={0.9} />
          <rect x={padL + pw - badgeW - 4} y={Math.max(2, ry - 18)} width={badgeW} height={16} rx={4} fill={ink.ground} stroke={refColor} strokeWidth={1} />
          <text x={padL + pw - badgeW / 2 - 4} y={Math.max(13, ry - 7)} textAnchor="middle" fontSize={10} fontWeight={600} fill={refColor} className="tabular-nums">
            {badgeText}
          </text>
        </g>
      );
    }
  }

  const hover = (i: number) => (e: React.MouseEvent) => {
    const rows = steps
      ? [{ colour: undefined, name: steps[i].total ? "Total" : "Change", value: full(steps[i].total ? steps[i].to : steps[i].to - steps[i].from, chart.currency) },
         ...(steps[i].total ? [] : [{ colour: undefined, name: "Running total", value: full(steps[i].to, chart.currency) }])]
      : series.map((s) => ({
          colour: refLine?.semantic && !stacked && left.length === 1 && s.values[i] !== null && s.values[i] !== undefined
            ? thresholdColour(s.values[i] as number, refLine.value, theme)
            : colour(s.axis === "right" ? left.length + right.indexOf(s) : left.indexOf(s), theme),
          name: human(s.name),
          value: full(s.values[i], chart.currency),
        }));
    onTip.show(e, names[i], rows);
  };

  return (
    <Svg chart={chart} width={width} height={height}>
      {defs.length > 0 && <defs>{defs}</defs>}
      {axis.map((t) => (horizontal ? (
        <g key={t}>
          <text x={padL + pos(t)} y={height - 6} textAnchor="middle" fontSize={11} fill={ink.muted} className="tabular-nums">{short(t)}</text>
        </g>
      ) : (
        <g key={t}>
          <line x1={padL} x2={padL + pw} y1={at(t)} y2={at(t)} stroke={ink.grid} strokeWidth={1} opacity={0.7} strokeDasharray="3 3" />
          <text x={padL - 6} y={at(t) + 4} textAnchor="end" fontSize={compact ? 10 : 11} fill={ink.muted} className="tabular-nums">{short(t)}</text>
        </g>
      )))}
      {rAxis?.map((t) => (
        <text key={`r${t}`} x={width - padR + 6} y={padT + ph - rpos(t) + 4} fontSize={11} fill={ink.muted}>{short(t)}</text>
      ))}
      {horizontal ? (
        <line x1={padL + zero} x2={padL + zero} y1={padT} y2={padT + ph} stroke={ink.axis} strokeWidth={1} />
      ) : (
        <line x1={padL} x2={padL + pw} y1={padT + ph - zero} y2={padT + ph - zero} stroke={ink.axis} strokeWidth={1} />
      )}
      {marks}
      {referenceMark}
      {names.map((l, i) => (i % every ? null : horizontal ? (
        <text key={`x${i}`} x={padL - 8} y={padT + band * (i + 0.5) + 4} textAnchor="end" fontSize={11.5} fill={ink.text}>
          <title>{l}</title>
          {clip(l, hLabelChars)}
        </text>
      ) : (
        <text
          key={`x${i}`}
          x={padL + band * (i + 0.5)}
          y={padT + ph + 15}
          fontSize={compact ? 10 : 11}
          fill={ink.muted}
          textAnchor={rotate ? "end" : "middle"}
          transform={rotate ? `rotate(-45 ${padL + band * (i + 0.5)} ${padT + ph + 15})` : undefined}
        >
          {clip(l, rotate ? tilt : compact ? 10 : 18)}
          {l.length > (rotate ? tilt : compact ? 10 : 18) && <title>{l}</title>}
        </text>
      )))}
      {names.map((l, i) => (
        <rect
          key={`h${i}`}
          data-hover=""
          x={horizontal ? padL : padL + band * i}
          y={horizontal ? padT + band * i : padT}
          width={horizontal ? pw : band}
          height={horizontal ? band : ph}
          fill="transparent"
          onMouseMove={(e) => { setHoverIdx(i); hover(i)(e); }}
          onMouseLeave={() => { setHoverIdx(null); onTip.hide(); }}
        />
      ))}
    </Svg>
  );
}

// ---------------------------------------------------------------------- other kinds

function Funnel({ chart, theme, onTip }: { chart: AskChart; theme: Theme; onTip: ReturnType<typeof useTip> }) {
  const ink = INK[theme];
  const values = chart.series[0]?.values ?? [];
  const max = Math.max(1e-9, ...values.map((v) => v ?? 0));
  const conv = conversion(values);
  const row = 34;
  const height = Math.max(row * values.length + 8, 60);
  return (
    <Svg chart={chart} height={height}>
      {values.map((v, i) => {
        const w = ((v ?? 0) / max) * 360;
        const y = 4 + i * row;
        return (
          <g key={i} onMouseMove={(e) => onTip.show(e, chart.labels[i], [
            { name: human(chart.series[0].name), value: full(v, chart.currency) },
            ...(conv[i].ofPrev !== null ? [{ name: "Of the step before", value: `${Math.round((conv[i].ofPrev ?? 0) * 100)}%` }] : []),
            ...(conv[i].ofFirst !== null && i > 0 ? [{ name: "Of the first step", value: `${Math.round((conv[i].ofFirst ?? 0) * 100)}%` }] : []),
          ])} onMouseLeave={onTip.hide}>
            <text x={150} y={y + row / 2 + 4} textAnchor="end" fontSize={12} fill={ink.text}>{clip(chart.labels[i], 22)}</text>
            <rect x={160 + (360 - w) / 2} y={y + 3} width={Math.max(w, 1)} height={row - 8} rx={3} ry={3} className="transition-opacity duration-150 hover:opacity-90" fill={colour(0, theme)} fillOpacity={1 - i * (0.5 / Math.max(1, values.length))} />
            <text x={530} y={y + row / 2 + 4} fontSize={12} fill={ink.text} className="tabular-nums">
              {short(v ?? 0)}
              {i > 0 && conv[i].ofPrev !== null ? <tspan fill={ink.muted}>{`  ${Math.round((conv[i].ofPrev ?? 0) * 100)}%`}</tspan> : null}
            </text>
          </g>
        );
      })}
    </Svg>
  );
}

function Pie({ chart, theme, onTip }: { chart: AskChart; theme: Theme; onTip: ReturnType<typeof useTip> }) {
  const ink = INK[theme];
  const values = (chart.series[0]?.values ?? []).map((v) => (v && v > 0 ? v : 0));
  const total = values.reduce((a, b) => a + b, 0) || 1;
  const [cx, cy, r] = [130, 130, 112];
  const hole = chart.kind === "donut" ? r * 0.58 : 0;
  let angle = -Math.PI / 2;
  const arc = (a0: number, a1: number) => {
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const p = (a: number, rr: number) => `${cx + rr * Math.cos(a)},${cy + rr * Math.sin(a)}`;
    if (a1 - a0 >= Math.PI * 2 - 1e-6) a1 = a0 + Math.PI * 2 - 1e-4;
    return hole
      ? `M${p(a0, r)} A${r},${r} 0 ${large} 1 ${p(a1, r)} L${p(a1, hole)} A${hole},${hole} 0 ${large} 0 ${p(a0, hole)} Z`
      : `M${cx},${cy} L${p(a0, r)} A${r},${r} 0 ${large} 1 ${p(a1, r)} Z`;
  };
  return (
    <Svg chart={chart} height={262}>
      {values.map((v, i) => {
        const a0 = angle;
        angle += (v / total) * Math.PI * 2;
        return v > 0 ? (
          <path key={i} d={arc(a0, angle)} fill={colour(i, theme)} stroke={ink.ground} strokeWidth={1.5} className="transition-opacity duration-150 hover:opacity-90"
            onMouseMove={(e) => onTip.show(e, chart.labels[i], [{ colour: colour(i, theme), name: `${Math.round((v / total) * 1000) / 10}%`, value: full(chart.series[0].values[i], chart.currency) }])}
            onMouseLeave={onTip.hide} />
        ) : null;
      })}
      {hole > 0 && (
        <>
          <text x={cx} y={cy + 2} textAnchor="middle" fontSize={20} fontWeight={600} fill={ink.text} className="tabular-nums">{short(total)}</text>
          <text x={cx} y={cy + 20} textAnchor="middle" fontSize={11} fill={ink.muted}>{chart.currency ? `total ${chart.currency}` : "total"}</text>
        </>
      )}
      {chart.labels.map((l, i) => (
        <g key={`k${i}`}>
          <rect x={290} y={18 + i * 26} width={10} height={10} rx={2} fill={colour(i, theme)} />
          <text x={308} y={27 + i * 26} fontSize={12} fill={ink.text}>{clip(l, 30)}</text>
          <text x={630} y={27 + i * 26} fontSize={12} fill={ink.muted} textAnchor="end" className="tabular-nums">
            {`${Math.round((values[i] / total) * 1000) / 10}%  ${short(values[i])}`}
          </text>
        </g>
      ))}
    </Svg>
  );
}

function Scatter({ chart, theme, onTip }: { chart: AskChart; theme: Theme; onTip: ReturnType<typeof useTip> }) {
  const ink = INK[theme];
  const xs = chart.xs ?? [];
  const known = xs.filter((v): v is number => v !== null && v !== undefined);
  const xa = ticks(Math.min(...known), Math.max(...known));
  const [ylo, yhi] = extent(chart.series);
  const yVals = chart.series.flatMap((s) => s.values.filter((v): v is number => v != null));
  const ya = ticks(Math.min(ylo, ...yVals), yhi);
  const [padL, padB, padR, padT] = [52, 42, 16, 18];
  const pw = W - padL - padR;
  const ph = H - padB - padT;
  const X = (v: number) => padL + ((v - xa[0]) / (xa[xa.length - 1] - xa[0] || 1)) * pw;
  const Y = (v: number) => padT + ph - ((v - ya[0]) / (ya[ya.length - 1] - ya[0] || 1)) * ph;
  const trend = chart.trend;

  // Quadrant mean crosshairs (Avg X, Avg Y) across paired valid points
  const pairs: { x: number; y: number }[] = [];
  for (const s of chart.series) {
    s.values.forEach((v, i) => {
      const xv = xs[i];
      if (v !== null && v !== undefined && xv !== null && xv !== undefined) pairs.push({ x: xv, y: v });
    });
  }
  const avgX = pairs.length >= 2 ? pairs.reduce((sum, p) => sum + p.x, 0) / pairs.length : null;
  const avgY = pairs.length >= 2 ? pairs.reduce((sum, p) => sum + p.y, 0) / pairs.length : null;
  const avgXLabel = avgX !== null ? `Avg X: ${short(avgX)}` : "";
  const avgYLabel = avgY !== null ? `Avg Y: ${short(avgY)}` : "";

  return (
    <Svg chart={chart}>
      {ya.map((t) => (
        <g key={`y${t}`}>
          <line x1={padL} x2={padL + pw} y1={Y(t)} y2={Y(t)} stroke={ink.grid} strokeWidth={1} strokeOpacity={theme === "dark" ? 0.85 : 0.75} />
          <text x={padL - 6} y={Y(t) + 4} textAnchor="end" fontSize={11} fill={ink.muted} className="tabular-nums">{short(t)}</text>
        </g>
      ))}
      {xa.map((t) => (
        <text key={`x${t}`} x={X(t)} y={padT + ph + 16} textAnchor="middle" fontSize={11} fill={ink.muted} className="tabular-nums">{short(t)}</text>
      ))}
      <text x={padL + pw} y={H - 4} textAnchor="end" fontSize={11} fill={ink.muted}>{human(chart.x)}</text>
      {avgX !== null && avgY !== null && (
        <g data-quadrant-crosshair="">
          <path
            d={`M ${X(avgX)} ${padT} L ${X(avgX)} ${padT + ph}`}
            fill="none"
            stroke={ink.muted}
            strokeOpacity={0.6}
            strokeWidth={1.25}
            strokeDasharray="4 4"
          />
          <path
            d={`M ${padL} ${Y(avgY)} L ${padL + pw} ${Y(avgY)}`}
            fill="none"
            stroke={ink.muted}
            strokeOpacity={0.6}
            strokeWidth={1.25}
            strokeDasharray="4 4"
          />
          <rect
            x={Math.min(Math.max(padL + 2, X(avgX) - (avgXLabel.length * 3.4 + 6)), padL + pw - (avgXLabel.length * 6.8 + 12))}
            y={1}
            width={avgXLabel.length * 6.8 + 12}
            height={15}
            rx={3}
            fill={ink.ground}
            stroke={ink.axis}
            strokeWidth={1}
          />
          <text
            x={Math.min(Math.max(padL + 2 + (avgXLabel.length * 3.4 + 6), X(avgX)), padL + pw - (avgXLabel.length * 3.4 + 6))}
            y={11.5}
            textAnchor="middle"
            fontSize={10}
            fontWeight={600}
            fill={ink.muted}
            className="tabular-nums"
          >
            {avgXLabel}
          </text>
          <rect
            x={padL + pw - (avgYLabel.length * 6.8 + 12) - 4}
            y={Math.max(padT + 2, Y(avgY) - 18)}
            width={avgYLabel.length * 6.8 + 12}
            height={15}
            rx={3}
            fill={ink.ground}
            stroke={ink.axis}
            strokeWidth={1}
          />
          <text
            x={padL + pw - (avgYLabel.length * 3.4 + 6) - 4}
            y={Math.max(padT + 12.5, Y(avgY) - 7.5)}
            textAnchor="middle"
            fontSize={10}
            fontWeight={600}
            fill={ink.muted}
            className="tabular-nums"
          >
            {avgYLabel}
          </text>
        </g>
      )}
      {chart.series.map((s, si) => s.values.map((v, i) => (v === null || v === undefined || xs[i] === null || xs[i] === undefined ? null : (
        <circle
          key={`${si}-${i}`}
          cx={X(xs[i] as number)}
          cy={Y(v)}
          r={6.5}
          fill={colour(si, theme)}
          fillOpacity={0.68}
          stroke={colour(si, theme)}
          strokeOpacity={0.95}
          strokeWidth={1.25}
          onMouseMove={(e) => onTip.show(e, chart.labels[i], [{ name: human(chart.x), value: full(xs[i]) }, { colour: colour(si, theme), name: human(s.name), value: full(v, chart.currency) }])}
          onMouseLeave={onTip.hide}
        />
      ))))}
      {trend && (
        <line x1={X(xa[0])} x2={X(xa[xa.length - 1])} y1={Y(trend.slope * xa[0] + trend.intercept)} y2={Y(trend.slope * xa[xa.length - 1] + trend.intercept)}
          stroke={ink.text} strokeOpacity={0.65} strokeWidth={1.75} strokeDasharray="5 4" />
      )}
    </Svg>
  );
}

function Heatmap({ chart, theme, onTip }: { chart: AskChart; theme: Theme; onTip: ReturnType<typeof useTip> }) {
  const uid = useId().replace(/:/g, "");
  const heatKeyId = `${uid}-heat-key`;
  const ink = INK[theme];
  const cells = chart.series.flatMap((s) => s.values.filter((v): v is number => v !== null && v !== undefined));
  const [lo, hi] = [Math.min(...cells), Math.max(...cells)];
  const mid = (lo + hi) / 2;
  const cols = chart.labels.length;
  const rows = chart.series.length;
  const padL = Math.min(160, 12 + Math.max(...chart.series.map((s) => Math.min(24, s.name.length))) * 6.6);
  const scaleRailW = 64;
  const cw = Math.max(10, Math.min(56, (W - padL - scaleRailW - 12) / Math.max(cols, 1)));
  const ch = Math.max(16, Math.min(30, 340 / Math.max(rows, 1)));
  const gridH = ch * rows;
  const height = Math.max(116, 10 + gridH + 56);
  const width = Math.max(W, padL + cw * cols + scaleRailW + 12);
  const every = Math.max(1, Math.ceil((cols * 12) / (cw * cols)));
  const barX = padL + cw * cols + 16;
  const barH = Math.max(64, Math.min(gridH, 160));
  const barY = 10 + Math.max(0, (gridH - barH) / 2);
  return (
    <Svg chart={chart} width={width} height={height}>
      {chart.series.map((s, r) => (
        <g key={r}>
          <text x={padL - 8} y={10 + ch * r + ch / 2 + 4} textAnchor="end" fontSize={11.5} fill={ink.text}>{clip(s.name, 24)}</text>
          {s.values.map((v, c) => (
            <rect
              key={c}
              x={padL + cw * c}
              y={10 + ch * r}
              width={cw - 2}
              height={ch - 2}
              rx={3}
              fill={v === null || v === undefined ? ink.grid : ramp(v, lo, hi, theme)}
              onMouseMove={(e) => onTip.show(e, `${s.name} · ${chart.labels[c]}`, [{ name: human(chart.spec?.y?.[0] ?? "value"), value: full(v, chart.currency) }])}
              onMouseLeave={onTip.hide}
            />
          ))}
          {cw >= 26 && ch >= 15 && s.values.map((v, c) => (v === null || v === undefined ? null : (
            <text
              key={`n${c}`}
              x={padL + cw * c + (cw - 2) / 2}
              y={10 + ch * r + (ch - 2) / 2 + 3.5}
              textAnchor="middle"
              fontSize={10.5}
              fontWeight={600}
              pointerEvents="none"
              className="tabular-nums"
              fill={inkOn(v, lo, hi, theme)}
            >
              {short(v)}
            </text>
          )))}
        </g>
      ))}
      {chart.labels.map((l, c) => (c % every ? null : (
        <text key={c} x={padL + cw * (c + 0.5)} y={10 + gridH + 14} fontSize={10.5} fill={ink.muted} textAnchor="end"
          transform={`rotate(-40 ${padL + cw * (c + 0.5)} ${10 + gridH + 14})`}>{clip(l, 12)}</text>
      )))}
      <defs>
        <linearGradient id={heatKeyId} x1="0" y1="1" x2="0" y2="0">
          <stop offset="0%" stopColor={ramp(lo, lo, hi, theme)} />
          <stop offset="50%" stopColor={ramp(mid, lo, hi, theme)} />
          <stop offset="100%" stopColor={ramp(hi, lo, hi, theme)} />
        </linearGradient>
      </defs>
      <g data-heatmap-scale="">
        <rect x={barX} y={barY} width={10} height={barH} rx={3} fill={`url(#${heatKeyId})`} stroke={ink.axis} strokeWidth={0.75} />
        <text x={barX + 16} y={barY + 8} fontSize={10} fontWeight={500} fill={ink.muted} className="tabular-nums">{short(hi)}</text>
        <text x={barX + 16} y={barY + barH / 2 + 3.5} fontSize={10} fill={ink.muted} className="tabular-nums">{short(mid)}</text>
        <text x={barX + 16} y={barY + barH} fontSize={10} fontWeight={500} fill={ink.muted} className="tabular-nums">{short(lo)}</text>
      </g>
    </Svg>
  );
}

function ColourTable({ chart, theme }: { chart: AskChart; theme: Theme }) {
  const ranges = chart.series.map((s) => {
    const v = s.values.filter((x): x is number => x !== null && x !== undefined);
    return [Math.min(...v), Math.max(...v)] as [number, number];
  });
  return (
    <div className="max-h-[420px] overflow-auto">
      <table className="w-full border-collapse text-[12px] tabular-nums">
        <thead className="sticky top-0 bg-bg">
          <tr className="border-b border-border text-left text-muted">
            <th className="px-2 py-1.5 font-medium">{human(chart.x)}</th>
            {chart.series.map((s) => <th key={s.name} className="px-2 py-1.5 text-right font-medium">{human(s.name)}</th>)}
          </tr>
        </thead>
        <tbody>
          {chart.labels.map((l, i) => (
            <tr key={i} className="border-t border-border/70 first:border-t-0">
              <td className="px-2 py-1.5">{l}</td>
              {chart.series.map((s, si) => {
                const v = s.values[i];
                const [lo, hi] = ranges[si];
                const known = v !== null && v !== undefined;
                return (
                  <td key={si} className="px-2 py-1.5 text-right whitespace-nowrap" title={full(v, chart.currency)}
                    style={known ? { background: ramp(v, lo, hi, theme), color: inkOn(v, lo, hi, theme) } : undefined}>
                    {known ? full(v) : ""}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Kpis({ chart, theme }: { chart: AskChart; theme: Theme }) {
  const ink = INK[theme];
  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(170px,1fr))] gap-x-6 gap-y-4">
      {chart.series.map((s, si) => {
        // the latest value and the one before it, by their place on the axis (a null skipped)
        const at = s.values.flatMap((v, i) => (v === null || v === undefined ? [] : [i]));
        const vals = at.map((i) => s.values[i] as number);
        const now = vals[vals.length - 1];
        const before = vals.length > 1 ? vals[vals.length - 2] : null;
        const change = before !== null && now !== undefined ? now - before : null;
        const nowLabel = at.length ? chart.labels[at[at.length - 1]] : undefined;
        const beforeLabel = at.length > 1 ? chart.labels[at[at.length - 2]] : undefined;
        const [lo, hi] = [Math.min(...vals), Math.max(...vals)];
        return (
          <div key={s.name} className="pt-0" data-testid="kpi-tile">
            <svg className="mb-2 block h-0.5 w-full" viewBox="0 0 100 2" preserveAspectRatio="none" aria-hidden>
              <rect width={100} height={2} fill={colour(si, theme)} />
            </svg>
            <div className="text-[12px] text-muted">{human(s.name)}</div>
            <div className="text-[26px] font-semibold leading-8 tabular-nums" title={full(now, chart.currency)}>
              {now === undefined ? "–" : full(now)}
              {chart.currency && <span className="ml-1 text-[13px] font-normal text-muted">{chart.currency}</span>}
            </div>
            {change !== null && (
              <div className="text-[12px] tabular-nums text-muted">
                {`${change >= 0 ? "▲" : "▼"} ${full(Math.abs(change))}`}
                {before ? ` (${change >= 0 ? "+" : "−"}${Math.abs(Math.round((change / before) * 1000) / 10)}%)` : ""}
                {nowLabel !== undefined && beforeLabel !== undefined
                  ? `, ${dateLabel(nowLabel)}${nowLabel === chart.open_period ? " so far" : ""} vs ${dateLabel(beforeLabel)}${beforeLabel === chart.first_period ? " (part)" : ""}`
                  : " vs the row before"}
              </div>
            )}
            {vals.length > 2 && (
              <svg viewBox="0 0 120 28" className="mt-1 h-7 w-[120px]" aria-hidden>
                <polyline fill="none" stroke={colour(si, theme)} strokeWidth={1.5}
                  points={vals.map((v, i) => `${(i / (vals.length - 1)) * 118 + 1},${27 - ((v - lo) / (hi - lo || 1)) * 25}`).join(" ")} />
              </svg>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------- the frame

/** The series legend: a square in each series' colour and its name. */
/** What names a chart's colours: its series and their colours, or (one series, no legend)
 * a line saying what the axes are. Shared by the screen and the PNG. */
export function chartKey(chart: AskChart, theme: Theme): { items: { name: string; colour: string }[]; caption: string } {
  const names = chart.panels?.[0]?.series ?? chart.series;
  const none = { items: [], caption: "" };
  if (["pie", "donut", "heatmap", "table", "kpi", "funnel"].includes(chart.kind)) return none;
  if (names.length < 2 || chart.kind === "waterfall") {
    const one = names[0];
    if (!one) return none;
    const by = (chart.x ?? "").replace(/_/g, " ").trim();
    const caption = chart.kind === "scatter" ? `Each dot is one row: ${by} along the bottom, ${human(one.name).toLowerCase()} up the side. Point at a dot for its name.`
      : chart.kind === "histogram" ? `Rows by ${human(one.name).toLowerCase()}`
      : human(one.name);
    return { items: [], caption };
  }
  const left = names.filter((s) => s.axis !== "right");
  const right = names.filter((s) => s.axis === "right");
  return {
    items: names.map((s) => ({
      name: `${human(s.name)}${s.axis === "right" ? " (scale on the right)" : ""}`,
      colour: colour(s.axis === "right" ? left.length + right.indexOf(s) : left.indexOf(s), theme),
    })),
    caption: "",
  };
}

function Legend({ chart, theme }: { chart: AskChart; theme: Theme }) {
  const { items, caption } = chartKey(chart, theme);
  if (caption) return <div className="text-[12px] text-muted" data-testid="axis-caption">{caption}</div>;
  if (!items.length) return null;
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-muted">
      {items.map((s) => (
        <span key={s.name} className="inline-flex items-center gap-1.5">
          <svg width={10} height={10} className="shrink-0" aria-hidden>
            <rect width={10} height={10} rx={2} fill={s.colour} />
          </svg>
          {s.name}
        </span>
      ))}
    </div>
  );
}

/** What the chart is drawn from and what it leaves out, in a line. */
export function chartNotes(chart: AskChart, opts?: { includeAggregation?: boolean }): string[] {
  const out: string[] = [];
  if (chart.aggregation && opts?.includeAggregation !== false) {
    out.push(chart.aggregation.endsWith(".") ? chart.aggregation : `${chart.aggregation}.`);
  }
  const rows = chart.rows ?? null;
  if (rows !== null && chart.shown !== null && chart.shown !== undefined && chart.shown < rows) out.push(`Shows ${chart.shown} of ${rows} rows.`);
  if (chart.partial) out.push(chart.rows_total ? `Drawn from the first ${rows ?? "few"} of ${chart.rows_total} rows.` : "The query returned only its first rows: this is of those rows.");
  if (chart.omitted) out.push(`${chart.omitted} smaller ${chart.omitted === 1 ? "series is" : "series are"} not drawn.`);
  if (chart.skipped) out.push(`${chart.skipped} ${chart.skipped === 1 ? "row has no value and is" : "rows have no value and are"} not counted.`);
  if (chart.trend) {
    const r = chart.trend.r;
    const strength = Math.abs(r) >= 0.7 ? "strongly" : Math.abs(r) >= 0.4 ? "moderately" : "weakly";
    out.push(`Fitted line: r = ${r.toFixed(2)} over ${chart.trend.n} points (${strength} ${r >= 0 ? "rising" : "falling"} together). That alone does not show one causes the other.`);
  }
  if (chart.open_period) out.push(`${dateLabel(chart.open_period)} is still in progress, so its figures are partial.`);
  if (chart.first_period) out.push(`${dateLabel(chart.first_period)} started before the period shown, so its figures are partial.`);
  if (chart.kind === "waterfall") out.push("Each bar starts where the one before ended; the last bar is the total.");
  if (chart.kind === "funnel") out.push("Each percentage is of the step before.");
  if (chart.currency && !["kpi", "pie", "donut"].includes(chart.kind)) out.push(`Amounts in ${chart.currency}.`);
  return out;
}

// a panel of a facet by the currency column (as charts._CURRENCY_COL names it) is in the
// currency its title codes; any other facet (a site or country code) keeps the chart's own
const ROW_PX = 24; // a horizontal bar's row, in the drawing's own units
const CURRENCY_COL = /(^|_)(currency|currency_code|ccy)$/i;
function panelCurrency(chart: AskChart, title: string) {
  return CURRENCY_COL.test(chart.spec?.facet ?? "") && /^[A-Z]{3}$/.test(title) ? title : chart.currency;
}

function Multiples({ chart, theme, onTip }: { chart: AskChart; theme: Theme; onTip: ReturnType<typeof useTip> }) {
  const panels = chart.panels ?? [];
  // KPI tiles per facet value; a panel named by a currency code shows its amounts in it
  if (chart.kind === "kpi") return (
    <div className="space-y-4">
      {panels.map((p) => (
        <div key={p.title}>
          <div className="text-[12px] font-medium">{p.title}</div>
          <Kpis chart={{ ...chart, labels: p.labels, series: p.series, panels: undefined, currency: panelCurrency(chart, p.title) }} theme={theme} />
        </div>
      ))}
    </div>
  );
  if (chart.kind === "pie" || chart.kind === "donut") return (
    <div className="grid grid-cols-1 gap-x-6 gap-y-4 sm:grid-cols-2">
      {panels.map((p) => (
        <div key={p.title}>
          <div className="text-[12px] font-medium">{p.title}</div>
          <Pie chart={{ ...chart, labels: p.labels, series: p.series, panels: undefined, currency: panelCurrency(chart, p.title) }} theme={theme} onTip={onTip} />
        </div>
      ))}
    </div>
  );
  // one shared scale: a waterfall's is of its running totals, not of its steps
  const range: [number, number] = chart.kind === "waterfall"
    ? panels.flatMap((p) => waterfall(p.series[0]?.values ?? [])).reduce<[number, number]>(
        ([lo, hi], s) => [Math.min(lo, s.from, s.to), Math.max(hi, s.from, s.to)], [0, 0])
    : extent(panels.flatMap((p) => p.series));
  // panels by currency are never on one axis (400,000 USD would flatten 40,000 CAD); a
  // horizontal bar chart gives each label a row of its own, so names never overlap
  const byCurrency = CURRENCY_COL.test(chart.spec?.facet ?? "");
  const rows = !!chart.spec?.horizontal;
  return (
    <div className={cn("grid grid-cols-1 gap-x-6 gap-y-6", !rows && "sm:grid-cols-2")}>
      {panels.map((p) => (
        <div key={p.title}>
          <div className="text-[12px] font-medium">{p.title}</div>
          <Cartesian
            chart={{ ...chart, currency: panelCurrency(chart, p.title) }}
            labels={p.labels}
            series={p.series}
            theme={theme}
            width={rows ? 560 : 320}
            height={rows ? Math.max(170, 36 + p.labels.length * ROW_PX) : 170}
            range={byCurrency ? extent(p.series) : range}
            compact={!rows}
            onTip={onTip}
          />
        </div>
      ))}
    </div>
  );
}

/** One chart: its legend, the drawing of its kind with hover values, and its notes. */
/** The box's width when it is narrower than the chart's own (a phone), else 0: a line or bar
 * chart is drawn at that width so its labels keep their size (a 640-wide drawing shrunk to a
 * 370 px screen made 11 px labels 6 px). */
function useNarrow(box: React.RefObject<HTMLDivElement | null>): number {
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const read = () => { const x = Math.round(el.getBoundingClientRect().width); setW(x > 0 && x < W ? Math.max(x, 300) : 0); };
    read();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [box]);
  return w;
}

// charts already drawn in this visit: a chart animates in the first time only (not when a tile
// scrolls back into view or the page re-renders)
const DRAWN = new Set<string>();
function drawKey(chart: AskChart): string {
  let h = 5381;
  const panelText = chart.panels?.map((p) => `${p.title}:${p.labels.join(",")}`).join("|") ?? "";
  const text = `${chart.title}|${chart.kind}|${chart.labels.join(",")}|${panelText}|${chart.series.map((s) => s.values.join(",")).join(";")}`;
  for (let i = 0; i < text.length; i++) h = ((h << 5) + h + text.charCodeAt(i)) | 0;
  return `${h}:${text.length}`;
}

export function ChartView({ chart, reference, threshold }: { chart: AskChart; reference?: ChartReference; threshold?: number }) {
  const theme = useChartTheme();
  const [first] = useState(() => {
    if (typeof window === "undefined") return true;
    const k = drawKey(chart);
    const fresh = !DRAWN.has(k);
    DRAWN.add(k);
    return fresh;
  });
  const tip = useTip();
  const narrow = useNarrow(tip.box);
  let body: ReactNode;
  if (chart.panels?.length) body = <Multiples chart={chart} theme={theme} onTip={tip} />;
  else if (chart.kind === "pie" || chart.kind === "donut") body = <Pie chart={chart} theme={theme} onTip={tip} />;
  else if (chart.kind === "scatter") body = <Scatter chart={chart} theme={theme} onTip={tip} />;
  else if (chart.kind === "heatmap") body = <Heatmap chart={chart} theme={theme} onTip={tip} />;
  else if (chart.kind === "table") body = <ColourTable chart={chart} theme={theme} />;
  else if (chart.kind === "kpi") body = <Kpis chart={chart} theme={theme} />;
  else if (chart.kind === "funnel") body = <Funnel chart={chart} theme={theme} onTip={tip} />;
  else body = (
    <Cartesian
      chart={chart}
      labels={chart.labels}
      series={chart.series}
      theme={theme}
      onTip={tip}
      reference={reference}
      threshold={threshold}
      width={narrow || W}
      height={chart.spec?.horizontal && chart.labels.length > 8 ? Math.min(520, Math.max(H, 40 + chart.labels.length * 28)) : H}
    />
  );
  const notes = chartNotes(chart, { includeAggregation: false });
  return (
    <div className="space-y-1.5" data-chart-kind={chart.kind} data-draw={first ? "in" : undefined}>
      <Legend chart={chart} theme={theme} />
      {chart.aggregation && (
        <p className="text-[12px] text-muted" data-testid="chart-aggregation">{chart.aggregation}</p>
      )}
      <div ref={tip.box} className="relative" onMouseLeave={tip.hide}>
        {body}
        <Tooltip tip={tip.tip} theme={theme} />
      </div>
      {notes.length > 0 && <p className="text-[12px] text-muted">{notes.join(" ")}</p>}
    </div>
  );
}
