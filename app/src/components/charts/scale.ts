// Pure helpers of the chart renderer: the palette (light and dark), value scales, ticks and
// number formats. Kept free of React so they are tested directly.

export type Theme = "light" | "dark";

// Desaturated Databricks Genie-calibrated editorial chart palette (ZERO neon cyan, ZERO purple).
export const PALETTE: Record<Theme, string[]> = {
  light: ["#2b6cb0", "#2f855a", "#b7791f", "#c53030", "#2c7a7b", "#c05621", "#3182ce", "#689f38"],
  dark: ["#5c9ce6", "#5da56f", "#dca138", "#d95b48", "#4ca6a8", "#d98245", "#7ea6d6", "#8fb35a"],
};

export const INK: Record<Theme, { text: string; muted: string; grid: string; axis: string; ground: string }> = {
  light: { text: "#111318", muted: "#3f4652", grid: "#e4e7ec", axis: "#cfd5de", ground: "#ffffff" },
  dark: { text: "#f3f3f3", muted: "#c8c8c8", grid: "#2a2a2a", axis: "#3d3d3d", ground: "#1e1e1e" },
};

// waterfall: rise, fall, total
export const MOVE: Record<Theme, { up: string; down: string; total: string }> = {
  light: { up: "#2f855a", down: "#c53030", total: "#3f4652" },
  dark: { up: "#5da56f", down: "#d95b48", total: "#c8c8c8" },
};

// semantic threshold colours matching Genie (sage emerald on-target, warm ochre watch, terracotta alert)
export const THRESHOLD: Record<Theme, { ok: string; risk: string; late: string }> = {
  light: { ok: "#2f855a", risk: "#c0821a", late: "#c53030" },
  dark: { ok: "#5da56f", risk: "#dca138", late: "#d95b48" },
};

// 3-stop diverging ramp of heatmaps and coloured tables: neutral matte/cream -> warm ochre -> terracotta coral
const RAMP_STOPS: Record<Theme, [[number, number, number], [number, number, number], [number, number, number]]> = {
  light: [[0xf4, 0xf4, 0xf4], [0xf6, 0xcb, 0x8b], [0xd9, 0x48, 0x48]],
  dark: [[0x26, 0x26, 0x26], [0xd9, 0x92, 0x4e], [0xd9, 0x5b, 0x48]],
};

export function colour(i: number, theme: Theme): string {
  const p = PALETTE[theme];
  return p[((i % p.length) + p.length) % p.length];
}

/** Semantic bar fill colour relative to a target threshold (emerald >= target, amber >= 75% target, coral-red below). */
export function thresholdColour(v: number, target: number, theme: Theme): string {
  const t = THRESHOLD[theme];
  if (v >= target) return t.ok;
  if (v >= target * 0.75) return t.risk;
  return t.late;
}

function rampRgb(v: number, lo: number, hi: number, theme: Theme): [number, number, number] {
  const t = hi > lo ? Math.min(1, Math.max(0, (v - lo) / (hi - lo))) : 0.5;
  const [low, mid, high] = RAMP_STOPS[theme];
  const [a, b, u] = t <= 0.5 ? [low, mid, t * 2] : [mid, high, (t - 0.5) * 2];
  return [
    Math.round(a[0] + (b[0] - a[0]) * u),
    Math.round(a[1] + (b[1] - a[1]) * u),
    Math.round(a[2] + (b[2] - a[2]) * u),
  ];
}

function relativeLuminance(r: number, g: number, b: number): number {
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

/** High-contrast foreground ink for any `#rrggbb` colour fill. */
export function inkForHex(hex: string): string {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex.trim());
  if (!m) return "#f4f2ec";
  const lum = relativeLuminance(parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16));
  return lum > 0.34 ? "#141413" : "#ffffff";
}

/** The ramp colour of ``v`` between ``lo`` and ``hi``. */
export function ramp(v: number, lo: number, hi: number, theme: Theme): string {
  const [r, g, b] = rampRgb(v, lo, hi, theme);
  return `rgb(${r},${g},${b})`;
}

/** Text that reads cleanly on a ramp colour of position ``v`` in ``lo..hi``. */
export function inkOn(v: number, lo: number, hi: number, theme: Theme): string {
  const [r, g, b] = rampRgb(v, lo, hi, theme);
  return relativeLuminance(r, g, b) > 0.34 ? "#141413" : "#f4f2ec";
}

/** Round ticks covering lo..hi, 1, 2 or 5 times a power of ten apart (the last >= hi). */
export function ticks(lo: number, hi: number, count = 5): number[] {
  if (!(hi > lo)) hi = lo + 1;
  const raw = (hi - lo) / Math.max(1, count - 1);
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * power).find((s) => s >= raw) ?? raw;
  const out = [Math.floor(lo / step) * step];
  while (out[out.length - 1] < hi - step * 1e-9 && out.length < 14) out.push(out[out.length - 1] + step);
  return out.map((v) => Number(v.toPrecision(12)));
}

/** A number as an axis label (1.2M, 34k, 512, 0.25). */
export function short(n: number): string {
  const a = Math.abs(n);
  if (a >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (a >= 1e4) return `${Math.round(n / 1e3)}k`;
  if (Number.isInteger(n)) return String(n);
  return a >= 100 ? n.toFixed(0) : String(Number(n.toFixed(2)));
}

/** A number in full, as a tooltip or a table shows it (1,234.5; with its currency). */
export function full(n: number | null | undefined, currency?: string | null): string {
  if (n === null || n === undefined) return "no value";
  const text = n.toLocaleString("en-US", { maximumFractionDigits: Math.abs(n) < 1 ? 4 : 2 });
  return currency ? `${text} ${currency}` : text;
}

/** A column name as people read it (mail_sent -> Mail sent). */
const SPELLED: Record<string, string> = { amt: "amount", qty: "quantity", cnt: "count", avg: "average", num: "number", val: "value", tot: "total", inv: "invoice", po: "PO", ar: "receivables", ap: "payables" };

/** A column name as a plain label: "amt_cad" -> "Amount CAD", "busiest_share_pct" -> "Busiest share %". */
export function human(name: string): string {
  // a series named by an amount (a pivot on a money column) reads as the amount, not raw digits
  if (/^-?\d+\.\d+$/.test(name.trim())) return full(Number(name));
  const s = name
    .replace(/_/g, " ")
    .replace(/\b[a-z]+\b/gi, (w) => (SPELLED[w.toLowerCase()] ?? (/^(cad|usd|eur|gbp|mxn)$/i.test(w) ? w.toUpperCase() : w)))
    .replace(/\bpct\b/gi, "%")
    .replace(/\s+%/g, " %")
    .trim();
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** An axis or period label as the app writes dates: "2026-11-16" -> "16 Nov" (the year added
 * when it is not this year's), "2026-11" -> "Nov 2026"; anything else as it is. */
export function dateLabel(label: string, now: Date = new Date()): string {
  const day = /^(\d{4})-(\d{2})-(\d{2})$/.exec(label);
  if (day) {
    const [y, m, d] = [Number(day[1]), Number(day[2]), Number(day[3])];
    if (m < 1 || m > 12 || d < 1 || d > 31) return label;
    return `${d} ${MONTHS[m - 1]}${y === now.getFullYear() ? "" : ` ${y}`}`;
  }
  const month = /^(\d{4})-(\d{2})$/.exec(label);
  if (month && Number(month[2]) >= 1 && Number(month[2]) <= 12) return `${MONTHS[Number(month[2]) - 1]} ${month[1]}`;
  return label;
}

export function clip(text: string, n: number): string {
  return text.length <= n ? text : `${text.slice(0, n - 1)}…`;
}

/** The value range of the series (always including 0); stacked: of the stacked sums. */
export function extent(series: { values: (number | null | undefined)[] }[], stacked = false): [number, number] {
  let lo = 0;
  let hi = 0;
  if (stacked) {
    const n = Math.max(0, ...series.map((s) => s.values.length));
    for (let i = 0; i < n; i++) {
      let pos = 0;
      let neg = 0;
      for (const s of series) {
        const v = s.values[i];
        if (v === null || v === undefined) continue;
        if (v >= 0) pos += v;
        else neg += v;
      }
      hi = Math.max(hi, pos);
      lo = Math.min(lo, neg);
    }
  } else {
    for (const s of series) for (const v of s.values) if (v !== null && v !== undefined) {
      hi = Math.max(hi, v);
      lo = Math.min(lo, v);
    }
  }
  return [lo, hi];
}

/** A waterfall's bars: each change from the running total, then the total from 0. */
export function waterfall(values: (number | null | undefined)[]): { from: number; to: number; total: boolean }[] {
  let run = 0;
  const out = values.map((v) => {
    const from = run;
    run += v ?? 0;
    return { from, to: run, total: false };
  });
  out.push({ from: 0, to: run, total: true });
  return out;
}

/** Of a funnel's stages, each one's share of the first and of the stage before. */
export function conversion(values: (number | null | undefined)[]): { ofFirst: number | null; ofPrev: number | null }[] {
  const first = values[0] ?? 0;
  return values.map((v, i) => {
    const prev = i > 0 ? values[i - 1] ?? 0 : null;
    return {
      ofFirst: first > 0 && v !== null && v !== undefined ? v / first : null,
      ofPrev: prev && v !== null && v !== undefined ? v / prev : null,
    };
  });
}

/** A chart's axis labels: each as ``dateLabel`` says it (a midnight timestamp is its day), and
 * labels that are all first days of months ("2026-10-01", "2026-11-01") as their months. */
export function axisLabels(labels: string[], now: Date = new Date()): string[] {
  const day = labels.map((l) => /^(\d{4}-\d{2}-\d{2})(?:[T ]00:00(?::00(?:\.0+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$/.exec(l)?.[1] ?? null);
  if (labels.length > 1 && day.every((d) => d && d.endsWith("-01"))) return day.map((d) => dateLabel(d!.slice(0, 7), now));
  return labels.map((l, i) => dateLabel(day[i] ?? l, now));
}
