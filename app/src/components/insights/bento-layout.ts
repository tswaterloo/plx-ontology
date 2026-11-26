import { full, short } from "@/components/charts/scale";
import type { InsightInfo, InsightResult } from "@/lib/types";

export type BentoSize = "s" | "m" | "l" | "full";
export type BentoHeight = "compact" | "standard" | "tall";

export interface BentoState {
  sizes: Record<string, BentoSize>;
  order: string[];
  hidden: string[];
  heights?: Record<string, BentoHeight>;
  canvasIds?: string[];
}

export const BENTO_STORAGE_KEY = "plx.insights.bento.v1";
export const DEFAULT_CANVAS_TILES = 6;
export const MAX_STORED_CANVAS_TILES = 16;

export const EMPTY_BENTO_STATE: BentoState = {
  sizes: {},
  order: [],
  hidden: [],
};

export const BENTO_SIZE_OPTIONS: readonly {
  size: BentoSize;
  short: string;
  name: string;
  label: string;
  cols: number;
}[] = [
  { size: "s", short: "S", name: "Compact", label: "Compact (4 columns)", cols: 4 },
  { size: "m", short: "M", name: "Standard", label: "Standard (6 columns)", cols: 6 },
  { size: "l", short: "L", name: "Wide", label: "Wide (8 columns)", cols: 8 },
  { size: "full", short: "Full", name: "Full", label: "Full width (12 columns)", cols: 12 },
];

export const BENTO_HEIGHT_OPTIONS: readonly {
  height: BentoHeight;
  label: string;
}[] = [
  { height: "compact", label: "Compact" },
  { height: "standard", label: "Medium" },
  { height: "tall", label: "Tall" },
];

export const BENTO_SIZE_COLS: Record<BentoSize, number> = {
  s: 4,
  m: 6,
  l: 8,
  full: 12,
};

export const BENTO_SIZE_NAMES: Record<BentoSize, string> = {
  s: "Compact",
  m: "Half",
  l: "Wide",
  full: "Full",
};

export const BENTO_HEIGHT_NAMES: Record<BentoHeight, string> = {
  compact: "Compact",
  standard: "Medium",
  tall: "Tall",
};

export const BENTO_COL_SPAN: Record<BentoSize, string> = {
  s: "lg:col-span-4",
  m: "lg:col-span-6",
  l: "lg:col-span-8",
  full: "lg:col-span-12",
};

/** Crisp max-height constraints for chart containers per Bento tile size so tiles stay balanced. */
export const BENTO_CHART_BOX: Record<BentoSize, string> = {
  s: "[&_svg[data-chart-svg]]:max-h-[210px] [&_svg[data-chart-svg]]:w-full",
  m: "[&_svg[data-chart-svg]]:max-h-[250px] [&_svg[data-chart-svg]]:w-full",
  l: "[&_svg[data-chart-svg]]:max-h-[290px] [&_svg[data-chart-svg]]:w-full",
  full: "[&_svg[data-chart-svg]]:max-h-[320px] [&_svg[data-chart-svg]]:w-full",
};

export const BENTO_HEIGHT_BOX: Record<BentoHeight, string> = {
  compact: "[&_svg[data-chart-svg]]:max-h-[180px] [&_svg[data-chart-svg]]:w-full",
  standard: "[&_svg[data-chart-svg]]:max-h-[255px] [&_svg[data-chart-svg]]:w-full",
  tall: "[&_svg[data-chart-svg]]:max-h-[360px] [&_svg[data-chart-svg]]:w-full",
};

const VALID_SIZES = new Set<BentoSize>(["s", "m", "l", "full"]);
const VALID_HEIGHTS = new Set<BentoHeight>(["compact", "standard", "tall"]);
const CURRENCY_CODE = /^[A-Z]{3}$/;

export function isBentoSize(v: unknown): v is BentoSize {
  return typeof v === "string" && VALID_SIZES.has(v as BentoSize);
}

export function isBentoHeight(v: unknown): v is BentoHeight {
  return typeof v === "string" && VALID_HEIGHTS.has(v as BentoHeight);
}

/** Format a live resize ghost badge label such as "Wide · Tall" or "Half · Medium". */
export function formatResizeBadge(size: BentoSize, height?: BentoHeight): string {
  const wLabel = BENTO_SIZE_NAMES[size];
  const hLabel = BENTO_HEIGHT_NAMES[height ?? "standard"];
  return `${wLabel} · ${hLabel}`;
}

/** Snap a continuous 12-column pointer drag estimate to the closest valid Bento width. */
export function snapColsToBentoSize(cols: number): BentoSize {
  if (cols <= 5) return "s";
  if (cols <= 7) return "m";
  if (cols <= 10) return "l";
  return "full";
}

/** Snap a continuous pixel height drag estimate to a clean chart height preset. */
export function snapHeightDeltaToBentoHeight(start: BentoHeight, deltaY: number): BentoHeight {
  const levels: BentoHeight[] = ["compact", "standard", "tall"];
  const idx = levels.indexOf(start);
  const shift = deltaY >= 56 ? 1 : deltaY <= -56 ? -1 : 0;
  return levels[Math.max(0, Math.min(levels.length - 1, idx + shift))];
}

/** Cycle through Bento widths for one-click resize. */
export function nextBentoSize(current: BentoSize): BentoSize {
  if (current === "s") return "m";
  if (current === "m") return "l";
  if (current === "l") return "full";
  return "s";
}

const CURATED_DOMAIN_PRIORITY = [
  "Finance",
  "Sales",
  "Purchasing",
  "Operations",
  "Quality",
  "Customers",
  "Customer Service",
  "Decisions",
  "Team",
];

/**
 * Pick a calm, scannable executive canvas (default 6 tiles) instead of dumping 30+ tiles
 * all at once on first open. Always keeps user-pinned tiles first, then balances across
 * top business domains. For small sets (<= 8 items), returns all items.
 */
export function pickDefaultCanvasIds(items: readonly InsightInfo[], max = DEFAULT_CANVAS_TILES): string[] {
  if (items.length <= 8) return items.map((it) => it.id);
  const picked: string[] = [];
  const seen = new Set<string>();
  const add = (id: string) => {
    if (seen.has(id) || picked.length >= max) return;
    seen.add(id);
    picked.push(id);
  };

  // 1. Always keep user-pinned tiles first
  for (const it of items) {
    if (it.saved) add(it.id);
  }

  // 2. One representative tile per priority domain (preferring governed measures or drawn charts)
  const domainOf = (g: string) => g.replace(/^Measures:\s*/i, "");
  for (const dom of CURATED_DOMAIN_PRIORITY) {
    if (picked.length >= max) break;
    const candidates = items.filter((it) => !seen.has(it.id) && domainOf(it.group) === dom);
    if (candidates.length === 0) continue;
    const best =
      candidates.find((it) => Boolean(it.measure) && Boolean(it.chart)) ??
      candidates.find((it) => Boolean(it.chart)) ??
      candidates[0];
    add(best.id);
  }

  // 3. Fill remaining slots with governed measures or top items
  for (const it of items) {
    if (picked.length >= max) break;
    if (it.measure) add(it.id);
  }
  for (const it of items) {
    if (picked.length >= max) break;
    add(it.id);
  }

  return picked;
}

/** Smart default Bento span based on chart type, panel count, and data shape. */
export function defaultTileSize(item: InsightInfo, result?: InsightResult | null): BentoSize {
  const drawn = result?.drawn ?? null;
  const kind = drawn?.kind ?? item.chart?.kind ?? null;

  // Multi-panel small multiples need full row width to breathe without squeezing labels
  if (drawn?.panels && drawn.panels.length >= 2) {
    return "full";
  }

  // Dedicated KPI or compact single-row summary -> sleek 4-col executive card
  if (kind === "kpi") return "s";
  if (!drawn && result && result.row_count <= 1 && result.columns.length <= 3) {
    return "s";
  }
  if (drawn && !drawn.panels?.length && drawn.labels.length === 1 && drawn.series.length === 1 && kind === "bar") {
    return "m";
  }

  // Wide tables or heatmaps read cleanest across 12 columns
  if (kind === "heatmap") return "full";
  if ((kind === "table" || (!drawn && result)) && (result?.columns.length ?? 0) >= 6) {
    return "full";
  }

  // Time-series and combo trends pair cleanly at 6 columns (or 12 if dense) to avoid ragged 8+6 row gaps
  if (kind === "line" || kind === "area" || kind === "combo") {
    const pts = drawn?.labels.length ?? 0;
    return pts >= 14 ? "full" : "m";
  }

  // Standard breakdowns (bar, stacked_bar, pie, donut, funnel, scatter) -> half-width 6-col tile
  return "m";
}

export interface KpiSummary {
  headline: string;
  rawValue: number;
  label: string;
  deltaPct: number | null;
  deltaLabel: string | null;
  positive: boolean | null;
  sparkline: number[];
  sparkLabels: string[];
  currency: string | null;
  kind: "kpi" | "series";
}

function parseNumeric(v: string | number | null | undefined): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v !== "string") return null;
  const trimmed = v.trim();
  if (!trimmed || !/^-?\d+(?:\.\d+)?$/.test(trimmed)) return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

/**
 * Extract an executive KPI summary (headline figure, delta %, and mini-sparkline/bars)
 * for KPI tiles, single-row summaries, or single-series measures rendered in a compact 4-col card.
 */
export function extractKpiSummary(
  item: InsightInfo,
  result?: InsightResult | null,
): KpiSummary | null {
  if (!result || result.not_measurable) return null;
  const drawn = result.drawn;

  // Case 1: Explicit KPI chart from server
  if (drawn && drawn.kind === "kpi" && !drawn.panels?.length) {
    const firstSeries = drawn.series[0];
    if (!firstSeries) return null;
    const nums = firstSeries.values.map((v) => (typeof v === "number" && Number.isFinite(v) ? v : null));
    const valid = nums.filter((v): v is number => v !== null);
    if (valid.length === 0) return null;
    const latest = valid[valid.length - 1];
    const prev = valid.length >= 2 ? valid[valid.length - 2] : null;
    const deltaPct = prev !== null && prev !== 0 ? ((latest - prev) / Math.abs(prev)) * 100 : null;
    const currency = drawn.currency && CURRENCY_CODE.test(drawn.currency) ? drawn.currency : null;
    return {
      headline: currency ? `${short(latest)} ${currency}` : full(latest, null),
      rawValue: latest,
      label: drawn.labels[drawn.labels.length - 1] || firstSeries.name || item.title,
      deltaPct,
      deltaLabel: prev !== null ? `vs ${short(prev)}` : null,
      positive: deltaPct === null ? null : deltaPct >= 0,
      sparkline: valid.slice(-12),
      sparkLabels: drawn.labels.slice(-12),
      currency,
      kind: "kpi",
    };
  }

  // Case 2: Single-row tabular summary with a clear numeric metric
  if (!drawn && result.rows.length === 1 && result.columns.length > 0 && result.columns.length <= 4) {
    const row = result.rows[0];
    for (let i = result.columns.length - 1; i >= 0; i--) {
      const col = result.columns[i];
      if (/(^|_)(id|currency|currency_code|ccy)$/i.test(col)) continue;
      const n = parseNumeric(row[i]);
      if (n !== null) {
        const ccyIdx = result.columns.indexOf(`${col}_currency`);
        const ccy = ccyIdx >= 0 && CURRENCY_CODE.test(row[ccyIdx] ?? "") ? row[ccyIdx] : null;
        return {
          headline: ccy ? `${full(n, ccy)}` : full(n, null),
          rawValue: n,
          label: col.replace(/_/g, " "),
          deltaPct: null,
          deltaLabel: null,
          positive: null,
          sparkline: [n],
          sparkLabels: [item.title],
          currency: ccy,
          kind: "kpi",
        };
      }
    }
  }

  // Case 3: Time-series / single-series chart with >= 2 points (useful for executive KPI strip on S-sized cards)
  if (drawn && !drawn.panels?.length && drawn.series.length === 1 && drawn.labels.length >= 2) {
    const s = drawn.series[0];
    const pairs: { label: string; value: number }[] = [];
    for (let i = 0; i < drawn.labels.length; i++) {
      const v = s.values[i];
      if (typeof v === "number" && Number.isFinite(v)) {
        pairs.push({ label: drawn.labels[i], value: v });
      }
    }
    if (pairs.length < 2) return null;
    const latest = pairs[pairs.length - 1];
    const prev = pairs[pairs.length - 2];
    const deltaPct = prev.value !== 0 ? ((latest.value - prev.value) / Math.abs(prev.value)) * 100 : null;
    const currency = drawn.currency && CURRENCY_CODE.test(drawn.currency) ? drawn.currency : null;
    const tail = pairs.slice(-12);
    return {
      headline: currency ? `${short(latest.value)} ${currency}` : short(latest.value),
      rawValue: latest.value,
      label: latest.label,
      deltaPct,
      deltaLabel: prev.label ? `vs ${prev.label}` : null,
      positive: deltaPct === null ? null : deltaPct >= 0,
      sparkline: tail.map((p) => p.value),
      sparkLabels: tail.map((p) => p.label),
      currency,
      kind: "series",
    };
  }

  return null;
}

/**
 * Read persisted Bento state from localStorage safely.
 * Auto-trims bloated legacy canvas selections (> 16 tiles from the old "Show all" action)
 * back to the curated 6-tile default so the dashboard never opens cluttered.
 */
export function loadBentoState(): BentoState {
  if (typeof window === "undefined") return EMPTY_BENTO_STATE;
  try {
    const raw = window.localStorage.getItem(BENTO_STORAGE_KEY);
    if (!raw) return EMPTY_BENTO_STATE;
    const parsed = JSON.parse(raw) as Partial<BentoState> | null;
    if (!parsed || typeof parsed !== "object") return EMPTY_BENTO_STATE;
    const sizes: Record<string, BentoSize> = {};
    if (parsed.sizes && typeof parsed.sizes === "object") {
      for (const [k, v] of Object.entries(parsed.sizes)) {
        if (isBentoSize(v)) sizes[k] = v;
      }
    }
    const order = Array.isArray(parsed.order)
      ? parsed.order.filter((x): x is string => typeof x === "string")
      : [];
    const hidden = Array.isArray(parsed.hidden)
      ? parsed.hidden.filter((x): x is string => typeof x === "string")
      : [];
    const out: BentoState = { sizes, order, hidden };
    if (parsed.heights && typeof parsed.heights === "object") {
      const heights: Record<string, BentoHeight> = {};
      for (const [k, v] of Object.entries(parsed.heights)) {
        if (isBentoHeight(v)) heights[k] = v;
      }
      if (Object.keys(heights).length > 0) out.heights = heights;
    }
    if (Array.isArray(parsed.canvasIds)) {
      const deduped: string[] = [];
      const seen = new Set<string>();
      for (const x of parsed.canvasIds) {
        if (typeof x === "string" && !seen.has(x)) {
          seen.add(x);
          deduped.push(x);
        }
      }
      // Guard against legacy "Show all 76" bloat: auto-reset to curated 6 when > 16 tiles stored
      if (deduped.length > 0 && deduped.length <= MAX_STORED_CANVAS_TILES) {
        out.canvasIds = deduped;
      }
    }
    return out;
  } catch {
    return EMPTY_BENTO_STATE;
  }
}

/** Persist Bento state to localStorage safely. */
export function saveBentoState(state: BentoState): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(BENTO_STORAGE_KEY, JSON.stringify(state));
  } catch {
    // storage full or unavailable
  }
}

/** Clear persisted Bento layout state. */
export function clearBentoState(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(BENTO_STORAGE_KEY);
  } catch {
    // ignore
  }
}

export function isBentoCustomized(state: BentoState): boolean {
  return (
    Object.keys(state.sizes).length > 0 ||
    state.order.length > 0 ||
    state.hidden.length > 0 ||
    Boolean(state.heights && Object.keys(state.heights).length > 0) ||
    Boolean(state.canvasIds)
  );
}

/** Apply custom order array to an items list while preserving any new items in their original order. */
export function applyBentoOrder<T extends { id: string }>(
  items: readonly T[],
  customOrder: readonly string[],
): T[] {
  if (customOrder.length === 0) return [...items];
  const rank = new Map<string, number>();
  customOrder.forEach((id, idx) => {
    if (!rank.has(id)) rank.set(id, idx);
  });
  return items
    .map((item, origIdx) => ({ item, origIdx, r: rank.get(item.id) }))
    .sort((a, b) => {
      if (a.r !== undefined && b.r !== undefined) return a.r - b.r;
      if (a.r !== undefined) return -1;
      if (b.r !== undefined) return 1;
      return a.origIdx - b.origIdx;
    })
    .map((x) => x.item);
}

/** Reorder an id array by placing `fromId` at the index of `toId`. */
export function reorderIds(ids: readonly string[], fromId: string, toId: string): string[] {
  if (fromId === toId) return [...ids];
  const fromIdx = ids.indexOf(fromId);
  const toIdx = ids.indexOf(toId);
  if (fromIdx < 0 || toIdx < 0) return [...ids];
  const next = [...ids];
  next.splice(fromIdx, 1);
  next.splice(toIdx, 0, fromId);
  return next;
}

/** Move `id` one step up (-1) or down (+1) in an id array. */
export function moveIdStep(ids: readonly string[], id: string, dir: -1 | 1): string[] {
  const idx = ids.indexOf(id);
  const target = idx + dir;
  if (idx < 0 || target < 0 || target >= ids.length) return [...ids];
  const next = [...ids];
  [next[idx], next[target]] = [next[target], next[idx]];
  return next;
}
