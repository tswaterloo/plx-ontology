// Pure helpers of the chart renderer: the palette (light and dark), value scales, ticks and
// number formats. Kept free of React so they are tested directly.

export type Theme = "light" | "dark";

// One categorical palette, colour-blind safe in order (blue, orange, teal, crimson, violet,
// ochre, sky, grey); the dark theme's are lighter so they read on a dark ground.
export const PALETTE: Record<Theme, string[]> = {
  light: ["#2E6FB7", "#E3832F", "#2A9D74", "#C9455B", "#7C5CBF", "#9C7A2E", "#3FA7C9", "#7F7F7F"],
  dark: ["#5B9BE6", "#F2A05A", "#4CC29A", "#E4707F", "#A58BE0", "#C9A456", "#6CC5E0", "#A6A6A6"],
};

export const INK: Record<Theme, { text: string; muted: string; grid: string; axis: string; ground: string }> = {
  light: { text: "#1F2937", muted: "#5B6170", grid: "#E5E7EB", axis: "#9CA3AF", ground: "#FFFFFF" },
  dark: { text: "#E5E7EB", muted: "#A3A9B6", grid: "#2A2F3A", axis: "#4B5563", ground: "#111418" },
};

// waterfall: rise, fall, total
export const MOVE: Record<Theme, { up: string; down: string; total: string }> = {
  light: { up: "#2E6FB7", down: "#E3832F", total: "#4B5563" },
  dark: { up: "#5B9BE6", down: "#F2A05A", total: "#9CA3AF" },
};

// the sequential ramp of heatmaps and coloured tables (low -> high)
const RAMP: Record<Theme, [number[], number[]]> = {
  light: [[0xee, 0xf4, 0xfb], [0x1f, 0x5a, 0x99]],
  dark: [[0x1b, 0x24, 0x33], [0x6f, 0xa8, 0xee]],
};

export function colour(i: number, theme: Theme): string {
  const p = PALETTE[theme];
  return p[((i % p.length) + p.length) % p.length];
}

/** The ramp colour of ``v`` between ``lo`` and ``hi``. */
export function ramp(v: number, lo: number, hi: number, theme: Theme): string {
  const t = hi > lo ? Math.min(1, Math.max(0, (v - lo) / (hi - lo))) : 0.5;
  const [a, b] = RAMP[theme];
  return `rgb(${a.map((x, i) => Math.round(x + (b[i] - x) * t)).join(",")})`;
}

/** Text that reads on a ramp colour of position ``t`` (0..1). */
export function inkOn(v: number, lo: number, hi: number, theme: Theme): string {
  const t = hi > lo ? (v - lo) / (hi - lo) : 0.5;
  if (theme === "dark") return t > 0.6 ? "#0B1220" : "#E5E7EB";
  return t > 0.55 ? "#FFFFFF" : "#1F2937";
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
export function human(name: string): string {
  const s = name.replace(/_/g, " ").trim();
  return s ? s[0].toUpperCase() + s.slice(1) : s;
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
