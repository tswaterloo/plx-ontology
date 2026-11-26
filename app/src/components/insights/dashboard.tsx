"use client";

import {
  ArrowDown,
  ArrowUp,
  BarChart3,
  Bell,
  ChevronDown,
  Clock,
  Code2,
  Download,
  EyeOff,
  GitBranch,
  GripVertical,
  HelpCircle,
  Layers,
  LayoutGrid,
  MoreHorizontal,
  Network,
  Plus,
  RotateCcw,
  RotateCw,
  Search,
  Sparkles,
  Table2,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import Link from "next/link";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { AnswerCharts, ChartSaves } from "@/components/answer-chart";
import { saveCsv } from "@/components/charts/export";
import { cn } from "@/lib/cn";
import { ExcelExportButton } from "@/components/excel-export-button";
import { DefinitionLines, definitionMeta } from "@/components/definition-lines";
import { QueryToggle, ShowWork } from "@/components/query-work";
import { LoadProblem } from "@/components/ui/notice";
import { WhyDidItChange } from "@/components/insights/why";
import { BenchmarksPanel } from "@/components/insights/benchmarks-panel";
import { OntologyAndMeasuresPanel } from "@/components/insights/ontology-map";
import { WidgetLibraryDrawer } from "@/components/insights/widget-library-drawer";
import {
  BENTO_CHART_BOX,
  BENTO_COL_SPAN,
  BENTO_HEIGHT_BOX,
  BENTO_HEIGHT_OPTIONS,
  BENTO_SIZE_COLS,
  BENTO_SIZE_OPTIONS,
  DEFAULT_CANVAS_TILES,
  EMPTY_BENTO_STATE,
  applyBentoOrder,
  clearBentoState,
  defaultTileSize,
  extractKpiSummary,
  formatResizeBadge,
  isBentoCustomized,
  loadBentoState,
  moveIdStep,
  pickDefaultCanvasIds,
  reorderIds,
  saveBentoState,
  snapColsToBentoSize,
  snapHeightDeltaToBentoHeight,
  type BentoHeight,
  type BentoSize,
  type BentoState,
  type KpiSummary,
} from "@/components/insights/bento-layout";
import { ChartSkeleton } from "@/components/charts/chart-skeleton";
import { Pending } from "@/components/ui/skeleton";
import { deleteInsight, getInsight, proposeMeasureCandidate, readInsightFiltered, setInsightSchedule } from "@/lib/api";
import { ApiError, noticeKind, plainError, tileError } from "@/lib/errors";
import { ago, formatDate, isBeforeToday } from "@/lib/format";
import { full, human } from "@/components/charts/scale";
import type { InsightInfo, InsightList, InsightResult } from "@/lib/types";

const quiet = "h-7 rounded-md border border-border px-2.5 text-[12.5px] font-medium text-fg transition-[background-color,border-color,color,transform] duration-150 ease-[cubic-bezier(0.2,0,0,1)] hover:border-control hover:bg-hover active:scale-[0.98] active:bg-surface-2 cursor-pointer disabled:cursor-default disabled:opacity-50 disabled:hover:bg-transparent disabled:active:scale-100";

export const TOP = 8;
type Drawn = NonNullable<InsightResult["drawn"]>;

/** Pure: total plotted items across a single-panel or multi-panel chart (never 0 when panels hold labels). */
export function chartPointCount(chart: Drawn, fallbackRows = 0): number {
  if (chart?.panels?.length) {
    const panelTotal = chart.panels.reduce((sum, p) => sum + (p.labels?.length ?? 0), 0);
    if (panelTotal > 0) return panelTotal;
  }
  const labels = chart?.labels ?? [];
  if (labels.length > 0) return labels.length;
  return chart?.rows ?? fallbackRows;
}

const CURRENCY_FACET = /(^|_)(currency|currency_code|ccy)$/i;

/** Pure: a chart cut to its first `n` names (bars, lines and their series alike); a chart
 * with no more names than that, or of a kind with no names along an axis, as it is. */
export function topOf(chart: Drawn, n = TOP): Drawn {
  if (!chart || !["bar", "line", "area", "combo", "stacked_bar"].includes(chart.kind)) return chart;
  if (chart.panels?.length) {
    if (!["bar", "stacked_bar"].includes(chart.kind)) return chart;
    const needsCut = chart.panels.some((p) => (p.labels?.length ?? 0) > n);
    if (!needsCut) return chart;
    return {
      ...chart,
      panels: chart.panels.map((p) =>
        (p.labels?.length ?? 0) <= n
          ? p
          : {
              ...p,
              labels: (p.labels ?? []).slice(0, n),
              series: (p.series ?? []).map((s) => ({ ...s, values: (s.values ?? []).slice(0, n) })),
            },
      ),
    };
  }
  const labels = chart.labels ?? [];
  if (labels.length <= n) return chart;
  return {
    ...chart,
    labels: labels.slice(0, n),
    series: (chart.series ?? []).map((s) => ({ ...s, values: (s.values ?? []).slice(0, n) })),
  };
}

/** What to type into Ask about a tile: its title as a question the reader can edit. */
export function askAbout(title: string): string {
  return `About the Insights chart "${title}": what changed lately, and why?`;
}

function groupKey(i: InsightInfo): string {
  return i.saved ? "Pinned by you" : i.group || "Other";
}

/** Pure: the dashboard's groups in the order they first appear, each with its insights. */
export function grouped(items: InsightInfo[]): [string, InsightInfo[]][] {
  const out = new Map<string, InsightInfo[]>();
  for (const it of items) {
    const key = it.saved ? "Pinned by you" : it.group || "Other";
    out.set(key, [...(out.get(key) ?? []), it]);
  }
  return [...out.entries()];
}

/** Pure: the ids in the order shown after `id` moves one place up (-1) or down (1) among the
 * charts of its own group; the same order when it cannot move. */
export function moved(items: InsightInfo[], id: string, dir: -1 | 1): string[] {
  const ids = items.map((i) => i.id);
  const me = items.find((i) => i.id === id);
  if (!me) return ids;
  const key = (i: InsightInfo) => (i.saved ? "Pinned by you" : i.group || "Other");
  const mates = items.filter((i) => key(i) === key(me)).map((i) => i.id);
  const swap = mates[mates.indexOf(id) + dir];
  if (swap === undefined) return ids;
  const a = ids.indexOf(id), b = ids.indexOf(swap);
  [ids[a], ids[b]] = [ids[b], ids[a]];
  return ids;
}

/** Pure: the ids in the order shown after `from` is dropped on `to`, both of one group: it
 * takes the place of `to`, the others making room; the same order for another group or an unknown id. */
export function placed(items: InsightInfo[], from: string, to: string): string[] {
  const ids = items.map((i) => i.id);
  const a = items.find((i) => i.id === from), b = items.find((i) => i.id === to);
  if (!a || !b || from === to || groupKey(a) !== groupKey(b)) return ids;
  const rest = ids.filter((x) => x !== from);
  rest.splice(ids.indexOf(to), 0, from);
  return rest;
}

const PEOPLE = new Set(["person", "who", "busiest_person"]);

/** Pure: one cell as people read it. */
export function cellText(cell: string | null | undefined, column: string, row: readonly (string | null)[], columns: readonly string[]): string {
  if (cell === null || cell === undefined) return "";
  if (!/^-?\d+\.\d+$/.test(cell)) return cell;
  const n = Number(cell);
  if (!Number.isFinite(n)) return cell;
  const code = row[columns.indexOf(`${column}_currency`)];
  return full(n, /^[A-Z]{3}$/.test(code ?? "") ? code : null);
}

/** Compact inline SVG sparkline / mini-bar chart for Executive KPI Bento cards. */
function KpiSparkline({ values, positive, label }: { values: number[]; positive: boolean | null; label: string }) {
  if (values.length === 0) return null;
  const w = 136;
  const h = 38;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const strokeColor = positive === false ? "var(--late)" : positive === true ? "var(--ok)" : "var(--accent)";

  if (values.length === 1) {
    return (
      <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="shrink-0" role="img" aria-label={label}>
        <rect x={8} y={14} width={w - 16} height={10} rx={5} fill="var(--surface-2)" />
        <rect x={8} y={14} width={Math.round((w - 16) * 0.72)} height={10} rx={5} fill={strokeColor} fillOpacity={0.85} />
      </svg>
    );
  }

  if (values.length <= 6) {
    const barW = Math.max(8, Math.floor((w - 8) / values.length) - 4);
    const gap = Math.floor((w - barW * values.length) / Math.max(1, values.length - 1));
    return (
      <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="shrink-0" role="img" aria-label={label}>
        {values.map((v, i) => {
          const barH = Math.max(4, Math.round(((v - min) / span) * (h - 8)) + 4);
          const x = i * (barW + gap);
          const isLast = i === values.length - 1;
          return (
            <rect
              key={i}
              x={x}
              y={h - barH}
              width={barW}
              height={barH}
              rx={2.5}
              fill={strokeColor}
              fillOpacity={isLast ? 0.95 : 0.35}
            />
          );
        })}
      </svg>
    );
  }

  const pts = values.map((v, i) => {
    const x = 4 + (i / (values.length - 1)) * (w - 8);
    const y = h - 4 - ((v - min) / span) * (h - 8);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const lastPt = pts[pts.length - 1].split(",");
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="shrink-0" role="img" aria-label={label}>
      <polyline
        fill="none"
        stroke={strokeColor}
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        points={pts.join(" ")}
      />
      <circle cx={Number(lastPt[0])} cy={Number(lastPt[1])} r={3} fill={strokeColor} />
    </svg>
  );
}

/** Sleek Executive KPI summary body for 4-col KPI tiles and compact single-number measures. */
function KpiCardBody({ kpi, title, compact = false }: { kpi: KpiSummary; title: string; compact?: boolean }) {
  const deltaText =
    kpi.deltaPct !== null
      ? `${kpi.deltaPct >= 0 ? "+" : ""}${Math.abs(kpi.deltaPct) >= 10 ? Math.round(kpi.deltaPct) : kpi.deltaPct.toFixed(1)}%`
      : null;

  return (
    <div
      className={cn(
        "flex items-end justify-between gap-3",
        compact ? "border-b border-border/60 pb-2.5" : "py-2",
      )}
      data-testid="kpi-bento-summary"
    >
      <div className="min-w-0 space-y-1">
        <div className={cn("font-semibold tracking-[-0.025em] text-fg tabular-nums", compact ? "text-[22px] leading-7" : "text-[28px] leading-9")}>
          {kpi.headline}
        </div>
        <div className="flex flex-wrap items-center gap-1.5 text-[12px]">
          {deltaText && (
            <span
              className={cn(
                "inline-flex items-center gap-1 text-[12px] font-medium tabular-nums",
                kpi.positive === false ? "text-late" : "text-ok",
              )}
            >
              {kpi.positive === false ? (
                <TrendingDown className="size-3 shrink-0" strokeWidth={2.2} aria-hidden />
              ) : (
                <TrendingUp className="size-3 shrink-0" strokeWidth={2.2} aria-hidden />
              )}
              {deltaText}
            </span>
          )}
          <span className="truncate text-muted">
            {kpi.deltaLabel ? `${kpi.label} (${kpi.deltaLabel})` : kpi.label}
          </span>
        </div>
      </div>
      {!compact && <KpiSparkline values={kpi.sparkline} positive={kpi.positive} label={title} />}
    </div>
  );
}

/** Compact Export dropdown for tabular tiles without a drawn SVG chart (CSV + Excel). */
function TableExportMenu({ data }: { data: InsightResult }) {
  const menu = useRef<HTMLDetailsElement>(null);
  return (
    <details ref={menu} className="relative">
      <summary className="inline-flex h-6 list-none items-center gap-1 rounded-md border border-border bg-bg px-1.5 text-[11.5px] font-medium text-muted hover:border-border-strong hover:bg-hover hover:text-fg cursor-pointer [&::-webkit-details-marker]:hidden">
        <Download className="size-3 shrink-0" strokeWidth={2} aria-hidden />
        <span>Export</span>
        <ChevronDown className="size-3 shrink-0 opacity-70" strokeWidth={2} aria-hidden />
      </summary>
      <div
        className="absolute right-0 z-20 mt-1 flex min-w-[9.5rem] flex-col rounded-lg border border-border bg-raised py-1 shadow-pop"
        role="menu"
      >
        <button
          type="button"
          role="menuitem"
          onClick={() => {
            if (menu.current) menu.current.open = false;
            saveCsv(data.columns, data.rows, data.title);
          }}
          className="px-3 py-1.5 text-left text-[12px] text-fg hover:bg-hover cursor-pointer"
          data-testid="csv-export-btn"
        >
          CSV
        </button>
        <div className="px-2 py-1">
          <ExcelExportButton
            title={data.title}
            label="Excel (.xlsx)"
            className="inline-flex w-full items-center gap-1.5 rounded px-1.5 py-1 text-left text-[12px] text-fg hover:bg-hover cursor-pointer"
            req={() => ({
              title: data.title,
              tables: [{
                title: data.title,
                columns: data.columns,
                rows: data.rows,
                sql: data.sql,
                as_of: data.as_of,
                total_rows: data.total_rows ?? null,
                truncated: Boolean(data.truncated || (data.total_rows != null && data.rows.length < data.total_rows)),
              }],
            })}
          />
        </div>
      </div>
    </details>
  );
}

/** The rows behind an insight, as read now (cells as text). */
function Rows({ data }: { data: InsightResult }) {
  const rows = data?.rows ?? [];
  const columns = data?.columns ?? [];
  if (rows.length === 0) return <p className="text-[13px] text-muted">No rows you can see right now.</p>;
  const numericCol = columns.map((_, j) => {
    const filled = rows.map((r) => r[j]).filter((c): c is string => c !== null && c !== undefined && c.trim() !== "");
    return filled.length > 0 && filled.every((c) => /^-?\d+(?:\.\d+)?$/.test(c.trim()));
  });
  return (
    <div className="max-h-72 overflow-auto">
      <table className="w-full border-collapse text-left text-[12px]">
        <thead className="sticky top-0 bg-surface text-muted">
          <tr className="border-b border-border">
            {columns.map((c, j) => (
              <th key={c} className={cn("px-2 py-1.5 font-medium whitespace-nowrap", numericCol[j] && "text-right")}>
                {human(c)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-border/60 first:border-t-0 hover:bg-hover/40">
              {r.map((c, j) => (
                <td key={j} className={cn("px-2 py-1.5 tabular-nums", (numericCol[j] || /^-?\d+\.\d+$/.test(c ?? "")) && "text-right whitespace-nowrap")}>
                  {PEOPLE.has(columns[j]) && c ? <Link href={`/team?person=${encodeURIComponent(data.people?.[i] || c)}`} className="hover:underline">{c}</Link> : cellText(c, columns[j], r, columns)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {data.truncated && <p className="pt-1 text-[12px] text-muted">{data.total_rows ? `First ${data.row_count} of ${data.total_rows} rows.` : `Only the first ${data.row_count} rows are shown.`}</p>}
      <div className="flex flex-wrap items-center gap-1.5 pt-2">
        <button
          type="button"
          onClick={() => saveCsv(columns, rows, data.title)}
          className="inline-flex h-7 items-center gap-1.5 rounded-md border border-border bg-bg px-2 text-[12px] font-medium text-muted hover:border-border-strong hover:bg-hover hover:text-fg cursor-pointer"
          data-testid="csv-export-btn"
        >
          <Download className="size-3 shrink-0" strokeWidth={2} aria-hidden />
          CSV
        </button>
        <ExcelExportButton
          title={data.title}
          className="inline-flex h-7 items-center gap-1.5 rounded-md border border-border bg-bg px-2 text-[12px] font-medium text-muted hover:border-border-strong hover:bg-hover hover:text-fg cursor-pointer disabled:opacity-50"
          req={() => ({
            title: data.title,
            tables: [{
              title: data.title,
              columns,
              rows,
              sql: data.sql,
              as_of: data.as_of,
              total_rows: data.total_rows ?? null,
              truncated: Boolean(data.truncated || (data.total_rows != null && rows.length < data.total_rows)),
            }],
          })}
        />
      </div>
    </div>
  );
}

const SCHEDULES: [InsightInfo["schedule"], string][] = [["", "Off"], ["hourly", "Hourly"], ["daily", "Daily"], ["weekly", "Weekly (Mon)"]];
const NOTIFY: [InsightInfo["notify"], string][] = [["new_rows", "When a new row appears"], ["rows", "When it has any rows"], ["crosses", "When a number crosses a line"], ["every_run", "Every time"], ["never", "Never"]];
type Watch = NonNullable<InsightInfo["watch"]>;

/** A saved insight's schedule control. */
export function ScheduleControl({ item, columns = [] }: { item: InsightInfo; columns?: string[] }) {
  const axis = item.chart?.x || null;
  const [now, setNow] = useState({ schedule: item.schedule, notify: item.notify, watch: item.watch ?? null });
  const [line, setLine] = useState<{ column: string; op: Watch["op"]; value: string }>({
    column: item.watch?.column ?? "", op: item.watch?.op ?? "above", value: item.watch ? String(item.watch.value) : "" });
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const save = async (next: { schedule: InsightInfo["schedule"]; notify: InsightInfo["notify"]; watch: Watch | null }) => {
    setBusy(true);
    setProblem(null);
    try {
      const got = await setInsightSchedule(item.id, { schedule: next.schedule, notify: next.notify, watch: next.watch });
      setNow({ schedule: got.schedule, notify: got.notify, watch: got.watch ?? null });
    } catch (e) {
      setProblem(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const value = Number(line.value);
  const ready = line.column !== "" && line.value.trim() !== "" && Number.isFinite(value);
  const saveLine = () => ready && save({ schedule: now.schedule, notify: "crosses", watch: { column: line.column, op: line.op, value, ...(axis ? { row: "latest" as const, order: axis } : { row: "any" as const }) } });
  const choose = (notify: InsightInfo["notify"]) => {
    if (notify === "crosses") {
      setNow({ ...now, notify });
      if (ready) saveLine();
    } else {
      save({ schedule: now.schedule, notify, watch: null });
    }
  };
  const field = "h-7 rounded-md border border-border bg-bg px-1.5 text-[12px] text-fg dark:bg-surface";
  const numeric = columns.length ? columns : now.watch ? [now.watch.column] : [];
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[12px] text-muted" data-testid={`schedule-${item.id}`}>
      <label className="flex items-center gap-1">
        Check it
        <select className={field} value={now.schedule} disabled={busy} onChange={(e) => save({ schedule: e.target.value as InsightInfo["schedule"], notify: now.notify === "never" && e.target.value ? "new_rows" : now.notify, watch: now.watch })}>
          {SCHEDULES.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
        </select>
      </label>
      {now.schedule && (
        <label className="flex items-center gap-1">
          Tell me on Home
          <select className={field} value={now.notify} disabled={busy} onChange={(e) => choose(e.target.value as InsightInfo["notify"])}>
            {NOTIFY.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
          </select>
        </label>
      )}
      {now.schedule && now.notify === "crosses" && (
        <span className="flex items-center gap-1" data-testid={`watch-${item.id}`}>
          <select className={field} aria-label="Column to watch" value={line.column} disabled={busy} onChange={(e) => setLine({ ...line, column: e.target.value })}>
            <option value="">Column</option>
            {numeric.map((c) => <option key={c} value={c}>{human(c)}</option>)}
          </select>
          <select className={field} aria-label="Above or below" value={line.op} disabled={busy} onChange={(e) => setLine({ ...line, op: e.target.value as Watch["op"] })}>
            <option value="above">above</option>
            <option value="below">below</option>
          </select>
          <input className={`${field} w-20`} aria-label="Line" inputMode="decimal" value={line.value} disabled={busy} onChange={(e) => setLine({ ...line, value: e.target.value })} />
          <button type="button" className={field} disabled={busy || !ready} onClick={saveLine}>Watch</button>
          {now.watch && <span>Watching {watchWords(now.watch)}</span>}
        </span>
      )}
      {now.schedule && item.last_run && <span>{item.last_run}</span>}
      <TellMeWhen item={item} />
      {problem && <span role="alert" className="text-fg">{problem}</span>}
    </div>
  );
}

function watchSubject(w: Watch): string {
  const col = human(w.column).toLowerCase();
  const of = w.where ? ` in ${w.where.value}` : "";
  return w.row === "latest" ? `the latest ${col}${of}` : w.row === "total" ? `the total ${col}${of}` : `${col}${of}`;
}

export function watchWords(w: Watch): string {
  return `${watchSubject(w)} ${w.op} ${w.value}`;
}

export function tellMeWhenHref(item: InsightInfo, words: string): string {
  const q = `Tell me when ${words.trim()} (my pinned chart "${item.title}")`;
  return `/ask?q=${encodeURIComponent(q)}&page=${encodeURIComponent(`/insights#insight-${item.id}`)}`;
}

function TellMeWhen({ item }: { item: InsightInfo }) {
  const [words, setWords] = useState("");
  const go = useRef<HTMLAnchorElement>(null);
  const ready = words.trim().length > 2;
  return (
    <span className="flex items-center gap-1" data-testid={`tell-${item.id}`}>
      <label className="flex items-center gap-1">
        Tell me when
        <input className="h-7 w-52 rounded-md border border-border bg-bg px-1.5 text-[12px]" value={words} maxLength={300}
          placeholder="USD revenue drops under 400k" onChange={(e) => setWords(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && ready) go.current?.click(); }} />
      </label>
      {ready
        ? <Link ref={go} prefetch={false} href={tellMeWhenHref(item, words)} className={`${quiet} inline-flex items-center`}>Set up</Link>
        : <span className={`${quiet} inline-flex items-center opacity-50`} aria-disabled="true">Set up</span>}
    </span>
  );
}

export const NEAR_MARGIN = "900px 0px";

function isTarget(id: string): boolean {
  return typeof window !== "undefined" && window.location.hash === `#insight-${id}`;
}

export type TileEntry = { result?: InsightResult; error?: { code: string; message?: string; status: number }; later?: boolean; waiting?: boolean };

/** What a person can do to a tile while arranging their dashboard. */
export type Arrange = {
  up?: () => void;
  down?: () => void;
  off: () => void;
  startPointer: (e: React.PointerEvent<HTMLElement>) => void;
  dragging: boolean;
  dragOver?: boolean;
};

/** Pointer-based direct drag hooks available on every tile even outside Customize mode. */
export type QuickDrag = {
  startPointer: (e: React.PointerEvent<HTMLElement>) => void;
  stepUp?: () => void;
  stepDown?: () => void;
  dragging: boolean;
  dragOver?: boolean;
};

export const InsightTile = memo(function InsightTile({
  item,
  onRemoved,
  entry,
  streaming,
  arrange,
  quickDrag,
  sizeOverride,
  heightOverride,
  onResize,
  onHeightResize,
  onRemoveFromCanvas,
  fillSolo,
}: {
  item: InsightInfo;
  onRemoved?: () => void;
  entry?: TileEntry;
  streaming?: boolean;
  arrange?: Arrange;
  quickDrag?: QuickDrag;
  sizeOverride?: BentoSize;
  heightOverride?: BentoHeight;
  onResize?: (size: BentoSize) => void;
  onHeightResize?: (height: BentoHeight) => void;
  onRemoveFromCanvas?: () => void;
  fillSolo?: boolean;
}) {
  const [data, setData] = useState<InsightResult | null>(() => (streaming !== undefined && !entry?.later && entry?.result ? entry.result : null));
  const [error, setError] = useState<unknown>(() => (streaming !== undefined && !entry?.later && entry?.error ? new ApiError(entry.error.status, entry.error.code, undefined, entry.error.message) : null));
  const [open, setOpen] = useState(false);
  const [sqlDirect, setSqlDirect] = useState(false);
  const [infoOpen, setInfoOpen] = useState(false);
  const [whyOpen, setWhyOpen] = useState(false);
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<"chart" | "table">("chart");
  const [round, setRound] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const [preparing, setPreparing] = useState(() => Boolean(entry?.waiting));
  const [all, setAll] = useState(false);
  const [proposeState, setProposeState] = useState<"idle" | "saving" | "sent" | "failed">("idle");
  const [proposeProblem, setProposeProblem] = useState("");
  const retry = useCallback(() => setRound((n) => n + 1), []);
  const [filtered, setFiltered] = useState(false);
  const [work, setWork] = useState<InsightResult["work"]>(null);
  useEffect(() => {
    if (data?.work && !filtered) setWork(data.work);
  }, [data, filtered]);
  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  const here = useRef<HTMLElement>(null);
  const fed = streaming !== undefined && round === 0 && !entry?.later;
  useEffect(() => {
    if (entry?.waiting && !data) setPreparing(true);
  }, [entry?.waiting, data]);
  useEffect(() => {
    if (!fed) return;
    if (entry?.result) { setData(entry.result); setError(null); setPreparing(false); }
    else if (entry?.error) { setError(new ApiError(entry.error.status, entry.error.code, undefined, entry.error.message)); setPreparing(false); }
  }, [fed, entry]);

  const noObserver = typeof IntersectionObserver === "undefined";
  const [near, setNear] = useState(() => noObserver || isTarget(item.id));
  const [inView, setInView] = useState(() => noObserver || isTarget(item.id));
  const body = useRef<HTMLDivElement>(null);
  const drawnHeight = useRef(180);
  useEffect(() => {
    if (!here.current || typeof IntersectionObserver === "undefined") return;
    const seen = new IntersectionObserver((entries) => {
      const on = entries.some((e) => e.isIntersecting);
      if (on) setNear(true);
      else if (body.current?.offsetHeight) drawnHeight.current = body.current.offsetHeight;
      setInView(on);
    }, { rootMargin: NEAR_MARGIN });
    seen.observe(here.current);
    return () => seen.disconnect();
  }, []);
  useEffect(() => {
    if (fed || !near) return;
    const ctl = new AbortController();
    setError(null);
    setRefreshing(round > 0);
    const onStored = Object.assign((d: InsightResult) => { setPreparing(false); setData(d); }, {
      onFilling: () => { if (!ctl.signal.aborted) setPreparing(true); },
    });
    getInsight(item.id, ctl.signal, round > 0, onStored).then(
      (d) => { setData(d); setPreparing(false); setRefreshing(false); },
      (e) => { if (!ctl.signal.aborted) { setError(e); setPreparing(false); setRefreshing(false); } },
    );
    return () => ctl.abort();
  }, [item.id, round, near, fed]);
  useEffect(() => {
    if (data && typeof window !== "undefined" && window.location.hash === `#insight-${item.id}`) {
      here.current?.scrollIntoView?.({ block: "start" });
    }
  }, [data, item.id]);

  const remove = async () => {
    try {
      await deleteInsight(item.id);
      onRemoved?.();
    } catch (e) {
      setError(e);
    }
  };
  const propose = async () => {
    if (!data?.sql) return;
    setProposeState("saving");
    setProposeProblem("");
    try {
      await proposeMeasureCandidate({
        insight_id: item.id,
        title: item.title,
        question: item.question || undefined,
        sql: data.sql,
        source_kind: "saved_tile",
      });
      setProposeState("sent");
    } catch (e) {
      setProposeProblem(plainError(e));
      setProposeState("failed");
    }
  };

  const [dragSizePreview, setDragSizePreview] = useState<BentoSize | null>(null);
  const [dragHeightPreview, setDragHeightPreview] = useState<BentoHeight | null>(null);
  const [resizingNow, setResizingNow] = useState(false);

  const effectiveSize: BentoSize = dragSizePreview ?? sizeOverride ?? defaultTileSize(item, data);
  const effectiveHeight: BentoHeight | undefined = dragHeightPreview ?? heightOverride;
  const colSpanClass =
    fillSolo && !sizeOverride && !dragSizePreview && effectiveSize === "m"
      ? "lg:col-span-12"
      : BENTO_COL_SPAN[effectiveSize];
  const chartBoxClass = effectiveHeight ? BENTO_HEIGHT_BOX[effectiveHeight] : BENTO_CHART_BOX[effectiveSize];

  const startPointerResize = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (!onResize || e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const tileEl = here.current;
    const gridEl = tileEl?.closest('[data-testid="bento-grid"]') as HTMLElement | null;
    const gridW = gridEl?.getBoundingClientRect().width || tileEl?.getBoundingClientRect().width || 960;
    const tileStartW = tileEl?.getBoundingClientRect().width || (gridW * BENTO_SIZE_COLS[effectiveSize]) / 12;
    const startX = e.clientX;
    const startY = e.clientY;
    const startHeight: BentoHeight = effectiveHeight ?? "standard";
    let latestSize: BentoSize = effectiveSize;
    let latestHeight: BentoHeight = startHeight;

    setResizingNow(true);
    setDragSizePreview(latestSize);
    setDragHeightPreview(startHeight);

    const onMove = (ev: PointerEvent) => {
      const dx = ev.clientX - startX;
      const dy = ev.clientY - startY;
      const nextCols = Math.max(3, Math.min(12, ((tileStartW + dx) / Math.max(320, gridW)) * 12));
      latestSize = snapColsToBentoSize(nextCols);
      latestHeight = snapHeightDeltaToBentoHeight(startHeight, dy);
      setDragSizePreview(latestSize);
      setDragHeightPreview(latestHeight);
    };

    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      setResizingNow(false);
      setDragSizePreview(null);
      setDragHeightPreview(null);
      onResize(latestSize);
      if (onHeightResize && latestHeight !== startHeight) {
        onHeightResize(latestHeight);
      }
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  };

  const activeDrag = arrange ?? quickDrag;
  const kpi = useMemo(() => extractKpiSummary(item, data), [item, data]);
  const isPureKpiCard = Boolean(
    kpi &&
    kpi.kind === "kpi" &&
    view === "chart" &&
    (data?.drawn?.kind === "kpi" || (!data?.drawn && (data?.rows ?? []).length === 1 && (data?.columns ?? []).length === 1)),
  );
  const showCompactKpiStrip = Boolean(
    kpi &&
    kpi.kind === "series" &&
    effectiveSize === "s" &&
    view === "chart" &&
    data?.drawn &&
    !data.drawn.panels?.length,
  );
  const hasMeasureWhy = Boolean(item.measure && item.id.startsWith("measure_") && (data?.columns ?? []).includes("month"));

  const asOfDate = data?.drawn?.as_of ?? data?.as_of ?? null;
  const isMultiPanel = Boolean(data?.drawn?.panels?.length);
  const isCurrencyFacet = isMultiPanel && CURRENCY_FACET.test(data?.drawn?.spec?.facet ?? "");
  const rowCount = data ? (data.row_count ?? data.rows?.length ?? 0) : 0;
  const totalPoints = data?.drawn ? chartPointCount(data.drawn, data.total_rows ?? rowCount) : 0;
  const isCut = Boolean(data?.drawn && topOf(data.drawn) !== data.drawn);
  const countText = data
    ? isCut && !all
      ? isMultiPanel
        ? isCurrencyFacet
          ? `Top ${TOP} per currency`
          : `Top ${TOP} per group`
        : `Top ${TOP} of ${totalPoints}`
      : data.truncated
        ? data.total_rows
          ? `First ${rowCount} of ${data.total_rows} rows`
          : `First ${rowCount} rows (more not counted)`
        : `${rowCount} ${rowCount === 1 ? "row" : "rows"}`
    : "";
  const footerMeta = [
    asOfDate ? `As of ${formatDate(asOfDate, { short: true })}` : null,
    countText || null,
    data?.read_at && (data.truncated || isBeforeToday(asOfDate)) ? `read ${ago(data.read_at)}` : null,
    refreshing ? "updating" : round > 0 && error ? "refresh failed" : null,
  ].filter(Boolean);

  const toggleSqlInspector = () => {
    if (open && sqlDirect) {
      setOpen(false);
      setSqlDirect(false);
    } else {
      setOpen(true);
      setSqlDirect(true);
    }
  };

  return (
    <section
      id={`insight-${item.id}`}
      ref={here}
      data-bento-size={effectiveSize}
      data-bento-height={effectiveHeight ?? "standard"}
      data-flip-id={item.id}
      data-drag-target={activeDrag?.dragOver ? "true" : undefined}
      className={cn(
        "group/tile cv-auto cv-tile relative flex min-w-0 scroll-mt-24 flex-col gap-3 rounded-lg border bg-bg-subtle p-4 transition-[border-color,box-shadow,opacity,background-color] duration-200 ease-[cubic-bezier(0.22,1,0.36,1)]",
        colSpanClass,
        resizingNow
          ? "z-10 border-accent ring-1 ring-accent/40 select-none"
          : activeDrag?.dragOver
            ? "border-accent bg-row-on/80 ring-1 ring-accent/40"
            : arrange
              ? "border-border-strong/90 hover:border-fg/40"
              : "border-border hover:border-border-strong",
        activeDrag?.dragging && "scale-[0.985] opacity-40 ring-1 ring-accent/40",
      )}
      data-testid={`insight-${item.id}`}
      aria-labelledby={`insight-${item.id}-title`}
    >
      {/* Genie-Clean Single-Line Header: Title + quiet domain meta on left, Chart|Table + overflow menu on right */}
      <header className="space-y-2">
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 flex-1 items-center gap-1.5">
            {activeDrag && (
              <span
                role="button"
                tabIndex={0}
                aria-label={`Drag handle for ${item.title}`}
                title="Drag to reorder"
                onPointerDown={(e) => {
                  if (!resizingNow) activeDrag.startPointer(e);
                }}
                onKeyDown={(e) => {
                  const goUp = arrange?.up ?? quickDrag?.stepUp;
                  const goDown = arrange?.down ?? quickDrag?.stepDown;
                  if (e.key === "ArrowUp" && goUp) { e.preventDefault(); goUp(); }
                  if (e.key === "ArrowDown" && goDown) { e.preventDefault(); goDown(); }
                }}
                className={cn(
                  "inline-flex size-6 shrink-0 touch-none cursor-grab items-center justify-center rounded-md text-faint transition-opacity hover:bg-hover hover:text-fg active:cursor-grabbing",
                  arrange || activeDrag.dragging
                    ? "opacity-100 text-muted"
                    : "opacity-0 group-hover/tile:opacity-100 focus-visible:opacity-100",
                )}
                data-testid={`drag-handle-${item.id}`}
              >
                <GripVertical className="size-3.5" strokeWidth={2} aria-hidden />
              </span>
            )}
            <h3
              id={`insight-${item.id}-title`}
              className="min-w-0 truncate text-[15px] font-semibold leading-5 tracking-[-0.015em] text-fg"
              title={item.title}
            >
              {item.title}
            </h3>
            {item.measure && (
              <span
                title={`${item.measure.name}: ${item.measure.definition} (${definitionMeta(item.measure)})`}
                className="hidden shrink-0 items-center gap-1.5 text-[11.5px] font-normal text-faint sm:inline-flex"
                data-testid={`verified-pill-${item.id}`}
              >
                <span>{item.measure.owner}</span>
                <span className="h-2.5 w-px bg-border" aria-hidden />
                <span className="font-mono">{item.measure.version}</span>
              </span>
            )}
          </div>

          <div className="flex shrink-0 items-center gap-1.5">
            {/* Right Header Cluster: Resize presets in Customize mode OR compact Chart|Table toggle */}
            {arrange && onResize ? (
              <div
                className="inline-flex shrink-0 items-center rounded-md border border-border bg-bg p-0.5"
                role="group"
                aria-label={`Resize ${item.title}`}
                data-testid={`tile-resize-${item.id}`}
              >
                {BENTO_SIZE_OPTIONS.map((opt) => (
                  <button
                    key={opt.size}
                    type="button"
                    aria-pressed={effectiveSize === opt.size}
                    title={opt.label}
                    onClick={() => onResize(opt.size)}
                    className={cn(
                      "inline-flex h-5 items-center rounded px-1.5 text-[11px] font-medium cursor-pointer",
                      effectiveSize === opt.size
                        ? "bg-fg text-bg"
                        : "text-muted hover:bg-hover hover:text-fg",
                    )}
                  >
                    {opt.short}
                  </button>
                ))}
              </div>
            ) : data?.drawn && !data.not_measurable && data.drawn.kind !== "table" ? (
              <div
                className="inline-flex shrink-0 items-center rounded-md border border-border/70 bg-bg/80 p-0.5"
                role="group"
                aria-label={`View mode for ${item.title}`}
                data-testid={`tile-view-toggle-${item.id}`}
              >
                <button
                  type="button"
                  aria-pressed={view === "chart"}
                  title="Chart view"
                  onClick={() => setView("chart")}
                  className={cn(
                    "inline-flex h-5 items-center gap-1 rounded px-1.5 text-[11px] font-medium cursor-pointer",
                    view === "chart" ? "bg-surface-2 text-fg" : "text-muted hover:text-fg",
                  )}
                >
                  <BarChart3 className="size-3 shrink-0" strokeWidth={2} aria-hidden />
                  <span className="sr-only sm:not-sr-only">Chart</span>
                </button>
                <button
                  type="button"
                  aria-pressed={view === "table"}
                  title="Table view"
                  onClick={() => setView("table")}
                  className={cn(
                    "inline-flex h-5 items-center gap-1 rounded px-1.5 text-[11px] font-medium cursor-pointer",
                    view === "table" ? "bg-surface-2 text-fg" : "text-muted hover:text-fg",
                  )}
                >
                  <Table2 className="size-3 shrink-0" strokeWidth={2} aria-hidden />
                  <span className="sr-only sm:not-sr-only">Table</span>
                </button>
              </div>
            ) : null}

            {/* Clean Genie "..." overflow menu holding Why?, Layout, Export, Schedule, Definition, Remove */}
            {!arrange && (data || item.saved || onResize || onRemoveFromCanvas) && (
              <div ref={menuRef} className="relative">
                <button
                  type="button"
                  aria-expanded={menuOpen}
                  aria-haspopup="menu"
                  aria-label={`More actions for ${item.title}`}
                  title="Tile actions"
                  onClick={() => setMenuOpen((v) => !v)}
                  data-testid={`tile-menu-${item.id}`}
                  className={cn(
                    "inline-flex size-7 items-center justify-center rounded-md text-muted transition-colors hover:bg-hover hover:text-fg cursor-pointer",
                    menuOpen && "bg-surface-2 text-fg",
                  )}
                >
                  <MoreHorizontal className="size-4" strokeWidth={2} aria-hidden />
                </button>

                {menuOpen && (
                  <div
                    role="menu"
                    aria-label={`Actions for ${item.title}`}
                    data-testid={`tile-menu-popover-${item.id}`}
                    className="liquid-glass-dock absolute right-0 top-full z-30 mt-1.5 w-60 space-y-2 rounded-lg p-2 text-[12.5px] text-fg shadow-pop"
                  >
                    <div className="space-y-0.5">
                      {data && !data.not_measurable && (
                        hasMeasureWhy ? (
                          <button
                            type="button"
                            role="menuitem"
                            onClick={() => {
                              setWhyOpen((v) => !v);
                              setMenuOpen(false);
                            }}
                            data-testid={`why-trigger-${item.id}`}
                            className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-fg transition-colors hover:bg-hover cursor-pointer"
                          >
                            <GitBranch className="size-3.5 text-muted" strokeWidth={2} aria-hidden />
                            <span>{whyOpen ? "Hide breakdown" : "Why did it change?"}</span>
                          </button>
                        ) : (
                          <Link
                            prefetch={false}
                            role="menuitem"
                            href={`/ask?q=${encodeURIComponent(askAbout(item.title))}`}
                            onClick={() => setMenuOpen(false)}
                            className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-fg transition-colors hover:bg-hover"
                          >
                            <Sparkles className="size-3.5 text-muted" strokeWidth={2} aria-hidden />
                            <span>Investigate in Ask</span>
                          </Link>
                        )
                      )}

                      {data?.about && !data.not_measurable && (
                        <button
                          type="button"
                          role="menuitem"
                          onClick={() => {
                            setInfoOpen((v) => !v);
                            setMenuOpen(false);
                          }}
                          className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-fg transition-colors hover:bg-hover cursor-pointer"
                        >
                          <HelpCircle className="size-3.5 text-muted" strokeWidth={2} aria-hidden />
                          <span>{infoOpen ? "Hide explanation" : "How it is calculated"}</span>
                        </button>
                      )}

                      {item.saved && (
                        <button
                          type="button"
                          role="menuitem"
                          onClick={() => {
                            setScheduleOpen((v) => !v);
                            setMenuOpen(false);
                          }}
                          data-testid={`schedule-trigger-${item.id}`}
                          className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-fg transition-colors hover:bg-hover cursor-pointer"
                        >
                          <Clock className="size-3.5 text-muted" strokeWidth={2} aria-hidden />
                          <span>{item.schedule ? `Schedule (${item.schedule})` : "Schedule alerts"}</span>
                        </button>
                      )}

                      {item.saved && data?.sql && (
                        proposeState === "sent" ? (
                          <div className="px-2.5 py-1.5 text-[12px] text-muted" data-testid={`proposed-${item.id}`}>
                            Proposed for review
                          </div>
                        ) : (
                          <button
                            type="button"
                            role="menuitem"
                            disabled={proposeState === "saving"}
                            onClick={() => {
                              void propose();
                              setMenuOpen(false);
                            }}
                            data-testid={`propose-${item.id}`}
                            className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-fg transition-colors hover:bg-hover disabled:opacity-50 cursor-pointer"
                          >
                            <Plus className="size-3.5 text-muted" strokeWidth={2} aria-hidden />
                            <span>{proposeState === "saving" ? "Proposing" : "Propose as shared definition"}</span>
                          </button>
                        )
                      )}
                    </div>

                    {data && !data.not_measurable && (
                      <div className="border-t border-border/70 pt-2 px-1">
                        {data.drawn ? (
                          <ChartSaves
                            chart={all ? data.drawn : topOf(data.drawn)}
                            figure={here}
                            label="Export"
                            align="left"
                            includeEmail
                            question={item.title}
                          />
                        ) : (
                          <TableExportMenu data={data} />
                        )}
                      </div>
                    )}

                    {onResize && (
                      <div className="space-y-2 border-t border-border/70 px-1 pt-2">
                        <div className="flex items-center justify-between">
                          <span className="text-[11px] font-medium uppercase tracking-[0.04em] text-faint">Width</span>
                          <div className="inline-flex items-center gap-0.5 rounded-md border border-border bg-bg p-0.5">
                            {BENTO_SIZE_OPTIONS.map((opt) => (
                              <button
                                key={opt.size}
                                type="button"
                                aria-pressed={effectiveSize === opt.size}
                                onClick={() => onResize(opt.size)}
                                className={cn(
                                  "h-5 rounded px-1.5 text-[11px] font-medium cursor-pointer",
                                  effectiveSize === opt.size ? "bg-fg text-bg" : "text-muted hover:text-fg",
                                )}
                              >
                                {opt.short}
                              </button>
                            ))}
                          </div>
                        </div>
                        {onHeightResize && (
                          <div className="flex items-center justify-between">
                            <span className="text-[11px] font-medium uppercase tracking-[0.04em] text-faint">Height</span>
                            <div className="inline-flex items-center gap-0.5 rounded-md border border-border bg-bg p-0.5">
                              {BENTO_HEIGHT_OPTIONS.map((opt) => (
                                <button
                                  key={opt.height}
                                  type="button"
                                  aria-pressed={(effectiveHeight ?? "standard") === opt.height}
                                  onClick={() => onHeightResize(opt.height)}
                                  className={cn(
                                    "h-5 rounded px-1.5 text-[11px] font-medium cursor-pointer",
                                    (effectiveHeight ?? "standard") === opt.height ? "bg-fg text-bg" : "text-muted hover:text-fg",
                                  )}
                                >
                                  {opt.label}
                                </button>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    )}

                    {(item.saved || onRemoveFromCanvas) && (
                      <div className="border-t border-border/70 pt-1">
                        {item.saved ? (
                          <button
                            type="button"
                            role="menuitem"
                            onClick={() => {
                              setMenuOpen(false);
                              void remove();
                            }}
                            className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-late transition-colors hover:bg-hover cursor-pointer"
                          >
                            <EyeOff className="size-3.5" strokeWidth={2} aria-hidden />
                            <span>Remove</span>
                          </button>
                        ) : onRemoveFromCanvas ? (
                          <button
                            type="button"
                            role="menuitem"
                            onClick={() => {
                              setMenuOpen(false);
                              onRemoveFromCanvas();
                            }}
                            className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-muted transition-colors hover:bg-hover hover:text-fg cursor-pointer"
                          >
                            <EyeOff className="size-3.5" strokeWidth={2} aria-hidden />
                            <span>Remove from canvas</span>
                          </button>
                        ) : null}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        {/* Customize Layout Row */}
        {arrange && (
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/70 pt-2" data-testid={`arrange-${item.id}`}>
            <span className="text-[12px] text-muted">Drag handle to reorder or resize from corner</span>
            <div className="flex flex-wrap items-center gap-1.5">
              <button type="button" className={quiet} disabled={!arrange.up} onClick={arrange.up} aria-label={`Move ${item.title} up`}>
                <span className="inline-flex items-center gap-1">
                  <ArrowUp className="size-3" aria-hidden />
                  Up
                </span>
              </button>
              <button type="button" className={quiet} disabled={!arrange.down} onClick={arrange.down} aria-label={`Move ${item.title} down`}>
                <span className="inline-flex items-center gap-1">
                  <ArrowDown className="size-3" aria-hidden />
                  Down
                </span>
              </button>
              <button type="button" className={quiet} onClick={arrange.off} aria-label={`Take ${item.title} off your dashboard`}>
                <span className="inline-flex items-center gap-1">
                  <EyeOff className="size-3" aria-hidden />
                  Remove from dashboard
                </span>
              </button>
            </div>
          </div>
        )}

        {infoOpen && data?.about && !data.not_measurable && (
          <p className="rounded-lg bg-bg px-3 py-2 text-[12.5px] leading-5 text-muted" data-testid={`takeaway-${item.id}`}>
            {data.about}
          </p>
        )}
      </header>

      {item.saved && scheduleOpen && (
        <div className="border-t border-border/60 pt-2">
          <ScheduleControl item={item} columns={data?.columns} />
        </div>
      )}

      {error ? (
        <LoadProblem message={tileError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
      ) : !near && !fed && !data ? (
        <div className="h-52" data-testid={`unread-${item.id}`} />
      ) : !data ? (
        <div className="min-h-52" aria-busy>
          <ChartSkeleton />
          <Pending label={preparing ? "Preparing chart..." : "Reading"} limit={90_000} onRetry={retry} bars={0} />
        </div>
      ) : !inView ? (
        <div style={{ height: drawnHeight.current }} data-testid={`undrawn-${item.id}`} aria-hidden />
      ) : (
        <div ref={body} className="flex flex-1 flex-col gap-3">
          {data.not_measurable && (
            <p className="text-[13px] text-muted" data-testid={`unmeasurable-${item.id}`}>
              {data.not_measurable}
            </p>
          )}
          {data.not_measurable ? null : isPureKpiCard && kpi ? (
            <KpiCardBody kpi={kpi} title={item.title} />
          ) : data.drawn && view === "chart" ? (
            <div className="space-y-2">
              {showCompactKpiStrip && kpi && <KpiCardBody kpi={kpi} title={item.title} compact />}
              <div
                className={cn(all ? "max-h-[28rem] overflow-y-auto" : chartBoxClass)}
                data-testid={`chart-body-${item.id}`}
              >
                <AnswerCharts charts={[all ? data.drawn : topOf(data.drawn)]} caption={false} hideFooter />
              </div>
            </div>
          ) : (
            <Rows data={data} />
          )}

          {(data.excluded ?? []).map((x) => (
            <p key={x.parameter} className="text-[12px] text-muted" data-testid={`excluded-${item.id}-${x.parameter}`}>
              {x.n === null || x.n === undefined ? `Not counted: what is ${x.says} (${x.why ?? "not read"})` : `${x.n.toLocaleString()} more ${x.says}`}
            </p>
          ))}

          {["reconciles", "differs"].includes((data as InsightResult & { checked?: { status: string } | null }).checked?.status ?? "") && (
            <p className={`text-[12px] ${(data as InsightResult & { checked?: { status: string } | null }).checked?.status === "differs" ? "text-late" : "text-muted"}`} data-testid={`checked-${item.id}`}>
              Checked: {(data as InsightResult & { checked?: { says: string } | null }).checked?.says}
            </p>
          )}

          {(item.fixed_dates || filtered) && (
            <p className="flex flex-wrap items-center gap-x-2 text-[12px] text-muted">
              {item.fixed_dates && <span data-testid={`fixed-${item.id}`}>Dates fixed when pinned: {item.fixed_dates}</span>}
              {filtered && <span data-testid={`filtered-${item.id}`}>Your filters (not saved)</span>}
            </p>
          )}

          {/* Multi-dimension Why? attribution panel */}
          {hasMeasureWhy && whyOpen && (
            <div className="rounded-lg border border-border/70 bg-bg/70 p-3" data-testid={`why-panel-${item.id}`}>
              <WhyDidItChange id={item.id} testId={`why-${item.id}`} autoStart />
            </div>
          )}

          {/* Quiet Databricks Genie-style Footer Bar */}
          <footer className="mt-auto flex flex-wrap items-center justify-between gap-2 border-t border-border/60 pt-2.5">
            <div className="flex flex-wrap items-center gap-2 text-[12px] text-faint tabular-nums">
              {data.sql && (
                <button
                  type="button"
                  aria-expanded={open && sqlDirect}
                  onClick={toggleSqlInspector}
                  data-testid={`sql-trigger-${item.id}`}
                  className={cn(
                    "inline-flex h-6 items-center gap-1 rounded-md px-1.5 text-[12px] font-medium transition-colors hover:bg-hover hover:text-fg cursor-pointer",
                    open && sqlDirect ? "bg-surface-2 text-fg" : "text-muted",
                  )}
                  title="Show governed SQL query"
                >
                  <Code2 className="size-3 shrink-0" strokeWidth={2} aria-hidden />
                  Show SQL
                </button>
              )}
              {footerMeta.length > 0 && (
                <span className="inline-flex flex-wrap items-center gap-x-2.5 gap-y-0.5">
                  {footerMeta.map((meta, idx) => (
                    <span key={idx} className="inline-flex items-center gap-2.5">
                      {idx > 0 && <span className="h-3 w-px bg-border" aria-hidden />}
                      <span>{meta}</span>
                    </span>
                  ))}
                </span>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-1">
              {isCut && view === "chart" && (
                <button
                  type="button"
                  className="inline-flex h-6 items-center rounded-md px-1.5 text-[11.5px] font-medium text-accent hover:bg-hover cursor-pointer"
                  onClick={() => setAll((a) => !a)}
                >
                  {all ? `Show top ${TOP}` : `Show all ${totalPoints}`}
                </button>
              )}
              <Link
                prefetch={false}
                href={`/ask?q=${encodeURIComponent(askAbout(item.title))}`}
                className="inline-flex h-6 items-center rounded-md px-1.5 text-[12px] font-medium text-muted hover:bg-hover hover:text-fg"
              >
                Ask about this
              </Link>
              <button
                type="button"
                className="inline-flex h-6 items-center rounded-md px-1.5 text-[12px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
                aria-expanded={open}
                onClick={() => {
                  setOpen((o) => !o);
                  setSqlDirect(false);
                }}
              >
                {open ? "Hide details" : "Details"}
              </button>
              {proposeState === "sent" && (
                <span className="px-1.5 text-[11.5px] text-ok" data-testid={`proposed-${item.id}`}>Proposed for review</span>
              )}
              {proposeState === "failed" && proposeProblem && <span className="text-[11.5px] text-muted" role="alert">{proposeProblem}</span>}
              <button
                type="button"
                aria-label="Refresh"
                title="Refresh"
                onClick={() => { setFiltered(false); retry(); }}
                className="inline-flex size-6 items-center justify-center rounded-md text-muted hover:bg-hover hover:text-fg cursor-pointer"
              >
                <RotateCw className={cn("size-3", refreshing && "animate-spin")} strokeWidth={2} aria-hidden />
                <span className="sr-only">Refresh</span>
              </button>
            </div>
          </footer>

          {/* Details / Governed Definition, SQL & Filters Drawer */}
          {open && (
            <div className="space-y-2.5 border-t border-border/60 pt-2.5" data-testid="insight-definition">
              {item.measure && typeof item.measure === "object" && (item.measure.name || item.measure.definition) && (
                <div className="text-[12px] leading-5 text-muted [&_dl]:text-[12px] [&_[data-testid='verified-measure-badge']]:hidden">
                  <DefinitionLines definitions={[item.measure]} testId={`measure-definition-${item.id}`} />
                </div>
              )}
              {!infoOpen && data.about && !data.not_measurable && (
                <p className="text-[12.5px] leading-5 text-muted">{data.about}</p>
              )}
              {work && (
                <ShowWork
                  key={round}
                  work={work}
                  testId={`insight-work-${item.id}`}
                  read={async (edits) => {
                    setData(await readInsightFiltered(item.id, edits));
                    setFiltered(true);
                  }}
                  onReset={() => {
                    setFiltered(false);
                    retry();
                  }}
                />
              )}
              {sqlDirect ? (
                <div className="space-y-1 text-[12px]" data-testid={`insight-query-${item.id}`}>
                  <div className="text-[11.5px] font-medium text-muted">Governed SQL query</div>
                  <pre className="overflow-x-auto whitespace-pre-wrap rounded-md border border-border bg-bg p-2.5 font-mono text-[11.5px] text-fg">
                    {data.sql}
                  </pre>
                </div>
              ) : (
                <QueryToggle sql={data.sql} testId={`insight-query-${item.id}`} />
              )}
              {data.drawn && !sqlDirect && <Rows data={data} />}
            </div>
          )}
        </div>
      )}

      {/* Corner Drag-to-Resize Handle (pointer-only, zero conflicting onClick!) + Live Liquid Glass Ghost Badge */}
      {onResize && (
        <>
          {resizingNow && (
            <div
              className="liquid-glass-dock pointer-events-none absolute right-3 bottom-3 z-20 inline-flex items-center gap-2 rounded-lg px-2.5 py-1 text-[11.5px] font-medium text-fg tabular-nums"
              data-testid={`resize-preview-${item.id}`}
            >
              <span>{formatResizeBadge(effectiveSize, effectiveHeight).replace(/\s*\u00b7\s*/g, " / ")}</span>
              <span className="h-3 w-px bg-border" aria-hidden />
              <span className="text-muted">{BENTO_SIZE_COLS[effectiveSize]}/12</span>
            </div>
          )}
          <button
            type="button"
            onPointerDown={startPointerResize}
            aria-label={`Drag corner to resize ${item.title}`}
            title="Drag corner to resize width and height"
            data-testid={`resize-handle-${item.id}`}
            className={cn(
              "absolute right-1 bottom-1 inline-flex size-6 touch-none cursor-nwse-resize items-end justify-end rounded p-1 text-faint transition-opacity hover:text-fg",
              arrange || resizingNow ? "opacity-100 text-muted" : "opacity-0 group-hover/tile:opacity-85 focus-visible:opacity-100",
            )}
          >
            <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden>
              <path d="M10 1.5L1.5 10M10 5.5L5.5 10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </button>
        </>
      )}
    </section>
  );
});

const MONITOR_CADENCE: Record<string, string> = {
  hourly: "Hourly schedule",
  daily: "Daily schedule",
  weekly: "Weekly (Mon)",
};

/** What each monitor checks and when, in a crisp single line. */
export function monitorLine(item: InsightInfo): string {
  const when = MONITOR_CADENCE[item.schedule] ?? SCHEDULES.find(([v]) => v === item.schedule)?.[1] ?? "";
  const w = item.watch;
  const notify = item.notify as string | undefined;
  let rule = "";
  if (notify === "crosses" && w) {
    rule = `alerts when ${watchSubject(w)} goes ${w.op} ${w.value}`;
  } else if (notify === "never") {
    rule = "silent on Home";
  } else if (notify === "new_rows") {
    rule = "alerts on new rows";
  } else if (notify === "rows") {
    rule = "alerts when rows appear";
  } else if (notify === "every_run" || notify === "always") {
    rule = "alerts every run";
  } else if (notify === "on_change") {
    rule = "alerts on change";
  }
  if (when && rule) return `${when}, ${rule}`;
  return when || rule || "Scheduled";
}

/** Compact single-line hairline strip for active scheduled monitors (zero rounded-box slop). */
function Monitors({ items }: { items: InsightInfo[] }) {
  const on = items.filter((i) => i.saved && i.schedule);
  if (on.length === 0) return null;
  return (
    <section aria-label="Monitors" className="border-y border-border/80 py-2" data-testid="monitors">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[12.5px]">
        <span className="inline-flex shrink-0 items-center gap-1.5 font-medium text-fg">
          <Bell className="size-3.5 text-muted" strokeWidth={2} aria-hidden />
          Monitors
          <span className="num text-[11.5px] font-normal text-muted">({on.length})</span>
        </span>
        <ul className="flex min-w-0 flex-1 flex-wrap items-center divide-x divide-border/70">
          {on.map((i) => (
            <li key={i.id} className="inline-flex flex-wrap items-baseline gap-x-2 px-3 first:pl-0 last:pr-0">
              <a href={`#insight-${i.id}`} className="font-medium text-fg hover:text-accent hover:underline">{i.title}</a>
              <span className="text-[12px] text-muted">{monitorLine(i)}</span>
              {i.last_run && (
                <span className="text-[11.5px] text-faint tabular-nums">
                  {ago(i.last_run) ?? formatDate(i.last_run, { short: true })}
                </span>
              )}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

type Measure = NonNullable<InsightList["measures"]>[number];

/** Exported helper component for agreed measures list. */
export function Measures({ measures }: { measures: Measure[] }) {
  const [open, setOpen] = useState(false);
  if (measures.length === 0) return null;
  return (
    <section className="rounded-lg border border-border/80 bg-surface/50 p-4 space-y-3" aria-label="Measures">
      <div className="flex items-baseline gap-2">
        <h2 className="flex-1 text-[14.5px] font-semibold">Measures</h2>
        <button type="button" className={quiet} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? "Hide" : `Show all ${measures.length}`}
        </button>
      </div>
      <p className="text-[12.5px] text-muted">The agreed definitions behind every figure Ask and these charts give.</p>
      {open && (
        <ul className="divide-y divide-border text-[13px]">
          {measures.map((m) => (
            <li key={m.id} className="space-y-1 py-2.5">
              <div className="flex flex-wrap items-baseline gap-x-2.5">
                <span className="font-medium">{m.name}</span>
                <span className="inline-flex flex-wrap items-center gap-2 text-[12px] text-muted">
                  <span>{m.owner}</span>
                  <span className="h-3 w-px bg-border" aria-hidden />
                  <span>{m.unit}</span>
                  <span className="h-3 w-px bg-border" aria-hidden />
                  <span>from {m.source}</span>
                </span>
              </div>
              <p className="text-muted">{m.definition}</p>
              <p className="text-[12px] text-muted">
                Split by {m.dimensions.join(", ")}. Reads {m.period}.
              </p>
              {m.ask && (
                <Link prefetch={false} href={`/ask?q=${encodeURIComponent(m.ask)}`} className="text-[12px] text-accent hover:underline">
                  {m.ask}
                </Link>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Customize bar: lets you reorder, resize, hide/show charts, or reset to standard layout. */
function Customise({
  list,
  hiddenItems,
  editing,
  bentoCustom,
  onToggle,
  onAddBack,
  onReset,
  inHeader,
}: {
  list: InsightList;
  hiddenItems: InsightInfo[];
  editing: boolean;
  bentoCustom: boolean;
  onToggle: () => void;
  onAddBack: (item: InsightInfo) => void;
  onReset: () => void;
  inHeader?: boolean;
}) {
  const [find, setFind] = useState("");
  const q = find.trim().toLowerCase();
  const shownHidden = q ? hiddenItems.filter((h) => `${h.title} ${h.group}`.toLowerCase().includes(q)) : hiddenItems;
  const anyCustom = Boolean(list.customised || bentoCustom);
  if (inHeader && !editing && !anyCustom && !list.layout_problem) return null;
  return (
    <section className="space-y-3" aria-label="Customize your dashboard" data-testid="customise">
      <div className={cn(
        "flex flex-wrap items-center gap-2.5",
        editing && "rounded-lg border border-border-strong bg-surface/50 px-3.5 py-2.5",
      )}>
        {!inHeader && (
          <button type="button" className={quiet} aria-pressed={editing} onClick={onToggle}>
            {editing ? "Done" : "Customize"}
          </button>
        )}
        {editing && (
          <button
            type="button"
            className={quiet}
            disabled={!anyCustom}
            onClick={onReset}
            data-testid="reset-bento-layout"
          >
            <span className="inline-flex items-center gap-1.5">
              <RotateCcw className="size-3.5" aria-hidden />
              Reset to the standard dashboard
            </span>
          </button>
        )}
        {!editing && anyCustom && (
          <>
            <span className="text-[12.5px] text-muted">Custom layout active</span>
            <button type="button" className={quiet} onClick={onReset} data-testid="reset-bento-layout">
              Back to the standard dashboard
            </button>
          </>
        )}
        {editing && (
          <span className="text-[12.5px] text-muted">
            Drag any tile handle to reorder, drag corners or pick S / M / L / Full width, or remove tiles below.
          </span>
        )}
      </div>
      {list.layout_problem && <p className="text-[13.5px] text-muted" role="status" data-testid="layout-problem">{list.layout_problem}</p>}
      {editing && (
        <div className="space-y-2 rounded-lg border border-border/80 bg-surface/50 p-4" data-testid="hidden-charts">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="flex-1 text-[14.5px] font-semibold">Add a chart ({hiddenItems.length})</h2>
            {hiddenItems.length > 0 && (
              <input
                type="search"
                value={find}
                onChange={(e) => setFind(e.target.value)}
                placeholder="Search the charts you took off"
                aria-label="Search the charts you took off"
                className="h-8 w-72 max-w-full rounded-md border border-control bg-bg px-3 text-[13px]"
              />
            )}
          </div>
          {hiddenItems.length === 0 ? (
            <p className="text-[13px] text-muted">None. Remove a chart from your dashboard and it waits here to be added back.</p>
          ) : shownHidden.length === 0 ? (
            <p className="text-[13px] text-muted">No chart matches that.</p>
          ) : (
            <ul className="divide-y divide-border border-y border-border">
              {shownHidden.map((h) => (
                <li key={h.id} className="flex items-center gap-3 py-2 text-[13.5px]">
                  <span className="flex-1">{h.title}<span className="pl-2 text-[12.5px] text-muted">{h.group}</span></span>
                  <button type="button" className={quiet} aria-label={`Add ${h.title} back`} onClick={() => onAddBack(h)}>
                    Add
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

export type InsightsTab = "dashboard" | "ontology" | "monitors";

interface ActivePointerDrag {
  id: string;
  title: string;
  x: number;
  y: number;
  targetId: string | null;
}

export function InsightsDashboard({
  list,
  onChanged,
  feed,
  streaming,
  onArrange,
  editing: controlledEditing,
  onEditingChange,
}: {
  list: InsightList;
  onChanged: () => void;
  feed?: Record<string, TileEntry>;
  streaming?: boolean;
  onArrange?: (order: string[], hidden: string[]) => void;
  editing?: boolean;
  onEditingChange?: (next: boolean) => void;
}) {
  const [localEditing, setLocalEditing] = useState(false);
  const editing = controlledEditing ?? localEditing;
  const toggleEditing = () => {
    const next = !editing;
    if (onEditingChange) onEditingChange(next);
    else setLocalEditing(next);
  };

  const [activeTab, setActiveTab] = useState<InsightsTab>("dashboard");
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [bento, setBento] = useState<BentoState>(() => loadBentoState());
  const updateBento = useCallback((updater: (prev: BentoState) => BentoState) => {
    setBento((prev) => {
      const next = updater(prev);
      saveBentoState(next);
      return next;
    });
  }, []);

  // Pointer-based drag-to-reorder state (smooth floating chip + live FLIP reordering)
  const [pointerDrag, setPointerDrag] = useState<ActivePointerDrag | null>(null);
  const [liveDragOrder, setLiveDragOrder] = useState<string[] | null>(null);
  const lastSwapAtRef = useRef<number>(0);

  const localHiddenSet = useMemo(() => new Set(bento.hidden), [bento.hidden]);
  const activeOrder = liveDragOrder ?? bento.order;

  const visibleItems = useMemo(() => {
    const remaining = list.items.filter((it) => !localHiddenSet.has(it.id));
    return applyBentoOrder(remaining, activeOrder);
  }, [list.items, localHiddenSet, activeOrder]);

  // Curated executive canvas mode when the library spans multiple domains with >8 tiles
  const supportsCuratedCanvas = useMemo(() => {
    if (visibleItems.length <= 8) return false;
    const distinctGroups = new Set(visibleItems.map((it) => groupKey(it)));
    return distinctGroups.size > 1;
  }, [visibleItems]);

  const effectiveCanvasIds = useMemo(() => {
    if (bento.canvasIds !== undefined) {
      const validIds = new Set(visibleItems.map((it) => it.id));
      return bento.canvasIds.filter((id) => validIds.has(id));
    }
    return pickDefaultCanvasIds(visibleItems, DEFAULT_CANVAS_TILES);
  }, [bento.canvasIds, visibleItems]);

  const canvasIdSet = useMemo(() => new Set(effectiveCanvasIds), [effectiveCanvasIds]);
  const canvasItems = useMemo(() => {
    const byId = new Map(visibleItems.map((it) => [it.id, it]));
    const picked = effectiveCanvasIds.map((id) => byId.get(id)).filter((x): x is InsightInfo => Boolean(x));
    return applyBentoOrder(picked, activeOrder);
  }, [visibleItems, effectiveCanvasIds, activeOrder]);

  const combinedHiddenItems = useMemo(() => {
    const byId = new Map<string, InsightInfo>();
    for (const h of list.hidden ?? []) byId.set(h.id, h);
    for (const it of list.items) {
      if (localHiddenSet.has(it.id) && !byId.has(it.id)) byId.set(it.id, it);
    }
    return [...byId.values()];
  }, [list.hidden, list.items, localHiddenSet]);

  const shownIds = useMemo(() => visibleItems.map((i) => i.id), [visibleItems]);
  const serverHiddenIds = useMemo(() => (list.hidden ?? []).map((h) => h.id), [list.hidden]);

  const handleResize = useCallback(
    (id: string, size: BentoSize) => {
      updateBento((prev) => ({
        ...prev,
        sizes: { ...prev.sizes, [id]: size },
      }));
    },
    [updateBento],
  );

  const handleHeightResize = useCallback(
    (id: string, height: BentoHeight) => {
      updateBento((prev) => ({
        ...prev,
        heights: { ...(prev.heights ?? {}), [id]: height },
      }));
    },
    [updateBento],
  );

  const handleToggleCanvasItem = useCallback(
    (item: InsightInfo) => {
      updateBento((prev) => {
        const current = prev.canvasIds && prev.canvasIds.length > 0
          ? prev.canvasIds
          : pickDefaultCanvasIds(visibleItems, DEFAULT_CANVAS_TILES);
        const exists = current.includes(item.id);
        const nextCanvas = exists
          ? current.filter((x) => x !== item.id)
          : [...current, item.id];
        return { ...prev, canvasIds: nextCanvas };
      });
    },
    [updateBento, visibleItems],
  );

  const handleRemoveFromCanvas = useCallback(
    (id: string) => {
      updateBento((prev) => {
        const current = prev.canvasIds && prev.canvasIds.length > 0
          ? prev.canvasIds
          : pickDefaultCanvasIds(visibleItems, DEFAULT_CANVAS_TILES);
        return { ...prev, canvasIds: current.filter((x) => x !== id) };
      });
    },
    [updateBento, visibleItems],
  );

  const handleResetCurated = useCallback(() => {
    updateBento((prev) => {
      const next = { ...prev };
      delete next.canvasIds;
      return next;
    });
  }, [updateBento]);

  const handleHideTile = useCallback(
    (id: string) => {
      if (onArrange) {
        onArrange(
          list.items.map((i) => i.id).filter((x) => x !== id),
          [...serverHiddenIds, id],
        );
      } else {
        updateBento((prev) => ({
          ...prev,
          hidden: prev.hidden.includes(id) ? prev.hidden : [...prev.hidden, id],
        }));
      }
    },
    [list.items, onArrange, serverHiddenIds, updateBento],
  );

  const handleAddBack = useCallback(
    (item: InsightInfo) => {
      const wasLocalHidden = localHiddenSet.has(item.id);
      if (wasLocalHidden) {
        updateBento((prev) => ({
          ...prev,
          hidden: prev.hidden.filter((x) => x !== item.id),
        }));
      }
      if (onArrange && (list.hidden ?? []).some((h) => h.id === item.id)) {
        onArrange(
          [...list.items.map((i) => i.id), item.id],
          (list.hidden ?? []).filter((x) => x.id !== item.id).map((x) => x.id),
        );
      }
    },
    [list.hidden, list.items, localHiddenSet, onArrange, updateBento],
  );

  const handleReset = useCallback(() => {
    clearBentoState();
    setBento(EMPTY_BENTO_STATE);
    if (onArrange) onArrange([], []);
  }, [onArrange]);

  // Smooth FLIP layout animation when tiles reorder or resize
  const gridContainerRef = useRef<HTMLDivElement>(null);
  const flipRectsRef = useRef<Map<string, DOMRect>>(new Map());
  const flipKey = useMemo(
    () => `${JSON.stringify(activeOrder)}|${JSON.stringify(bento.sizes)}|${JSON.stringify(bento.heights ?? {})}|${effectiveCanvasIds.join(",")}`,
    [activeOrder, bento.sizes, bento.heights, effectiveCanvasIds],
  );
  useLayoutEffect(() => {
    const root = gridContainerRef.current;
    if (!root || typeof window === "undefined") return;
    const reduced = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const nodes = Array.from(root.querySelectorAll<HTMLElement>("[data-flip-id]"));
    const nextRects = new Map<string, DOMRect>();
    for (const el of nodes) {
      const id = el.dataset.flipId;
      if (!id) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) continue;
      nextRects.set(id, rect);
      const prev = flipRectsRef.current.get(id);
      if (prev && !reduced) {
        const dx = prev.left - rect.left;
        const dy = prev.top - rect.top;
        if (Math.abs(dx) > 1 || Math.abs(dy) > 1) {
          el.style.transition = "none";
          el.style.transform = `translate3d(${dx}px, ${dy}px, 0)`;
          requestAnimationFrame(() => {
            el.style.transition = "transform 260ms cubic-bezier(0.22, 1, 0.36, 1)";
            el.style.transform = "translate3d(0, 0, 0)";
            window.setTimeout(() => {
              el.style.transition = "";
              el.style.transform = "";
            }, 280);
          });
        }
      }
    }
    flipRectsRef.current = nextRects;
  }, [flipKey]);

  /** Start smooth pointer-based drag reordering for a tile within `poolIds`. */
  const startPointerDrag = useCallback(
    (item: InsightInfo, poolIds: readonly string[], e: React.PointerEvent<HTMLElement>, syncServerGroup = false) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();

      const startX = e.clientX;
      const startY = e.clientY;
      let started = false;
      let currentOrder = [...poolIds];
      const poolSet = new Set(poolIds);
      let latestTarget: string | null = null;

      const onMove = (ev: PointerEvent) => {
        const dist = Math.hypot(ev.clientX - startX, ev.clientY - startY);
        if (!started) {
          if (dist < 4) return;
          started = true;
          setLiveDragOrder(currentOrder);
        }

        // Hit-test sibling tiles inside the grid container
        const root = gridContainerRef.current;
        let hitId: string | null = null;
        if (root) {
          const candidates = root.querySelectorAll<HTMLElement>("[data-flip-id]");
          for (const el of candidates) {
            const cid = el.dataset.flipId;
            if (!cid || cid === item.id || !poolSet.has(cid)) continue;
            const r = el.getBoundingClientRect();
            if (ev.clientX >= r.left && ev.clientX <= r.right && ev.clientY >= r.top && ev.clientY <= r.bottom) {
              hitId = cid;
              break;
            }
          }
        }
        latestTarget = hitId;

        const now = Date.now();
        if (hitId && hitId !== item.id && now - lastSwapAtRef.current > 130) {
          const reordered = reorderIds(currentOrder, item.id, hitId);
          if (reordered.some((x, idx) => x !== currentOrder[idx])) {
            lastSwapAtRef.current = now;
            currentOrder = reordered;
            setLiveDragOrder(currentOrder);
          }
        }

        setPointerDrag({
          id: item.id,
          title: item.title,
          x: ev.clientX,
          y: ev.clientY,
          targetId: latestTarget,
        });
      };

      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        window.removeEventListener("pointercancel", onUp);
        setPointerDrag(null);
        setLiveDragOrder(null);
        if (!started) return;

        const changed = currentOrder.some((x, idx) => x !== poolIds[idx]);
        if (!changed) return;

        if (syncServerGroup && onArrange && latestTarget) {
          const nextGroup = placed(visibleItems, item.id, latestTarget);
          if (nextGroup.some((x, idx) => x !== shownIds[idx])) {
            onArrange(nextGroup, serverHiddenIds);
            return;
          }
        }
        updateBento((prev) => ({ ...prev, order: currentOrder }));
      };

      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onUp);
    },
    [onArrange, serverHiddenIds, shownIds, updateBento, visibleItems],
  );

  const quickDragOf = (it: InsightInfo, poolIds: readonly string[]): QuickDrag => ({
    startPointer: (e) => startPointerDrag(it, poolIds, e, false),
    stepUp: () => {
      const next = moveIdStep(poolIds, it.id, -1);
      if (next.some((x, i) => x !== poolIds[i])) updateBento((prev) => ({ ...prev, order: next }));
    },
    stepDown: () => {
      const next = moveIdStep(poolIds, it.id, 1);
      if (next.some((x, i) => x !== poolIds[i])) updateBento((prev) => ({ ...prev, order: next }));
    },
    dragging: pointerDrag?.id === it.id,
    dragOver: pointerDrag?.targetId === it.id && pointerDrag.id !== it.id,
  });

  const arrangeOf = (it: InsightInfo, poolIds: readonly string[]): Arrange | undefined => {
    if (!editing) return undefined;
    const up = moved(visibleItems, it.id, -1);
    const down = moved(visibleItems, it.id, 1);
    const canUp = up.some((x, i) => x !== shownIds[i]);
    const canDown = down.some((x, i) => x !== shownIds[i]);
    return {
      up: canUp
        ? () => {
            if (onArrange) onArrange(up, serverHiddenIds);
            else updateBento((prev) => ({ ...prev, order: up }));
          }
        : undefined,
      down: canDown
        ? () => {
            if (onArrange) onArrange(down, serverHiddenIds);
            else updateBento((prev) => ({ ...prev, order: down }));
          }
        : undefined,
      off: () => handleHideTile(it.id),
      startPointer: (e) => startPointerDrag(it, poolIds, e, true),
      dragging: pointerDrag?.id === it.id,
      dragOver: pointerDrag?.targetId === it.id && pointerDrag.id !== it.id,
    };
  };

  const allGroups = grouped(visibleItems);
  const measureGroups = allGroups.filter(([g]) => /^Measures:\s*/i.test(g));
  const totalMeasures = measureGroups.reduce((n, [, items]) => n + items.length, 0);
  const catalogMeasuresCount = Math.max((list.measures ?? []).length, totalMeasures);
  const categoryOf = (group: string) => group.replace(/^Measures:\s*/i, "");
  const PRIMARY_ORDER = [
    "Pinned by you",
    "Finance",
    "Sales",
    "Purchasing",
    "Operations",
    "Quality",
    "Customer Service",
    "Customers",
    "Decisions",
    "Team",
  ];
  const ALL_SECTION_ORDER = [
    "Pinned by you",
    "Finance",
    "Sales",
    "Purchasing",
    "Operations",
    "Quality",
    "Customer Service",
    "Customers",
    "Decisions",
    "Team",
  ];
  const sectionRank = (cat: string) => {
    const idx = ALL_SECTION_ORDER.indexOf(cat);
    return idx >= 0 ? idx : ALL_SECTION_ORDER.length;
  };
  const orderedGroups = useMemo(
    () =>
      [...allGroups].sort(([ga], [gb]) => {
        const ra = sectionRank(categoryOf(ga));
        const rb = sectionRank(categoryOf(gb));
        if (ra !== rb) return ra - rb;
        const ma = /^Measures:\s*/i.test(ga) ? 1 : 0;
        const mb = /^Measures:\s*/i.test(gb) ? 1 : 0;
        if (ma !== mb) return ma - mb;
        return 0;
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [allGroups],
  );
  const categories = useMemo(() => {
    const seen = new Map<string, { charts: number; measures: number }>();
    for (const [g, items] of orderedGroups) {
      const cat = categoryOf(g);
      const cur = seen.get(cat) ?? { charts: 0, measures: 0 };
      if (/^Measures:\s*/i.test(g)) cur.measures += items.length;
      else cur.charts += items.length;
      seen.set(cat, cur);
    }
    const keys = [...seen.keys()].sort((a, b) => {
      const ia = PRIMARY_ORDER.indexOf(a);
      const ib = PRIMARY_ORDER.indexOf(b);
      if (ia >= 0 && ib >= 0) return ia - ib;
      if (ia >= 0) return -1;
      if (ib >= 0) return 1;
      return a.localeCompare(b);
    });
    return keys.map((cat) => {
      const counts = seen.get(cat) ?? { charts: 0, measures: 0 };
      return { cat, charts: counts.charts, measures: counts.measures, total: counts.charts + counts.measures };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderedGroups]);
  const [activeCategory, setActiveCategory] = useState<string>(() => {
    if (visibleItems.length <= 8) return "All";
    const distinctGroups = new Set(visibleItems.map((it) => groupKey(it)));
    return distinctGroups.size > 1 ? "Canvas" : "All";
  });
  const [measuresOpen, setMeasuresOpen] = useState(false);
  const [measureQuery, setMeasureQuery] = useState("");
  const measuresRef = useRef<HTMLDivElement>(null);
  const activeMonitorsCount = useMemo(
    () => visibleItems.filter((i) => i.saved && i.schedule).length,
    [visibleItems],
  );
  useEffect(() => {
    if (!measuresOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMeasuresOpen(false);
    };
    const onDown = (e: PointerEvent) => {
      if (measuresRef.current && !measuresRef.current.contains(e.target as Node)) {
        setMeasuresOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onDown);
    };
  }, [measuresOpen]);
  const effectiveCategory = activeCategory;
  const visibleGroups = effectiveCategory === "All" || effectiveCategory === "Canvas"
    ? orderedGroups
    : orderedGroups.filter(([g]) => categoryOf(g) === effectiveCategory);
  const filteredMeasureGroups = useMemo(() => {
    const q = measureQuery.trim().toLowerCase();
    if (!q) return measureGroups;
    return measureGroups
      .map(([g, items]) => [g, items.filter((it) => `${it.title} ${g}`.toLowerCase().includes(q))] as [string, InsightInfo[]])
      .filter(([, items]) => items.length > 0);
  }, [measureGroups, measureQuery]);

  const jumpToTile = (id: string) => {
    setActiveTab("dashboard");
    const targetItem = visibleItems.find((it) => it.id === id);
    if (supportsCuratedCanvas && targetItem) {
      if (!canvasIdSet.has(id)) {
        handleToggleCanvasItem(targetItem);
      }
      setActiveCategory("Canvas");
    } else if (targetItem) {
      setActiveCategory(categoryOf(groupKey(targetItem)));
    }
    setTimeout(() => {
      document.getElementById(`insight-${id}`)?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    }, 30);
  };

  const bentoCustom = isBentoCustomized(bento);

  return (
    <div ref={gridContainerRef} className="mx-auto w-full max-w-[1680px] space-y-4 text-[14px] leading-6">
      {/* Clean Apple Liquid Glass 4-Tab Bar: My canvas | All views | Ontology | Monitors + Actions */}
      <div
        className="flex flex-wrap items-center justify-between gap-3"
        data-testid="insights-view-tabs"
      >
        <div
          role="tablist"
          aria-label="Dashboard sections"
          className="liquid-glass-pill inline-flex items-center gap-0.5 overflow-x-auto rounded-lg p-0.5"
        >
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "dashboard" && effectiveCategory === "Canvas"}
            onClick={() => {
              setActiveTab("dashboard");
              setActiveCategory("Canvas");
            }}
            data-testid="tab-dashboard"
            className={cn(
              "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md px-3 text-[12.5px] font-medium cursor-pointer",
              activeTab === "dashboard" && effectiveCategory === "Canvas"
                ? "liquid-glass-active"
                : "text-muted hover:text-fg",
            )}
          >
            <LayoutGrid className="size-3.5 shrink-0" strokeWidth={1.9} aria-hidden />
            My canvas
            <span className="num text-[11px] opacity-75">
              {canvasItems.length}
            </span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "dashboard" && effectiveCategory !== "Canvas"}
            onClick={() => {
              setActiveTab("dashboard");
              setActiveCategory("All");
            }}
            data-testid="tab-all-views"
            className={cn(
              "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md px-3 text-[12.5px] font-medium cursor-pointer",
              activeTab === "dashboard" && effectiveCategory !== "Canvas"
                ? "liquid-glass-active"
                : "text-muted hover:text-fg",
            )}
          >
            <Layers className="size-3.5 shrink-0" strokeWidth={1.9} aria-hidden />
            All views
            <span className="num text-[11px] opacity-75">{visibleItems.length}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "ontology"}
            onClick={() => setActiveTab("ontology")}
            data-testid="tab-ontology"
            className={cn(
              "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md px-3 text-[12.5px] font-medium cursor-pointer",
              activeTab === "ontology" ? "liquid-glass-active" : "text-muted hover:text-fg",
            )}
          >
            <Network className="size-3.5 shrink-0" strokeWidth={1.9} aria-hidden />
            Ontology
            <span className="num text-[11px] opacity-75">{catalogMeasuresCount}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "monitors"}
            onClick={() => setActiveTab("monitors")}
            data-testid="tab-monitors"
            className={cn(
              "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md px-3 text-[12.5px] font-medium cursor-pointer",
              activeTab === "monitors" ? "liquid-glass-active" : "text-muted hover:text-fg",
            )}
          >
            <Bell className="size-3.5 shrink-0" strokeWidth={1.9} aria-hidden />
            Monitors
            {activeMonitorsCount > 0 && (
              <span className="num text-[11px] opacity-75">{activeMonitorsCount}</span>
            )}
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {activeTab === "dashboard" && list.scope === "self" && (
            <p className="text-[12px] text-muted">The team views show your own row only.</p>
          )}
          {activeTab === "dashboard" && visibleItems.length > 0 && (
            <button
              type="button"
              onClick={() => setLibraryOpen(true)}
              data-testid="open-widget-library"
              className="inline-flex h-7 items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 text-[12px] font-medium text-fg transition-colors hover:border-border-strong hover:bg-hover cursor-pointer"
            >
              <Plus className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
              Add widget
            </button>
          )}
          {activeTab === "dashboard" && (
            <Link
              prefetch={false}
              href="/ask"
              data-testid="new-chart-link"
              className="inline-flex h-7 items-center gap-1.5 rounded-md bg-fg px-2.5 text-[12px] font-medium text-bg transition-opacity hover:opacity-90"
            >
              <Sparkles className="size-3 shrink-0" strokeWidth={2} aria-hidden />
              New chart
            </Link>
          )}
        </div>
      </div>

      {activeTab === "ontology" ? (
        <OntologyAndMeasuresPanel
          measures={list.measures ?? []}
          items={visibleItems}
          canvasIdSet={canvasIdSet}
          onToggleCanvas={handleToggleCanvasItem}
          onJumpToTile={jumpToTile}
        />
      ) : activeTab === "monitors" ? (
        <div data-testid="monitors-quality-panel">
          <BenchmarksPanel
            list={list}
            monitorLine={monitorLine}
            onJumpToTile={jumpToTile}
          />
        </div>
      ) : (
        <>
          {(list.saving || editing || bentoCustom || list.layout_problem) && (
            <Customise
              list={list}
              hiddenItems={combinedHiddenItems}
              editing={editing}
              bentoCustom={bentoCustom}
              onToggle={toggleEditing}
              onAddBack={handleAddBack}
              onReset={handleReset}
              inHeader={onEditingChange !== undefined}
            />
          )}

          {list.saving && effectiveCategory !== "Canvas" && <Monitors items={visibleItems} />}

          {/* Domain Filter Strip + My Canvas Switcher */}
          {(allGroups.length > 2 || supportsCuratedCanvas) && (
            <nav
              aria-label="Groups"
              className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-2 border-b border-border bg-canvas/95 py-2"
              data-testid="insight-groups"
            >
              <div role="tablist" aria-label="Categories" className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto pb-0.5">
                {supportsCuratedCanvas ? (
                  <button
                    type="button"
                    role="tab"
                    aria-selected={effectiveCategory === "Canvas"}
                    onClick={() => setActiveCategory("Canvas")}
                    data-testid="category-canvas"
                    className={cn(
                      "inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md px-3 text-[13px] font-medium whitespace-nowrap transition-colors cursor-pointer",
                      effectiveCategory === "Canvas" ? "bg-surface-2 text-fg" : "text-muted hover:bg-hover hover:text-fg",
                    )}
                  >
                    My canvas
                    <span className="num text-[11px] text-faint">{canvasItems.length}</span>
                  </button>
                ) : (
                  <button
                    type="button"
                    role="tab"
                    aria-selected={effectiveCategory === "All"}
                    onClick={() => setActiveCategory("All")}
                    className={cn(
                      "inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md px-3 text-[13px] font-medium whitespace-nowrap transition-colors cursor-pointer",
                      effectiveCategory === "All" ? "bg-surface-2 text-fg" : "text-muted hover:bg-hover hover:text-fg",
                    )}
                  >
                    All
                    <span className="num text-[11px] text-faint">{visibleItems.length}</span>
                  </button>
                )}
                {categories.map(({ cat, charts, measures, total }) => (
                  <button
                    key={cat}
                    type="button"
                    role="tab"
                    aria-selected={effectiveCategory === cat}
                    title={charts > 0 && measures > 0 ? `${cat}: ${charts} ${charts === 1 ? "chart" : "charts"}, ${measures} ${measures === 1 ? "measure" : "measures"}` : undefined}
                    onClick={() => setActiveCategory(cat)}
                    className={cn(
                      "inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md px-3 text-[13px] font-medium whitespace-nowrap transition-[background-color,color,transform] duration-150 ease-[cubic-bezier(0.2,0,0,1)] active:scale-[0.98] cursor-pointer",
                      effectiveCategory === cat ? "bg-surface-2 text-fg" : "text-muted hover:bg-hover hover:text-fg",
                    )}
                  >
                    {cat}
                    <span className="num text-[11px] text-faint">{total}</span>
                  </button>
                ))}
              </div>
              {totalMeasures > 0 && (
                <div ref={measuresRef} className="relative shrink-0">
                  <button
                    type="button"
                    aria-expanded={measuresOpen}
                    onClick={() => setMeasuresOpen((v) => !v)}
                    data-testid="browse-measures"
                    className={cn(
                      "inline-flex h-7 items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 text-[12.5px] font-medium text-fg transition-colors hover:border-border-strong hover:bg-hover active:scale-[0.98] cursor-pointer",
                      measuresOpen && "border-accent bg-accent-soft",
                    )}
                  >
                    <span>Browse measures ({totalMeasures})</span>
                    <ChevronDown className={cn("size-3.5 text-muted transition-transform", measuresOpen && "rotate-180")} strokeWidth={2} aria-hidden />
                  </button>
                  {measuresOpen && (
                    <div className="liquid-glass-dock absolute right-0 z-30 mt-1.5 w-[340px] max-w-[calc(100vw-2rem)] rounded-lg p-3 shadow-pop" data-testid="measures-drawer">
                      <label className="flex h-8 items-center gap-2 rounded-lg border border-border bg-bg/90 px-2.5 text-[12.5px] focus-within:border-accent">
                        <Search className="size-3.5 shrink-0 text-faint" strokeWidth={2} aria-hidden />
                        <input
                          type="search"
                          value={measureQuery}
                          onChange={(e) => setMeasureQuery(e.target.value)}
                          placeholder="Filter measures"
                          aria-label="Filter measures"
                          className="min-w-0 flex-1 bg-transparent text-fg outline-none focus-visible:outline-none placeholder:text-faint"
                        />
                      </label>
                      <div className="mt-2.5 max-h-80 space-y-3 overflow-y-auto pr-1">
                        {filteredMeasureGroups.length === 0 ? (
                          <p className="px-1 py-2 text-[12px] text-muted">No measure matches that.</p>
                        ) : (
                          filteredMeasureGroups.map(([group, items]) => {
                            const cat = categoryOf(group);
                            return (
                              <div key={group} className="space-y-1">
                                <button
                                  type="button"
                                  onClick={() => {
                                    setActiveCategory(cat);
                                    setMeasuresOpen(false);
                                  }}
                                  className="flex w-full items-center justify-between px-1.5 text-[11px] font-semibold uppercase tracking-[0.04em] text-faint hover:text-fg cursor-pointer"
                                >
                                  <span>{cat}</span>
                                  <span className="num">{items.length}</span>
                                </button>
                                <ul className="space-y-0.5">
                                  {items.map((it) => (
                                    <li key={it.id}>
                                      <button
                                        type="button"
                                        onClick={() => {
                                          setMeasuresOpen(false);
                                          jumpToTile(it.id);
                                        }}
                                        className="block w-full truncate rounded-md px-2 py-1 text-left text-[12.5px] text-fg hover:bg-hover cursor-pointer"
                                      >
                                        {it.title}
                                      </button>
                                    </li>
                                  ))}
                                </ul>
                              </div>
                            );
                          })
                        )}
                      </div>
                    </div>
                  )}
                </div>
              )}
            </nav>
          )}

          {effectiveCategory !== "All" && effectiveCategory !== "Canvas" && visibleGroups.length > 1 && (() => {
            const activeMeta = categories.find((c) => c.cat === effectiveCategory);
            const measureGroup = visibleGroups.find(([g]) => /^Measures:\s*/i.test(g));
            if (!activeMeta || activeMeta.charts === 0 || activeMeta.measures === 0) return null;
            const measureAnchor = measureGroup ? `#insight-group-${measureGroup[0].toLowerCase().replace(/[^a-z0-9]+/g, "-")}` : null;
            return (
              <div className="flex flex-wrap items-center justify-between gap-2 text-[12.5px] text-muted" data-testid="category-breakdown">
                <span className="inline-flex flex-wrap items-center gap-2.5 tabular-nums">
                  <span className="font-medium text-fg">{effectiveCategory}</span>
                  <span className="h-3 w-px bg-border" aria-hidden />
                  <span>{activeMeta.charts} {activeMeta.charts === 1 ? "chart" : "charts"}</span>
                  <span className="h-3 w-px bg-border" aria-hidden />
                  <span>{activeMeta.measures} {activeMeta.measures === 1 ? "measure" : "measures"}</span>
                </span>
                {measureAnchor && (
                  <a href={measureAnchor} className="text-[12px] font-medium text-accent hover:underline">
                    Jump to measures ({activeMeta.measures})
                  </a>
                )}
              </div>
            );
          })()}

          {/* Unified Curated Executive Canvas OR 12-Column Responsive Bento Grid per Domain */}
          {effectiveCategory === "Canvas" ? (
            <section className="space-y-3" aria-label="My canvas" data-testid="curated-canvas">
              <div className="flex flex-wrap items-center justify-between gap-2 text-[12px] text-muted">
                <span className="inline-flex flex-wrap items-center gap-2.5">
                  <span className="tabular-nums">Showing {canvasItems.length} of {visibleItems.length} widgets</span>
                  <span className="h-3 w-px bg-border" aria-hidden />
                  <span>Drag any handle to reorder or drag bottom-right corners to resize.</span>
                </span>
                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    onClick={() => setLibraryOpen(true)}
                    className="font-medium text-accent hover:underline cursor-pointer"
                  >
                    + Add from library ({visibleItems.length - canvasItems.length} available)
                  </button>
                  <button
                    type="button"
                    onClick={() => setActiveTab("ontology")}
                    className="text-muted hover:text-fg hover:underline cursor-pointer"
                  >
                    Ontology map
                  </button>
                </div>
              </div>
              {canvasItems.length === 0 ? (
                <div className="rounded-lg border border-border/80 bg-surface/50 p-8 text-center space-y-2">
                  <p className="text-[13.5px] font-medium text-fg">Your canvas is empty</p>
                  <p className="text-[12.5px] text-muted">Pick charts or governed measures from the widget library to build your view.</p>
                  <button
                    type="button"
                    onClick={() => setLibraryOpen(true)}
                    className="mt-1 inline-flex h-8 items-center gap-1.5 rounded-md bg-fg px-3 text-[12.5px] font-medium text-bg hover:opacity-90 cursor-pointer"
                  >
                    <Plus className="size-3.5" aria-hidden />
                    Add widgets
                  </button>
                </div>
              ) : (
                <div className="grid grid-cols-1 gap-4 lg:grid-cols-12 lg:grid-flow-dense" data-testid="bento-grid">
                  {canvasItems.map((it) => {
                    const pool = canvasItems.map((x) => x.id);
                    return (
                      <InsightTile
                        key={it.id}
                        item={it}
                        onRemoved={onChanged}
                        entry={feed?.[it.id]}
                        streaming={feed ? !!streaming : undefined}
                        arrange={arrangeOf(it, pool)}
                        quickDrag={quickDragOf(it, pool)}
                        sizeOverride={bento.sizes[it.id]}
                        heightOverride={bento.heights?.[it.id]}
                        onResize={(sz) => handleResize(it.id, sz)}
                        onHeightResize={(h) => handleHeightResize(it.id, h)}
                        onRemoveFromCanvas={() => handleRemoveFromCanvas(it.id)}
                        fillSolo={canvasItems.length === 1}
                      />
                    );
                  })}
                </div>
              )}
            </section>
          ) : effectiveCategory === "All" && visibleGroups.length >= 2 && visibleGroups.some(([, items]) => items.length === 1) ? (
            <section className="space-y-3" aria-label="All dashboard widgets" data-testid="unified-bento-canvas">
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-12 lg:grid-flow-dense" data-testid="bento-grid">
                {visibleItems.map((it) => {
                  const pool = visibleItems.map((x) => x.id);
                  return (
                    <InsightTile
                      key={it.id}
                      item={it}
                      onRemoved={onChanged}
                      entry={feed?.[it.id]}
                      streaming={feed ? !!streaming : undefined}
                      arrange={arrangeOf(it, pool)}
                      quickDrag={quickDragOf(it, pool)}
                      sizeOverride={bento.sizes[it.id]}
                      heightOverride={bento.heights?.[it.id]}
                      onResize={(sz) => handleResize(it.id, sz)}
                      onHeightResize={(h) => handleHeightResize(it.id, h)}
                      fillSolo={visibleItems.length === 1}
                    />
                  );
                })}
              </div>
            </section>
          ) : (
            visibleGroups.map(([group, items]) => {
              const isMeasureGroup = /^Measures:\s*/i.test(group);
              const cat = categoryOf(group);
              const catMeta = categories.find((c) => c.cat === cat);
              const hasBoth = Boolean(catMeta && catMeta.charts > 0 && catMeta.measures > 0);
              const unitLabel = isMeasureGroup ? (items.length === 1 ? "measure" : "measures") : (items.length === 1 ? "chart" : "charts");
              const groupPool = items.map((x) => x.id);
              return (
                <section
                  key={group}
                  id={`insight-group-${group.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`}
                  className="scroll-mt-16 space-y-3"
                  aria-label={group}
                >
                  <h2 className="flex items-baseline gap-2 border-b border-border/80 pb-2 text-[14.5px] font-semibold tracking-[-0.02em] text-fg">
                    <span>{group}</span>
                    <span className="num text-[12px] font-normal text-muted">
                      {hasBoth ? `${items.length} ${unitLabel}` : items.length}
                    </span>
                  </h2>
                  <div className="grid grid-cols-1 gap-4 lg:grid-cols-12 lg:grid-flow-dense" data-testid="bento-grid">
                    {items.map((it) => (
                      <InsightTile
                        key={it.id}
                        item={it}
                        onRemoved={onChanged}
                        entry={feed?.[it.id]}
                        streaming={feed ? !!streaming : undefined}
                        arrange={arrangeOf(it, groupPool)}
                        quickDrag={quickDragOf(it, groupPool)}
                        sizeOverride={bento.sizes[it.id]}
                        heightOverride={bento.heights?.[it.id]}
                        onResize={(sz) => handleResize(it.id, sz)}
                        onHeightResize={(h) => handleHeightResize(it.id, h)}
                        fillSolo={items.length === 1}
                      />
                    ))}
                  </div>
                </section>
              );
            })
          )}

          {list.saving && !visibleItems.some((i) => i.saved) && (
            <p className="text-[13px] text-muted">Ask for a chart and pin it from the answer: it shows here and stays live. Set it to check by itself and it is listed under Monitors.</p>
          )}
        </>
      )}

      {/* Floating pointer drag indicator (zero browser screenshot ghost) */}
      {pointerDrag && (
        <div
          data-testid="drag-floating-chip"
          style={{ left: pointerDrag.x + 14, top: pointerDrag.y + 14 }}
          className="liquid-glass-dock pointer-events-none fixed z-50 flex items-center gap-2 rounded-lg px-3 py-1.5 text-[12px] font-medium text-fg"
        >
          <GripVertical className="size-3.5 text-accent-text" strokeWidth={2} aria-hidden />
          <span className="max-w-[240px] truncate">{pointerDrag.title}</span>
        </div>
      )}

      <WidgetLibraryDrawer
        open={libraryOpen}
        onClose={() => setLibraryOpen(false)}
        allItems={visibleItems}
        canvasIdSet={canvasIdSet}
        onToggleCanvas={handleToggleCanvasItem}
        onResetCurated={handleResetCurated}
      />
    </div>
  );
}
