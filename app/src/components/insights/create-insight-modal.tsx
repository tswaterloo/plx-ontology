"use client";

import {
  AlignLeft,
  ArrowUpRight,
  BarChart3,
  Check,
  Code2,
  Hash,
  LineChart,
  Loader2,
  Plus,
  Search,
  Sparkles,
  Table2,
  X,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import type { BentoSize } from "@/components/insights/bento-layout";
import { saveInsight } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import type { InsightInfo, InsightList, InsightSave } from "@/lib/types";

export type CreateInsightChartKind = "bar" | "horizontal_bar" | "line" | "kpi" | "table";
export type CreateInsightWidthPreset = "m" | "l" | "full";

export interface CreateInsightLayout {
  size: BentoSize;
  chartKind?: CreateInsightChartKind;
}

export interface CreateInsightModalProps {
  open: boolean;
  onClose: () => void;
  items: InsightInfo[];
  measures?: NonNullable<InsightList["measures"]>;
  canvasIdSet?: Set<string>;
  onAddTile: (tileId: string, layout?: CreateInsightLayout) => void;
  onCreated?: (created: InsightInfo, layout?: CreateInsightLayout) => void;
  initialMode?: "measure" | "custom";
  initialPrompt?: string;
}

const DOMAIN_ORDER = [
  "Finance",
  "Sales",
  "Purchasing",
  "Operations",
  "Quality",
  "Customers",
  "Customer Service",
  "Team",
];

const CHART_KIND_OPTIONS: readonly {
  id: CreateInsightChartKind;
  label: string;
  icon: typeof BarChart3;
}[] = [
  { id: "bar", label: "Bar", icon: BarChart3 },
  { id: "horizontal_bar", label: "Horizontal bar", icon: AlignLeft },
  { id: "line", label: "Line", icon: LineChart },
  { id: "kpi", label: "KPI", icon: Hash },
  { id: "table", label: "Table", icon: Table2 },
];

const WIDTH_PRESETS: readonly {
  id: CreateInsightWidthPreset;
  label: string;
  hint: string;
}[] = [
  { id: "m", label: "Half", hint: "6 cols" },
  { id: "l", label: "Wide", hint: "8 cols" },
  { id: "full", label: "Full", hint: "12 cols" },
];

const CUSTOM_EXAMPLES = [
  "Monthly revenue by top 10 customers",
  "Open PO value by supplier",
  "QC hold volume by product",
  "Overdue AR balance by aging bucket",
] as const;

type MeasureEntry = NonNullable<InsightList["measures"]>[number];

interface SelectableMeasure {
  key: string;
  title: string;
  description: string;
  domain: string;
  source?: string;
  dimensions: string[];
  tile?: InsightInfo;
  measure?: MeasureEntry;
  defaultChartKind: CreateInsightChartKind;
}

function domainOfTile(item: InsightInfo): string {
  const fromMeasure = item.measure?.owner?.trim();
  if (fromMeasure) return fromMeasure;
  const cleaned = (item.group || "").replace(/^Measures:\s*/i, "").trim();
  return cleaned || "Operations";
}

function chartKindOfTile(item?: InsightInfo): CreateInsightChartKind {
  const spec = item?.chart as { kind?: string; horizontal?: boolean } | null | undefined;
  if (!spec?.kind) return "table";
  if (spec.kind === "bar" && spec.horizontal) return "horizontal_bar";
  if (spec.kind === "line" || spec.kind === "area" || spec.kind === "combo") return "line";
  if (spec.kind === "kpi") return "kpi";
  if (spec.kind === "bar" || spec.kind === "stacked_bar") return "bar";
  return "table";
}

function buildChartSpecForSave(
  chartKind: CreateInsightChartKind,
  fallbackX = "label",
  fallbackY = "value",
): InsightSave["chart"] {
  if (chartKind === "table") return null;
  if (chartKind === "kpi") return { kind: "kpi", y: [fallbackY] };
  if (chartKind === "line") return { kind: "line", x: fallbackX, y: [fallbackY] };
  if (chartKind === "horizontal_bar") {
    return { kind: "bar", x: fallbackX, y: [fallbackY], horizontal: true };
  }
  return { kind: "bar", x: fallbackX, y: [fallbackY] };
}

function buildSelectableCatalog(
  items: readonly InsightInfo[],
  measures: readonly MeasureEntry[],
): SelectableMeasure[] {
  const out: SelectableMeasure[] = [];
  const coveredTileIds = new Set<string>();
  const coveredMeasureIds = new Set<string>();

  for (const m of measures) {
    coveredMeasureIds.add(m.id);
    const tile = items.find((t) => t.measure?.id === m.id || t.id === `measure_${m.id}`);
    if (tile) coveredTileIds.add(tile.id);
    const domain = m.owner || (tile ? domainOfTile(tile) : "Operations");
    out.push({
      key: tile?.id ?? `measure_${m.id}`,
      title: tile?.title ?? m.name,
      description: m.definition || tile?.about || "",
      domain,
      source: m.source ? (m.source.includes(".") ? m.source : `biz.${m.source}`) : undefined,
      dimensions: m.dimensions ?? [],
      tile,
      measure: m,
      defaultChartKind: chartKindOfTile(tile),
    });
  }

  for (const it of items) {
    if (coveredTileIds.has(it.id)) continue;
    if (it.measure && coveredMeasureIds.has(it.measure.id)) continue;
    coveredTileIds.add(it.id);
    out.push({
      key: it.id,
      title: it.title,
      description: it.about || it.measure?.definition || "",
      domain: domainOfTile(it),
      dimensions: it.chart?.x ? [it.chart.x] : [],
      tile: it,
      defaultChartKind: chartKindOfTile(it),
    });
  }

  return out;
}

/**
 * Real in-page Create Insight modal on Insights:
 * 1. Pick a governed measure / live catalog insight, choose chart type & width preset, and add to My canvas immediately.
 * 2. Describe a custom chart in plain English (instant live tile when backed by SQL or matched measure; transparent Ask handoff for novel custom SQL).
 */
export function CreateInsightModal({
  open,
  onClose,
  items,
  measures = [],
  canvasIdSet,
  onAddTile,
  onCreated,
  initialMode = "measure",
  initialPrompt = "",
}: CreateInsightModalProps) {
  const [mode, setMode] = useState<"measure" | "custom">(initialMode);
  const [search, setSearch] = useState("");
  const [domainFilter, setDomainFilter] = useState<string>("All");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [chartKind, setChartKind] = useState<CreateInsightChartKind>("bar");
  const [widthPreset, setWidthPreset] = useState<CreateInsightWidthPreset>("m");

  // Custom plain-English state
  const [customPrompt, setCustomPrompt] = useState(initialPrompt);
  const [customTitle, setCustomTitle] = useState("");
  const [showSql, setShowSql] = useState(false);
  const [customSql, setCustomSql] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const catalog = useMemo(() => buildSelectableCatalog(items, measures), [items, measures]);

  useEffect(() => {
    if (!open) return;
    setMode(initialMode);
    if (initialPrompt) setCustomPrompt(initialPrompt);
    setError(null);
    setSaving(false);
  }, [open, initialMode, initialPrompt]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const domains = useMemo(() => {
    const counts = new Map<string, number>();
    for (const entry of catalog) {
      counts.set(entry.domain, (counts.get(entry.domain) ?? 0) + 1);
    }
    const ordered = [...counts.keys()].sort((a, b) => {
      const ia = DOMAIN_ORDER.indexOf(a);
      const ib = DOMAIN_ORDER.indexOf(b);
      if (ia >= 0 && ib >= 0) return ia - ib;
      if (ia >= 0) return -1;
      if (ib >= 0) return 1;
      return a.localeCompare(b);
    });
    return [
      { name: "All", count: catalog.length },
      ...ordered.map((name) => ({ name, count: counts.get(name) ?? 0 })),
    ];
  }, [catalog]);

  const filteredCatalog = useMemo(() => {
    const q = search.trim().toLowerCase();
    return catalog.filter((entry) => {
      if (domainFilter !== "All" && entry.domain !== domainFilter) return false;
      if (!q) return true;
      return `${entry.title} ${entry.description} ${entry.domain} ${entry.source ?? ""} ${entry.dimensions.join(" ")}`
        .toLowerCase()
        .includes(q);
    });
  }, [catalog, domainFilter, search]);

  const groupedCatalog = useMemo(() => {
    const map = new Map<string, SelectableMeasure[]>();
    for (const entry of filteredCatalog) {
      const bucket = map.get(entry.domain);
      if (bucket) bucket.push(entry);
      else map.set(entry.domain, [entry]);
    }
    return [...map.entries()];
  }, [filteredCatalog]);

  const selectedEntry = useMemo(() => {
    if (selectedKey) {
      const hit = filteredCatalog.find((e) => e.key === selectedKey) ?? catalog.find((e) => e.key === selectedKey);
      if (hit) return hit;
    }
    return filteredCatalog[0] ?? null;
  }, [selectedKey, filteredCatalog, catalog]);

  // Suggest an instant live catalog match when user types a plain-English custom query
  const instantMatch = useMemo(() => {
    const q = customPrompt.trim().toLowerCase();
    if (q.length < 4) return null;
    const tokens = q.split(/\s+/).filter((w) => w.length >= 3);
    if (tokens.length === 0) return null;
    let best: { entry: SelectableMeasure; score: number } | null = null;
    for (const entry of catalog) {
      if (!entry.tile) continue;
      const hay = `${entry.title} ${entry.description} ${entry.domain}`.toLowerCase();
      let score = 0;
      for (const t of tokens) {
        if (hay.includes(t)) score += 1;
      }
      if (score >= Math.min(2, tokens.length) && (!best || score > best.score)) {
        best = { entry, score };
      }
    }
    return best?.entry ?? null;
  }, [customPrompt, catalog]);

  if (!open) return null;

  const handleSelectMeasure = (entry: SelectableMeasure) => {
    setSelectedKey(entry.key);
    setChartKind(entry.defaultChartKind);
    setError(null);
  };

  const handleAddSelectedMeasure = async () => {
    if (!selectedEntry) return;
    setError(null);
    const layout: CreateInsightLayout = { size: widthPreset, chartKind };

    if (selectedEntry.tile) {
      onAddTile(selectedEntry.tile.id, layout);
      onClose();
      return;
    }

    // Measure exists without a tile yet: create a live saved insight from its governed source SQL.
    if (selectedEntry.measure) {
      const m = selectedEntry.measure;
      const src = m.source ? (m.source.includes(".") ? m.source : `biz.${m.source}`) : "biz.workspace_views";
      const dim = m.dimensions?.[0] || "month";
      const sql = `SELECT ${dim}, ${m.id} FROM ${src} GROUP BY ${dim}`;
      setSaving(true);
      try {
        const created = await saveInsight({
          title: selectedEntry.title,
          question: m.ask || `Show ${m.name} by ${dim}`,
          sql,
          chart: buildChartSpecForSave(chartKind, dim, m.id),
          schedule: "",
          notify: "never",
        });
        onCreated?.(created, layout);
        onAddTile(created.id, layout);
        onClose();
      } catch (e) {
        setError(plainError(e));
      } finally {
        setSaving(false);
      }
    }
  };

  const handleCreateCustomSqlTile = async () => {
    const promptText = customPrompt.trim();
    const sqlText = customSql.trim();
    if (!promptText && !sqlText) return;
    if (!sqlText) return;

    setSaving(true);
    setError(null);
    const title = (customTitle.trim() || promptText || "Custom Insight").slice(0, 140);
    const layout: CreateInsightLayout = { size: widthPreset, chartKind };
    try {
      const created = await saveInsight({
        title,
        question: promptText || title,
        sql: sqlText,
        chart: buildChartSpecForSave(chartKind),
        schedule: "",
        notify: "never",
      });
      onCreated?.(created, layout);
      onAddTile(created.id, layout);
      onClose();
    } catch (e) {
      setError(plainError(e));
    } finally {
      setSaving(false);
    }
  };

  const trimmedPrompt = customPrompt.trim();
  const isInstantLiveCustom = Boolean(customSql.trim());
  const askChartLabel = CHART_KIND_OPTIONS.find((c) => c.id === chartKind)?.label.toLowerCase() ?? "bar";
  const askHandoffHref = `/ask?q=${encodeURIComponent(
    trimmedPrompt
      ? `Create a ${askChartLabel} chart for: ${trimmedPrompt}`
      : "Create a chart I can save to My canvas",
  )}&save_chart=1`;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-xs"
      role="dialog"
      aria-modal="true"
      aria-label="Create insight"
      data-testid="create-insight-modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="flex max-h-[88vh] w-full max-w-[680px] flex-col overflow-hidden rounded-2xl border border-border-strong/80 bg-raised shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_24px_60px_-16px_rgba(0,0,0,0.65)] dark:bg-[#141519]">
        {/* Header */}
        <div className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h2 className="font-display text-[16px] font-semibold tracking-[-0.02em] text-fg">
              Create insight
            </h2>
            <p className="mt-0.5 text-[12.5px] text-muted">
              Add a governed measure directly to My canvas or build a custom chart from plain English.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close create insight modal"
            className="inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg cursor-pointer"
          >
            <X className="size-4" strokeWidth={2} aria-hidden />
          </button>
        </div>

        {/* Liquid Glass Mode Switcher */}
        <div className="border-b border-border/80 px-5 py-3">
          <div
            role="tablist"
            aria-label="Create insight mode"
            className="liquid-glass-pill inline-flex w-full items-center gap-1 rounded-xl p-1 sm:w-auto"
          >
            <button
              type="button"
              role="tab"
              aria-selected={mode === "measure"}
              onClick={() => {
                setMode("measure");
                setError(null);
              }}
              data-testid="create-insight-mode-measure"
              className={cn(
                "flex-1 rounded-lg px-3 py-1.5 text-[12.5px] font-medium transition-colors sm:flex-none cursor-pointer",
                mode === "measure" ? "liquid-glass-active" : "text-muted hover:text-fg",
              )}
            >
              Pick a governed measure
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "custom"}
              onClick={() => {
                setMode("custom");
                setError(null);
              }}
              data-testid="create-insight-mode-custom"
              className={cn(
                "inline-flex flex-1 items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-[12.5px] font-medium transition-colors sm:flex-none cursor-pointer",
                mode === "custom" ? "liquid-glass-active" : "text-muted hover:text-fg",
              )}
            >
              <Sparkles className="size-3.5 shrink-0" strokeWidth={1.9} aria-hidden />
              Custom chart from plain English
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {mode === "measure" ? (
            <div className="space-y-4">
              {/* Search + Domain Pills */}
              <div className="space-y-2.5">
                <label className="flex h-9 items-center gap-2 rounded-lg border border-border bg-bg/80 px-3 text-[13px] focus-within:border-border-strong">
                  <Search className="size-3.5 shrink-0 text-faint" strokeWidth={2} aria-hidden />
                  <input
                    type="search"
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder="Search Finance, Sales, Purchasing, Operations, or Quality measures"
                    aria-label="Search governed measures"
                    data-testid="create-insight-search"
                    className="min-w-0 flex-1 bg-transparent text-fg outline-none focus-visible:outline-none placeholder:text-faint"
                  />
                  {search && (
                    <button
                      type="button"
                      onClick={() => setSearch("")}
                      aria-label="Clear search"
                      className="text-faint hover:text-fg cursor-pointer"
                    >
                      <X className="size-3.5" aria-hidden />
                    </button>
                  )}
                </label>

                <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Filter measures by domain">
                  {domains.map((d) => (
                    <button
                      key={d.name}
                      type="button"
                      aria-pressed={domainFilter === d.name}
                      onClick={() => setDomainFilter(d.name)}
                      className={cn(
                        "inline-flex h-7 items-center gap-1 rounded-md px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
                        domainFilter === d.name
                          ? "bg-surface-2 text-fg"
                          : "text-muted hover:bg-hover hover:text-fg",
                      )}
                    >
                      <span>{d.name}</span>
                      <span className="num text-[11px] text-faint">{d.count}</span>
                    </button>
                  ))}
                </div>
              </div>

              {/* Grouped Selectable List */}
              <div className="max-h-[240px] space-y-3 overflow-y-auto rounded-xl border border-border/80 bg-bg/60 p-2">
                {groupedCatalog.length === 0 ? (
                  <p className="py-8 text-center text-[13px] text-muted">
                    No governed measures match &ldquo;{search}&rdquo;. Switch to custom plain English to query it directly.
                  </p>
                ) : (
                  groupedCatalog.map(([domainName, entries]) => (
                    <div key={domainName} className="space-y-1">
                      <div className="px-2 pt-1 text-[11px] font-semibold uppercase tracking-[0.04em] text-faint">
                        {domainName}
                      </div>
                      <ul className="space-y-0.5">
                        {entries.map((entry) => {
                          const isSelected = selectedEntry?.key === entry.key;
                          const alreadyOnCanvas = Boolean(entry.tile && canvasIdSet?.has(entry.tile.id));
                          return (
                            <li key={entry.key}>
                              <button
                                type="button"
                                onClick={() => handleSelectMeasure(entry)}
                                data-testid={`create-insight-option-${entry.key}`}
                                className={cn(
                                  "flex w-full items-start justify-between gap-3 rounded-lg px-2.5 py-2 text-left transition-colors cursor-pointer",
                                  isSelected
                                    ? "bg-row-on text-fg ring-1 ring-border-strong"
                                    : "text-fg hover:bg-hover",
                                )}
                              >
                                <div className="min-w-0 flex-1">
                                  <div className="flex items-center gap-2">
                                    <span className="truncate text-[13px] font-medium">{entry.title}</span>
                                    {alreadyOnCanvas && (
                                      <span className="inline-flex shrink-0 items-center gap-1 text-[11px] font-medium text-ok">
                                        <Check className="size-3" strokeWidth={2.2} aria-hidden />
                                        On canvas
                                      </span>
                                    )}
                                  </div>
                                  {entry.description && (
                                    <p className="mt-0.5 line-clamp-1 text-[12px] text-muted">
                                      {entry.description}
                                    </p>
                                  )}
                                </div>
                                <span className="shrink-0 rounded border border-border/70 px-1.5 py-0.5 text-[10.5px] font-medium text-faint">
                                  {entry.tile ? "Live tile" : "Measure"}
                                </span>
                              </button>
                            </li>
                          );
                        })}
                      </ul>
                    </div>
                  ))
                )}
              </div>
            </div>
          ) : (
            <div className="space-y-4">
              <div className="flex items-center justify-between gap-2">
                <span className="text-[12.5px] font-medium text-fg">Describe the chart you want</span>
                <span
                  data-testid="create-insight-mode-badge"
                  className={cn(
                    "inline-flex items-center gap-1.5 text-[12px] font-medium",
                    isInstantLiveCustom
                      ? "border-ok-line/60 bg-ok-soft text-ok"
                      : "border-border-strong bg-surface text-muted",
                  )}
                >
                  <span
                    className={cn("size-1.5 rounded-full", isInstantLiveCustom ? "bg-dot-ok" : "bg-accent")}
                    aria-hidden
                  />
                  {isInstantLiveCustom ? "Instant live tile" : "Custom AI query"}
                </span>
              </div>

              <textarea
                rows={3}
                value={customPrompt}
                onChange={(e) => setCustomPrompt(e.target.value)}
                placeholder="Example: Monthly revenue by top 10 customers, or open PO value by supplier"
                aria-label="Describe custom chart"
                data-testid="create-insight-custom-prompt"
                className="w-full rounded-xl border border-border-strong bg-bg/85 px-3 py-2.5 text-[13px] text-fg outline-none placeholder:text-faint focus:border-fg/40"
              />

              {/* Quick prompt starters */}
              <div className="flex flex-wrap items-center gap-1.5">
                {CUSTOM_EXAMPLES.map((example) => (
                  <button
                    key={example}
                    type="button"
                    onClick={() => setCustomPrompt(example)}
                    className="rounded-md border border-border/80 bg-bg/60 px-2 py-1 text-[11.5px] text-muted transition-colors hover:border-border-strong hover:text-fg cursor-pointer"
                  >
                    {example}
                  </button>
                ))}
              </div>

              {/* Instant catalog match suggestion */}
              {instantMatch?.tile && (
                <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border-strong bg-surface/60 px-3 py-2.5">
                  <div className="min-w-0">
                    <div className="text-[11px] font-medium uppercase tracking-[0.04em] text-ok">
                      Instant live tile available
                    </div>
                    <div className="truncate text-[13px] font-medium text-fg">{instantMatch.title}</div>
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      onAddTile(instantMatch.tile!.id, { size: widthPreset, chartKind });
                      onClose();
                    }}
                    className="inline-flex h-7 shrink-0 items-center gap-1 rounded-md bg-fg px-2.5 text-[12px] font-medium text-bg hover:opacity-90 cursor-pointer"
                  >
                    <Plus className="size-3" strokeWidth={2.2} aria-hidden />
                    Add live tile
                  </button>
                </div>
              )}

              {/* Optional Direct SQL / Title override for instant save */}
              <div className="space-y-2 border-t border-border/70 pt-3">
                <button
                  type="button"
                  onClick={() => setShowSql((v) => !v)}
                  className="inline-flex items-center gap-1.5 text-[12px] font-medium text-muted hover:text-fg cursor-pointer"
                >
                  <Code2 className="size-3.5" strokeWidth={1.9} aria-hidden />
                  {showSql ? "Hide direct SQL (instant save)" : "Have SQL? Save as an instant live tile"}
                </button>
                {showSql && (
                  <div className="space-y-2.5">
                    <input
                      type="text"
                      value={customTitle}
                      onChange={(e) => setCustomTitle(e.target.value)}
                      placeholder="Tile title (optional, derived from description if blank)"
                      aria-label="Custom tile title"
                      className="h-8 w-full rounded-lg border border-border-strong bg-bg px-2.5 text-[12.5px] text-fg placeholder:text-faint"
                    />
                    <textarea
                      rows={3}
                      value={customSql}
                      onChange={(e) => setCustomSql(e.target.value)}
                      placeholder="SELECT month, SUM(net_amount) AS value FROM biz.sales_invoices GROUP BY 1 ORDER BY 1"
                      aria-label="Custom SQL query"
                      data-testid="create-insight-custom-sql"
                      className="w-full rounded-lg border border-border-strong bg-bg px-2.5 py-2 font-mono text-[12px] text-fg placeholder:text-faint"
                    />
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Shared Chart Type + Width Preset Controls */}
          <div className="mt-5 grid grid-cols-1 gap-4 border-t border-border/80 pt-4 sm:grid-cols-12">
            <div className="space-y-1.5 sm:col-span-7">
              <div className="text-[11.5px] font-medium text-muted">Chart type</div>
              <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Chart type">
                {CHART_KIND_OPTIONS.map((opt) => {
                  const Icon = opt.icon;
                  const active = chartKind === opt.id;
                  return (
                    <button
                      key={opt.id}
                      type="button"
                      aria-pressed={active}
                      onClick={() => setChartKind(opt.id)}
                      data-testid={`create-insight-chart-${opt.id}`}
                      className={cn(
                        "inline-flex h-7 items-center gap-1 rounded-md border px-2 text-[12px] font-medium transition-colors cursor-pointer",
                        active
                          ? "border-border-strong bg-surface-2 text-fg"
                          : "border-border/80 bg-bg/60 text-muted hover:border-border-strong hover:text-fg",
                      )}
                    >
                      <Icon className="size-3 shrink-0" strokeWidth={1.9} aria-hidden />
                      {opt.label}
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="space-y-1.5 sm:col-span-5">
              <div className="text-[11.5px] font-medium text-muted">Canvas width</div>
              <div className="flex items-center gap-1" role="group" aria-label="Canvas width preset">
                {WIDTH_PRESETS.map((preset) => {
                  const active = widthPreset === preset.id;
                  return (
                    <button
                      key={preset.id}
                      type="button"
                      aria-pressed={active}
                      onClick={() => setWidthPreset(preset.id)}
                      data-testid={`create-insight-width-${preset.id}`}
                      className={cn(
                        "flex-1 rounded-md border px-2 py-1 text-center text-[12px] font-medium transition-colors cursor-pointer",
                        active
                          ? "border-border-strong bg-surface-2 text-fg"
                          : "border-border/80 bg-bg/60 text-muted hover:border-border-strong hover:text-fg",
                      )}
                    >
                      {preset.label}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>

          {error && (
            <p role="alert" className="mt-3 text-[12.5px] text-late">
              {error}
            </p>
          )}
        </div>

        {/* Footer */}
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-5 py-3.5">
          <div className="min-w-0 text-[12px] text-muted">
            {mode === "measure" && selectedEntry ? (
              <span className="truncate">
                Selected: <strong className="font-medium text-fg">{selectedEntry.title}</strong>
              </span>
            ) : mode === "custom" ? (
              <span>
                {isInstantLiveCustom
                  ? "Saves immediately to your live dashboard"
                  : "Ask generates verified SQL and offers 1-click Save to canvas"}
              </span>
            ) : null}
          </div>

          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="h-8 rounded-lg border border-border px-3 text-[12.5px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
            >
              Cancel
            </button>
            {mode === "measure" ? (
              <button
                type="button"
                disabled={!selectedEntry || saving}
                onClick={() => void handleAddSelectedMeasure()}
                data-testid="create-insight-submit"
                className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-fg px-3.5 text-[12.5px] font-medium text-bg hover:opacity-90 disabled:opacity-50 cursor-pointer"
              >
                {saving ? (
                  <Loader2 className="size-3.5 animate-spin" aria-hidden />
                ) : (
                  <Plus className="size-3.5" strokeWidth={2.2} aria-hidden />
                )}
                Add to My canvas
              </button>
            ) : isInstantLiveCustom ? (
              <button
                type="button"
                disabled={saving || !customSql.trim()}
                onClick={() => void handleCreateCustomSqlTile()}
                data-testid="create-insight-submit"
                className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-fg px-3.5 text-[12.5px] font-medium text-bg hover:opacity-90 disabled:opacity-50 cursor-pointer"
              >
                {saving ? (
                  <Loader2 className="size-3.5 animate-spin" aria-hidden />
                ) : (
                  <Plus className="size-3.5" strokeWidth={2.2} aria-hidden />
                )}
                Add to My canvas
              </button>
            ) : (
              <Link
                prefetch={false}
                href={askHandoffHref}
                onClick={onClose}
                data-testid="create-insight-ask-handoff"
                className={cn(
                  "inline-flex h-8 items-center gap-1.5 rounded-lg bg-fg px-3.5 text-[12.5px] font-medium text-bg hover:opacity-90",
                  !trimmedPrompt && "pointer-events-none opacity-50",
                )}
              >
                Build in Ask
                <ArrowUpRight className="size-3.5" strokeWidth={2} aria-hidden />
              </Link>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
