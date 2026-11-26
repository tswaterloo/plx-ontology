"use client";

import {
  ArrowUpRight,
  Bell,
  Check,
  ChevronDown,
  ChevronUp,
  Code2,
  Copy,
  Database,
  ExternalLink,
  LayoutGrid,
  Maximize2,
  Minimize2,
  Network,
  Pin,
  Plus,
  Search,
  ShieldCheck,
  Table2,
  X,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { CreateTriggerModal } from "@/components/insights/create-trigger-modal";
import { DotGridCanvas, type DotGridEdge, type DotGridNode } from "@/components/ui/dot-grid-canvas";
import { MeasureCandidates } from "@/components/watching/measure-candidates";
import { getInsight } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { InsightInfo, InsightList } from "@/lib/types";

type Measure = NonNullable<InsightList["measures"]>[number];
export type OntologySubView = "split" | "graph" | "catalog" | "candidates";

const DOMAIN_ORDER = [
  "Finance",
  "Purchasing",
  "Operations",
  "Quality",
  "Sales",
  "Customers",
  "Customer Service",
  "Team",
];

const CATALOG_PAGE_SIZE = 12;
const ALL_DOMAINS_GRAPH_LIMIT = 10;

export interface OntologyInspectorModel {
  id: string;
  kind: "asset" | "measure" | "surface";
  kindLabel: string;
  title: string;
  domain: string;
  description: string;
  owner?: string;
  version?: string;
  unit?: string;
  source?: string;
  dimensions?: string[];
  period?: string;
  sql?: string;
  askQuestion?: string;
  recordsHref: string;
  tile?: InsightInfo;
  upstreamIds: string[];
  downstreamIds: string[];
}

export interface NormalizedMeasure {
  id: string;
  name: string;
  definition: string;
  owner: string;
  domain: string;
  unit: string;
  version: string;
  source: string;
  dimensions: string[];
  period: string;
  ask: string;
  recordsHref: string;
  tile?: InsightInfo;
}

function normalizeSourceName(source?: string | null): string {
  const clean = (source ?? "").trim();
  if (!clean) return "biz.workspace_views";
  return clean.includes(".") ? clean : `biz.${clean}`;
}

/** Pure: map a source table or domain to the real live table in /records. */
export function recordsHrefFor(source: string | undefined, domain: string): string {
  const s = (source ?? "").toLowerCase().replace(/^biz\./, "");
  if (s.includes("purchase_order") || s.includes("po_")) return "/records?kind=orders";
  if (s.includes("sales_order") || s.includes("so_")) return "/records?kind=orders&list=sales_orders";
  if (s.includes("customer") || s.includes("chain")) return "/records?kind=customers";
  if (s.includes("supplier")) return "/records?kind=suppliers";
  if (s.includes("product") || s.includes("item") || s.includes("inventory")) return "/records?kind=products";
  if (s.includes("batch") || s.includes("production") || s.includes("lot")) return "/records?kind=production";
  if (s.includes("invoice") || s.includes("shipment") || s.includes("receipt") || s.includes("credit")) {
    return "/records?kind=documents";
  }

  const d = domain.toLowerCase();
  if (d.includes("purchasing")) return "/records?kind=orders";
  if (d.includes("sales")) return "/records?kind=orders&list=sales_orders";
  if (d.includes("finance")) return "/records?kind=documents";
  if (d.includes("customer")) return "/records?kind=customers";
  if (d.includes("quality") || d.includes("operations")) return "/records?kind=production";
  return "/records?kind=lists";
}

function measureSqlPreview(m: NormalizedMeasure): string {
  const dims = m.dimensions.length > 0 ? m.dimensions.slice(0, 3).join(", ") : "month";
  const periodComment = m.period ? ` -- ${m.period}` : "";
  return `SELECT ${dims}, ${m.id}\nFROM ${m.source}\nGROUP BY ${dims}${periodComment};`;
}

/** Pure: merge server `list.measures` with any governed measure tiles into a unified catalog. */
export function buildNormalizedMeasures(
  measures: readonly Measure[],
  items: readonly InsightInfo[],
): NormalizedMeasure[] {
  const out: NormalizedMeasure[] = [];
  const seen = new Set<string>();

  for (const m of measures) {
    seen.add(m.id);
    const tile = items.find((t) => t.measure?.id === m.id || t.id === `measure_${m.id}`);
    const domain = m.owner || (tile?.group ?? "").replace(/^Measures:\s*/i, "") || "Operations";
    const source = normalizeSourceName(m.source);
    out.push({
      id: m.id,
      name: m.name,
      definition: m.definition,
      owner: m.owner || domain,
      domain,
      unit: m.unit || "value",
      version: tile?.measure?.version || "v1",
      source,
      dimensions: m.dimensions ?? [],
      period: m.period || "Active reporting period",
      ask: m.ask?.trim() || `Show ${m.name} by ${m.dimensions?.[0] || "month"}`,
      recordsHref: recordsHrefFor(source, domain),
      tile,
    });
  }

  for (const it of items) {
    if (!it.measure || seen.has(it.measure.id)) continue;
    seen.add(it.measure.id);
    const domain = it.measure.owner || (it.group ?? "").replace(/^Measures:\s*/i, "") || "Operations";
    const source = `biz.${domain.toLowerCase().replace(/[^a-z0-9]+/g, "_")}_view`;
    out.push({
      id: it.measure.id,
      name: it.measure.name || it.title,
      definition: it.measure.definition || it.about,
      owner: it.measure.owner || domain,
      domain,
      unit: "governed",
      version: it.measure.version || "v1",
      source,
      dimensions: it.chart?.x ? [it.chart.x] : ["month"],
      period: it.measure.defined_on ? `Defined ${it.measure.defined_on}` : "Active reporting period",
      ask: it.question?.trim() || `Explain recent changes in ${it.measure.name || it.title}`,
      recordsHref: recordsHrefFor(source, domain),
      tile: it,
    });
  }

  return out;
}

/**
 * Pure: build a real 2- or 3-level lineage graph from real data ONLY:
 * Data table/view -> Governed measure -> Live dashboard tile (when present).
 * Zero synthetic placeholder "Ask agent" nodes.
 */
export function buildOntologyGraph(
  measures: readonly NormalizedMeasure[],
  items: readonly InsightInfo[],
  canvasIdSet: Set<string>,
  selectedDomain: string,
  searchQuery = "",
  focusMeasureId: string | null = null,
): {
  nodes: DotGridNode[];
  edges: DotGridEdge[];
  inspectors: Record<string, OntologyInspectorModel>;
  totalMeasuresInDomain: number;
  shownMeasuresCount: number;
  hasSurfaces: boolean;
} {
  const q = searchQuery.trim().toLowerCase();
  const domainMeasures = measures.filter((m) => {
    if (selectedDomain !== "All" && m.domain.toLowerCase() !== selectedDomain.toLowerCase()) {
      return false;
    }
    if (!q) return true;
    return `${m.name} ${m.definition} ${m.domain} ${m.owner} ${m.source} ${m.dimensions.join(" ")} ${m.tile?.title ?? ""}`
      .toLowerCase()
      .includes(q);
  });

  let shownMeasures =
    selectedDomain === "All" && !q && domainMeasures.length > ALL_DOMAINS_GRAPH_LIMIT
      ? domainMeasures.slice(0, ALL_DOMAINS_GRAPH_LIMIT)
      : domainMeasures;

  if (focusMeasureId && !shownMeasures.some((m) => m.id === focusMeasureId)) {
    const focused = domainMeasures.find((m) => m.id === focusMeasureId);
    if (focused) {
      shownMeasures = [focused, ...shownMeasures.slice(0, Math.max(0, ALL_DOMAINS_GRAPH_LIMIT - 1))];
    }
  }

  const nodes: DotGridNode[] = [];
  const edges: DotGridEdge[] = [];
  const inspectors: Record<string, OntologyInspectorModel> = {};

  // Column 1: Unique source tables / views
  const assetToMeasures = new Map<string, NormalizedMeasure[]>();
  for (const m of shownMeasures) {
    const existing = assetToMeasures.get(m.source);
    if (existing) existing.push(m);
    else assetToMeasures.set(m.source, [m]);
  }
  const assets = [...assetToMeasures.entries()];

  // Column 3: REAL live dashboard tiles & scheduled monitors ONLY
  type SurfaceEntry = {
    id: string;
    title: string;
    kindLabel: string;
    description: string;
    badge: string;
    iconKind: "tile" | "monitor";
    domain: string;
    recordsHref: string;
    tile: InsightInfo;
    askQuestion: string;
    measureIds: string[];
  };
  const surfaceMap = new Map<string, SurfaceEntry>();

  for (const m of shownMeasures) {
    if (!m.tile) continue;
    const isMonitor = Boolean(m.tile.saved && m.tile.schedule);
    const surfId = `surface:tile:${m.tile.id}`;
    const existing = surfaceMap.get(surfId);
    if (existing) {
      existing.measureIds.push(m.id);
    } else {
      surfaceMap.set(surfId, {
        id: surfId,
        title: m.tile.title,
        kindLabel: isMonitor ? "Trigger" : "Dashboard tile",
        description: isMonitor
          ? `Runs ${m.tile.schedule} using ${m.name}.`
          : `Live chart backed by ${m.name}.`,
        badge: canvasIdSet.has(m.tile.id) ? "On canvas" : isMonitor ? m.tile.schedule : "Catalog",
        iconKind: isMonitor ? "monitor" : "tile",
        domain: m.domain,
        recordsHref: m.recordsHref,
        tile: m.tile,
        askQuestion: m.ask,
        measureIds: [m.id],
      });
    }
  }

  const scheduledMonitors = items.filter((it) => it.saved && it.schedule && it.measure);
  for (const mon of scheduledMonitors) {
    const mid = mon.measure?.id;
    if (!mid || !shownMeasures.some((m) => m.id === mid)) continue;
    const surfId = `surface:tile:${mon.id}`;
    if (!surfaceMap.has(surfId)) {
      const monDomain = mon.measure?.owner || (mon.group ?? "").replace(/^Measures:\s*/i, "") || "Operations";
      surfaceMap.set(surfId, {
        id: surfId,
        title: mon.title,
        kindLabel: "Trigger",
        description: `Runs ${mon.schedule} as you and checks threshold rules.`,
        badge: mon.schedule || "Watch",
        iconKind: "monitor",
        domain: monDomain,
        recordsHref: recordsHrefFor(undefined, monDomain),
        tile: mon,
        askQuestion: mon.question || `Explain recent changes in ${mon.title}`,
        measureIds: [mid],
      });
    }
  }

  const surfaces = [...surfaceMap.values()];
  const hasSurfaces = surfaces.length > 0;

  const COL_X = hasSurfaces ? ([36, 344, 652] as const) : ([92, 432, 432] as const);
  const NODE_W = 244;
  const NODE_H = 96;
  const ROW_STEP = 126;
  const maxRows = Math.max(assets.length, shownMeasures.length, surfaces.length, 1);
  const totalH = maxRows * ROW_STEP;
  const yStart = (count: number) => Math.max(24, Math.round((totalH - count * ROW_STEP) / 2) + 24);

  // Place Column 1: Data tables / views
  const assetY0 = yStart(assets.length);
  assets.forEach(([source, linkedMeasures], idx) => {
    const id = `asset:${source}`;
    const domains = [...new Set(linkedMeasures.map((m) => m.domain))];
    const primaryDomain = domains[0] ?? "Workspace";
    nodes.push({
      id,
      x: COL_X[0],
      y: assetY0 + idx * ROW_STEP,
      width: NODE_W,
      height: NODE_H,
      kindLabel: "Source table",
      title: source,
      description: `Feeds ${linkedMeasures.length} governed ${linkedMeasures.length === 1 ? "measure" : "measures"}.`,
      badge: `${linkedMeasures.length}`,
      tone: "neutral",
      icon: <Database className="size-3.5" strokeWidth={1.8} aria-hidden />,
    });
    inspectors[id] = {
      id,
      kind: "asset",
      kindLabel: "Source table",
      title: source,
      domain: domains.join(", ") || "Workspace",
      description: `Governed business view supplying ${linkedMeasures.length} ${linkedMeasures.length === 1 ? "measure" : "measures"} under per-reader row permissions.`,
      source,
      sql: `SELECT *\nFROM ${source}\nLIMIT 50;`,
      askQuestion: `Summarize recent activity from ${source}`,
      recordsHref: recordsHrefFor(source, primaryDomain),
      upstreamIds: [],
      downstreamIds: linkedMeasures.map((m) => `measure:${m.id}`),
    };
  });

  // Place Column 2: Governed measures
  const measureY0 = yStart(shownMeasures.length);
  shownMeasures.forEach((m, idx) => {
    const id = `measure:${m.id}`;
    const connectedSurfaces = surfaces.filter((s) => s.measureIds.includes(m.id));
    nodes.push({
      id,
      x: COL_X[1],
      y: measureY0 + idx * ROW_STEP,
      width: NODE_W,
      height: NODE_H,
      kindLabel: m.domain,
      title: m.name,
      description: m.definition,
      badge: m.version,
      tone: "accent",
      icon: <ShieldCheck className="size-3.5" strokeWidth={1.8} aria-hidden />,
    });
    inspectors[id] = {
      id,
      kind: "measure",
      kindLabel: "Governed measure",
      title: m.name,
      domain: m.domain,
      description: m.definition,
      owner: m.owner,
      version: m.version,
      unit: m.unit,
      source: m.source,
      dimensions: m.dimensions,
      period: m.period,
      sql: measureSqlPreview(m),
      askQuestion: m.ask,
      recordsHref: m.recordsHref,
      tile: m.tile,
      upstreamIds: [`asset:${m.source}`],
      downstreamIds: connectedSurfaces.map((s) => s.id),
    };
    edges.push({
      id: `edge:${m.source}->${m.id}`,
      from: `asset:${m.source}`,
      to: id,
    });
  });

  // Place Column 3: Live dashboard tiles / scheduled monitors
  if (hasSurfaces) {
    const surfaceY0 = yStart(surfaces.length);
    surfaces.forEach((s, idx) => {
      const icon =
        s.iconKind === "monitor" ? (
          <Bell className="size-3.5" strokeWidth={1.8} aria-hidden />
        ) : (
          <LayoutGrid className="size-3.5" strokeWidth={1.8} aria-hidden />
        );
      nodes.push({
        id: s.id,
        x: COL_X[2],
        y: surfaceY0 + idx * ROW_STEP,
        width: NODE_W,
        height: NODE_H,
        kindLabel: s.kindLabel,
        title: s.title,
        description: s.description,
        badge: s.badge,
        tone: "neutral",
        icon,
      });
      inspectors[s.id] = {
        id: s.id,
        kind: "surface",
        kindLabel: s.kindLabel,
        title: s.title,
        domain: s.domain,
        description: s.description,
        tile: s.tile,
        askQuestion: s.askQuestion,
        recordsHref: s.recordsHref,
        upstreamIds: s.measureIds.map((mid) => `measure:${mid}`),
        downstreamIds: [],
      };
      for (const mid of s.measureIds) {
        edges.push({
          id: `edge:${mid}->${s.id}`,
          from: `measure:${mid}`,
          to: s.id,
        });
      }
    });
  }

  return {
    nodes,
    edges,
    inspectors,
    totalMeasuresInDomain: domainMeasures.length,
    shownMeasuresCount: shownMeasures.length,
    hasSurfaces,
  };
}

/**
 * Side-by-Side Split Explorer for Governed Measures & Lineage:
 * - Left pane (400px on desktop): scannable Governed Measures catalog with search, domain filter,
 *   plain-English definitions, source table badges, and 1-click Pin to canvas / Set trigger / Inspect.
 * - Right pane (1fr on desktop): Interactive dotted grid lineage canvas ALWAYS visible on desktop
 *   showing Source Table -> Governed Measure -> Live Dashboard Tile, plus a collapsible SQL &
 *   Definition Inspector cleanly docked along the bottom.
 */
export function OntologyAndMeasuresPanel({
  measures,
  items,
  canvasIdSet,
  onToggleCanvas,
  onJumpToTile,
}: {
  measures: Measure[];
  items: InsightInfo[];
  canvasIdSet: Set<string>;
  onToggleCanvas: (item: InsightInfo) => void;
  onJumpToTile: (id: string) => void;
}) {
  const [subView, setSubView] = useState<OntologySubView>("split");
  const [selectedDomain, setSelectedDomain] = useState<string>("All");
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [triggerMeasureName, setTriggerMeasureName] = useState<string | null>(null);

  const [catalogLimit, setCatalogLimit] = useState(CATALOG_PAGE_SIZE);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const liveSqlCache = useRef<Record<string, string>>({});
  const [liveSqlMap, setLiveSqlMap] = useState<Record<string, string>>({});

  const normalizedMeasures = useMemo(
    () => buildNormalizedMeasures(measures, items),
    [measures, items],
  );

  const domains = useMemo(() => {
    const counts = new Map<string, number>();
    for (const m of normalizedMeasures) {
      counts.set(m.domain, (counts.get(m.domain) ?? 0) + 1);
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
      { name: "All", label: "All", count: normalizedMeasures.length },
      ...ordered.map((name) => ({ name, label: name, count: counts.get(name) ?? 0 })),
    ];
  }, [normalizedMeasures]);

  const focusedMeasureId = selectedNodeId?.startsWith("measure:")
    ? selectedNodeId.slice("measure:".length)
    : null;

  const graph = useMemo(
    () =>
      buildOntologyGraph(
        normalizedMeasures,
        items,
        canvasIdSet,
        selectedDomain,
        searchQuery,
        focusedMeasureId,
      ),
    [normalizedMeasures, items, canvasIdSet, selectedDomain, searchQuery, focusedMeasureId],
  );

  const activeNodeId = useMemo(() => {
    if (selectedNodeId && graph.inspectors[selectedNodeId]) return selectedNodeId;
    const firstMeasure = graph.nodes.find((n) => n.id.startsWith("measure:"));
    return firstMeasure?.id ?? graph.nodes[0]?.id ?? null;
  }, [selectedNodeId, graph.inspectors, graph.nodes]);

  const activeInspector = activeNodeId ? graph.inspectors[activeNodeId] ?? null : null;
  const activeMeasureId = activeNodeId?.startsWith("measure:") ? activeNodeId.slice("measure:".length) : null;

  const activeTileId = activeInspector?.tile?.id ?? null;
  useEffect(() => {
    if (!activeTileId || liveSqlCache.current[activeTileId]) return;
    const ctl = new AbortController();
    getInsight(activeTileId, ctl.signal, false)
      .then((res) => {
        if (!ctl.signal.aborted && res?.sql) {
          liveSqlCache.current[activeTileId] = res.sql;
          setLiveSqlMap((prev) => ({ ...prev, [activeTileId]: res.sql }));
        }
      })
      .catch(() => {
        // Fall back quietly to the measure SQL preview
      });
    return () => ctl.abort();
  }, [activeTileId]);

  const activeSql =
    (activeTileId && liveSqlMap[activeTileId]) ||
    activeInspector?.sql ||
    null;

  const filteredCatalog = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return normalizedMeasures.filter((m) => {
      if (selectedDomain !== "All" && m.domain.toLowerCase() !== selectedDomain.toLowerCase()) return false;
      if (!q) return true;
      return `${m.name} ${m.owner} ${m.domain} ${m.definition} ${m.source} ${m.dimensions.join(" ")}`
        .toLowerCase()
        .includes(q);
    });
  }, [normalizedMeasures, selectedDomain, searchQuery]);

  const visibleCatalog = useMemo(
    () => filteredCatalog.slice(0, catalogLimit),
    [filteredCatalog, catalogLimit],
  );
  const remainingCatalog = Math.max(0, filteredCatalog.length - visibleCatalog.length);

  const assetCount = useMemo(
    () => new Set(normalizedMeasures.map((m) => m.source)).size,
    [normalizedMeasures],
  );
  const tileCount = useMemo(
    () => normalizedMeasures.filter((m) => Boolean(m.tile)).length,
    [normalizedMeasures],
  );

  const copySql = async (id: string, sql: string) => {
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(sql);
      }
      setCopiedId(id);
      window.setTimeout(() => {
        setCopiedId((cur) => (cur === id ? null : cur));
      }, 1800);
    } catch {
      // ignore clipboard restrictions
    }
  };

  const selectMeasure = (measureId: string, domain: string) => {
    if (selectedDomain !== "All" && selectedDomain.toLowerCase() !== domain.toLowerCase()) {
      setSelectedDomain(domain);
    }
    setSelectedNodeId(`measure:${measureId}`);
    setInspectorCollapsed(false);
  };

  const isGraphExpanded = subView === "graph";
  const showCandidates = subView === "candidates";

  return (
    <div className="space-y-4" data-testid="ontology-tab-panel">
      <CreateTriggerModal
        open={triggerMeasureName !== null}
        onClose={() => setTriggerMeasureName(null)}
        measures={measures}
        initialMeasureName={triggerMeasureName ?? undefined}
      />

      {/* Unified Split-Explorer Studio Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/80 pb-3">
        <div className="flex flex-wrap items-baseline gap-3">
          <h2 className="text-[15px] font-semibold tracking-[-0.015em] text-fg">
            Governed measures and lineage
          </h2>
          <div className="inline-flex items-center gap-2 text-[12px] text-faint tabular-nums">
            <span>{assetCount} {assetCount === 1 ? "table" : "tables"}</span>
            <span className="h-3 w-px bg-border" aria-hidden />
            <span>{normalizedMeasures.length} {normalizedMeasures.length === 1 ? "measure" : "measures"}</span>
            {tileCount > 0 && (
              <>
                <span className="h-3 w-px bg-border" aria-hidden />
                <span>{tileCount} live {tileCount === 1 ? "tile" : "tiles"}</span>
              </>
            )}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div
            role="tablist"
            aria-label="Governed measures layout"
            className="inline-flex items-center gap-0.5 rounded-lg border border-border/80 bg-surface/60 p-0.5"
            data-testid="ontology-subview-tabs"
          >
            <button
              type="button"
              role="tab"
              aria-selected={subView === "split" || subView === "catalog"}
              onClick={() => setSubView("split")}
              data-testid="subtab-measures-catalog"
              className={cn(
                "inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
                subView === "split" || subView === "catalog"
                  ? "bg-bg text-fg shadow-sm dark:bg-surface-2"
                  : "text-muted hover:text-fg",
              )}
            >
              <Table2 className="size-3.5 shrink-0" strokeWidth={1.9} aria-hidden />
              Split explorer
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={subView === "graph"}
              onClick={() => setSubView(isGraphExpanded ? "split" : "graph")}
              data-testid="subtab-ontology-graph"
              className={cn(
                "inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
                isGraphExpanded
                  ? "bg-bg text-fg shadow-sm dark:bg-surface-2"
                  : "text-muted hover:text-fg",
              )}
            >
              {isGraphExpanded ? (
                <Minimize2 className="size-3.5 shrink-0" strokeWidth={1.9} aria-hidden />
              ) : (
                <Maximize2 className="size-3.5 shrink-0" strokeWidth={1.9} aria-hidden />
              )}
              {isGraphExpanded ? "Split view" : "Expand graph"}
            </button>
          </div>

          <button
            type="button"
            role="tab"
            aria-selected={showCandidates}
            onClick={() => setSubView(showCandidates ? "split" : "candidates")}
            data-testid="subtab-candidate-review"
            className={cn(
              "inline-flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
              showCandidates
                ? "border-border-strong bg-surface-2 text-fg"
                : "border-border bg-bg text-muted hover:border-border-strong hover:bg-hover hover:text-fg",
            )}
          >
            <ShieldCheck className="size-3.5 shrink-0" strokeWidth={1.9} aria-hidden />
            Proposed measures
          </button>
        </div>
      </div>

      {showCandidates ? (
        <section
          className="pt-1"
          aria-label="Proposed measures queue"
          data-testid="candidates-tab-panel"
        >
          <MeasureCandidates />
        </section>
      ) : (
        <div
          className={cn(
            "grid grid-cols-1 items-start gap-5",
            !isGraphExpanded && "lg:grid-cols-[400px_1fr]",
          )}
          data-testid="ontology-split-workspace"
        >
          {/* Expanded graph filter strip (shown only when catalog pane is collapsed) */}
          {isGraphExpanded && (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div
                className="flex flex-wrap items-center gap-1"
                role="group"
                aria-label="Filter ontology by domain"
                data-testid="ontology-domain-filters"
              >
                {domains.map((d) => (
                  <button
                    key={d.name}
                    type="button"
                    aria-pressed={selectedDomain === d.name}
                    onClick={() => {
                      setSelectedDomain(d.name);
                      setSelectedNodeId(null);
                      setCatalogLimit(CATALOG_PAGE_SIZE);
                    }}
                    className={cn(
                      "inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
                      selectedDomain === d.name
                        ? "bg-surface-2 text-fg"
                        : "text-muted hover:bg-hover hover:text-fg",
                    )}
                  >
                    <span>{d.label}</span>
                    <span className="num text-[11px] text-faint">{d.count}</span>
                  </button>
                ))}
              </div>

              <label className="relative flex h-8 w-64 max-w-full items-center gap-2 rounded-md border border-border bg-bg px-2.5 text-[12.5px] focus-within:border-border-strong">
                <Search className="size-3.5 shrink-0 text-faint" strokeWidth={2} aria-hidden />
                <input
                  type="search"
                  value={searchQuery}
                  onChange={(e) => {
                    setSearchQuery(e.target.value);
                    setCatalogLimit(CATALOG_PAGE_SIZE);
                  }}
                  placeholder="Search measures, tables, domains"
                  aria-label="Search ontology and measures"
                  data-testid="ontology-search"
                  className="min-w-0 flex-1 bg-transparent text-fg outline-none focus-visible:outline-none placeholder:text-faint"
                />
                {searchQuery && (
                  <button
                    type="button"
                    onClick={() => setSearchQuery("")}
                    aria-label="Clear search"
                    className="inline-flex size-4 items-center justify-center rounded text-muted hover:text-fg cursor-pointer"
                  >
                    <X className="size-3" aria-hidden />
                  </button>
                )}
              </label>
            </div>
          )}

          {/* LEFT PANE: Governed Measures Catalog (400px desktop split) */}
          {!isGraphExpanded && (
            <section
              aria-label="Measures catalog"
              data-testid="measures-tab-panel"
              className="flex flex-col overflow-hidden rounded-xl border border-border/80 bg-bg dark:bg-[#14161b]"
            >
              {/* Sticky Search + Compact Domain Filter Strip */}
              <div className="space-y-2.5 border-b border-border/80 p-3">
                <label className="relative flex h-8 w-full items-center gap-2 rounded-md border border-border bg-surface/60 px-2.5 text-[12.5px] focus-within:border-border-strong dark:bg-bg">
                  <Search className="size-3.5 shrink-0 text-faint" strokeWidth={2} aria-hidden />
                  <input
                    type="search"
                    value={searchQuery}
                    onChange={(e) => {
                      setSearchQuery(e.target.value);
                      setCatalogLimit(CATALOG_PAGE_SIZE);
                    }}
                    placeholder="Search measures, tables, domains"
                    aria-label="Search ontology and measures"
                    data-testid="ontology-search"
                    className="min-w-0 flex-1 bg-transparent text-fg outline-none focus-visible:outline-none placeholder:text-faint"
                  />
                  {searchQuery && (
                    <button
                      type="button"
                      onClick={() => setSearchQuery("")}
                      aria-label="Clear search"
                      className="inline-flex size-4 items-center justify-center rounded text-muted hover:text-fg cursor-pointer"
                    >
                      <X className="size-3" aria-hidden />
                    </button>
                  )}
                </label>

                {domains.length > 1 && (
                  <div
                    className="flex flex-wrap items-center gap-1"
                    role="group"
                    aria-label="Filter ontology by domain"
                    data-testid="ontology-domain-filters"
                  >
                    {domains.map((d) => (
                      <button
                        key={d.name}
                        type="button"
                        aria-pressed={selectedDomain === d.name}
                        onClick={() => {
                          setSelectedDomain(d.name);
                          setSelectedNodeId(null);
                          setCatalogLimit(CATALOG_PAGE_SIZE);
                        }}
                        className={cn(
                          "inline-flex h-6 items-center gap-1 rounded-md px-2 text-[11.5px] font-medium transition-colors cursor-pointer",
                          selectedDomain === d.name
                            ? "bg-surface-2 text-fg"
                            : "text-muted hover:bg-hover hover:text-fg",
                        )}
                      >
                        <span>{d.label}</span>
                        <span className="num text-[10.5px] text-faint">{d.count}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {/* Crisp Flush Rows for Governed Measures */}
              {filteredCatalog.length === 0 ? (
                <div className="px-4 py-12 text-center text-[13px] text-muted">
                  No governed measures match this filter.
                </div>
              ) : (
                <div className="max-h-[680px] overflow-y-auto">
                  <table className="w-full border-collapse text-left" data-testid="measures-catalog-table">
                    <thead className="sr-only">
                      <tr>
                        <th>Governed measure</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border/60">
                      {visibleCatalog.map((m) => {
                        const isSelected = activeMeasureId === m.id;
                        const pinned = Boolean(m.tile && canvasIdSet.has(m.tile.id));
                        return (
                          <tr
                            key={m.id}
                            data-testid={`measure-row-${m.id}`}
                            data-selected={isSelected ? "true" : undefined}
                            className={cn(
                              "group/row transition-colors",
                              isSelected ? "bg-row-on" : "hover:bg-hover/60",
                            )}
                          >
                            <td className="px-3.5 py-3">
                              <div className="space-y-1.5">
                                <div className="flex items-start justify-between gap-2">
                                  <button
                                    type="button"
                                    onClick={() => selectMeasure(m.id, m.domain)}
                                    className="min-w-0 flex-1 text-left cursor-pointer"
                                  >
                                    <div className="flex items-baseline gap-1.5">
                                      <span
                                        className={cn(
                                          "truncate text-[13.5px] font-semibold leading-5 tracking-[-0.01em]",
                                          isSelected ? "text-fg" : "text-fg group-hover/row:text-fg",
                                        )}
                                      >
                                        {m.name}
                                      </span>
                                      <span className="shrink-0 font-mono text-[11px] text-faint">
                                        {m.version}
                                      </span>
                                    </div>
                                    <p className="mt-0.5 line-clamp-2 text-[12px] leading-4 text-muted">
                                      {m.definition}
                                    </p>
                                  </button>
                                </div>

                                <div className="flex flex-wrap items-center justify-between gap-2 pt-0.5">
                                  <div className="flex min-w-0 items-center gap-2 text-[11px]">
                                    <span className="text-faint">{m.domain}</span>
                                    <span className="h-2.5 w-px bg-border" aria-hidden />
                                    <Link
                                      href={m.recordsHref}
                                      className="truncate rounded-sm border border-border/80 bg-surface/60 px-1.5 py-0.5 font-mono text-[11px] text-muted hover:border-border-strong hover:text-fg"
                                    >
                                      {m.source}
                                    </Link>
                                  </div>
                                </div>

                                <div className="flex flex-wrap items-center gap-1 pt-0.5">
                                  {m.tile && (
                                    <button
                                      type="button"
                                      onClick={() => onToggleCanvas(m.tile!)}
                                      data-testid={`measure-add-canvas-${m.id}`}
                                      className={cn(
                                        "inline-flex h-6 items-center gap-1 rounded-md border px-2 text-[11.5px] font-medium transition-colors cursor-pointer",
                                        pinned
                                          ? "border-border-strong bg-surface-2 text-fg"
                                          : "border-border bg-bg text-fg hover:border-border-strong hover:bg-hover",
                                      )}
                                    >
                                      {pinned ? (
                                        <>
                                          <Check className="size-3 text-ok" strokeWidth={2.2} aria-hidden />
                                          On canvas
                                        </>
                                      ) : (
                                        <>
                                          <Pin className="size-3" strokeWidth={2} aria-hidden />
                                          Pin to canvas
                                        </>
                                      )}
                                    </button>
                                  )}
                                  <button
                                    type="button"
                                    onClick={() => setTriggerMeasureName(m.name)}
                                    data-testid={`measure-set-trigger-${m.id}`}
                                    className="inline-flex h-6 items-center gap-1 rounded-md border border-border bg-bg px-2 text-[11.5px] font-medium text-muted transition-colors hover:border-border-strong hover:bg-hover hover:text-fg cursor-pointer"
                                  >
                                    <Bell className="size-3" strokeWidth={1.9} aria-hidden />
                                    Set trigger
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => selectMeasure(m.id, m.domain)}
                                    data-testid={`measure-inspect-lineage-${m.id}`}
                                    className={cn(
                                      "inline-flex h-6 items-center gap-1 rounded-md px-2 text-[11.5px] font-medium transition-colors cursor-pointer",
                                      isSelected
                                        ? "bg-surface-2 text-fg"
                                        : "text-muted hover:bg-hover hover:text-fg",
                                    )}
                                  >
                                    <Network className="size-3" strokeWidth={1.9} aria-hidden />
                                    Inspect
                                  </button>
                                </div>
                              </div>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}

              {remainingCatalog > 0 && (
                <div className="flex items-center justify-between gap-2 border-t border-border/80 px-3.5 py-2.5">
                  <span className="text-[11.5px] text-muted tabular-nums">
                    Showing {visibleCatalog.length} of {filteredCatalog.length}
                  </span>
                  <button
                    type="button"
                    onClick={() => setCatalogLimit((n) => n + CATALOG_PAGE_SIZE)}
                    data-testid="measures-show-more"
                    className="h-7 rounded-md border border-border bg-bg px-2.5 text-[12px] font-medium text-fg transition-colors hover:border-border-strong hover:bg-hover cursor-pointer"
                  >
                    Show {Math.min(CATALOG_PAGE_SIZE, remainingCatalog)} more
                  </button>
                </div>
              )}
            </section>
          )}

          {/* RIGHT PANE: Interactive Dotted-Grid Lineage Canvas + Docked Bottom SQL & Definition Inspector */}
          <section
            aria-label="Lineage canvas"
            data-testid="ontology-graph-view"
            className="flex min-w-0 flex-col overflow-hidden rounded-xl border border-border/80 bg-bg dark:bg-[#14161b]"
          >
            {/* Quiet Column Legend Bar */}
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/80 px-4 py-2.5">
              <div
                className={cn(
                  "grid flex-1 gap-4 text-[12px] font-medium text-muted",
                  graph.hasSurfaces ? "grid-cols-3" : "grid-cols-2",
                )}
              >
                <span className="inline-flex items-center gap-1.5">
                  <Database className="size-3.5 text-faint" strokeWidth={1.8} aria-hidden />
                  Source tables
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <ShieldCheck className="size-3.5 text-accent-text" strokeWidth={1.8} aria-hidden />
                  Governed measures
                </span>
                {graph.hasSurfaces && (
                  <span className="inline-flex items-center gap-1.5">
                    <LayoutGrid className="size-3.5 text-faint" strokeWidth={1.8} aria-hidden />
                    Live dashboard tiles
                  </span>
                )}
              </div>
              {graph.totalMeasuresInDomain > graph.shownMeasuresCount && (
                <span className="text-[11.5px] text-faint tabular-nums">
                  {graph.shownMeasuresCount} of {graph.totalMeasuresInDomain} in graph
                </span>
              )}
            </div>

            {/* Interactive Dotted Grid Lineage Canvas (always mounted & visible) */}
            <div className="p-3">
              <DotGridCanvas
                nodes={graph.nodes}
                edges={graph.edges}
                selectedId={activeNodeId}
                onSelectNode={(id) => {
                  setSelectedNodeId(id);
                  setInspectorCollapsed(false);
                }}
                height={isGraphExpanded ? 520 : 420}
                emptyMessage="No tables or measures match this filter."
                ariaLabel="Business data model lineage graph"
              />
            </div>

            {/* Docked Collapsible SQL & Definition Inspector along the bottom of the canvas */}
            {activeInspector && (
              <aside
                aria-label="SQL and definition inspector"
                data-testid="ontology-inspector"
                className="border-t border-border/80 bg-surface/40 dark:bg-[#101217]"
              >
                <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <span className="text-[11.5px] font-medium text-accent-text">
                      {activeInspector.kindLabel}
                    </span>
                    <span className="h-3 w-px bg-border" aria-hidden />
                    <h3 className="truncate text-[13.5px] font-semibold tracking-[-0.01em] text-fg">
                      {activeInspector.title}
                    </h3>
                    <span className="h-3 w-px bg-border" aria-hidden />
                    <span className="text-[12px] text-muted">{activeInspector.domain}</span>
                    {activeInspector.version && (
                      <span className="font-mono text-[11px] text-faint">{activeInspector.version}</span>
                    )}
                  </div>

                  <div className="flex items-center gap-1.5">
                    <button
                      type="button"
                      aria-expanded={!inspectorCollapsed}
                      onClick={() => setInspectorCollapsed((v) => !v)}
                      data-testid="inspector-collapse-toggle"
                      className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-[12px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg cursor-pointer"
                    >
                      {inspectorCollapsed ? (
                        <>
                          <ChevronUp className="size-3.5" strokeWidth={2} aria-hidden />
                          Expand SQL &amp; details
                        </>
                      ) : (
                        <>
                          <ChevronDown className="size-3.5" strokeWidth={2} aria-hidden />
                          Collapse
                        </>
                      )}
                    </button>
                  </div>
                </div>

                {!inspectorCollapsed && (
                  <div className="space-y-3 border-t border-border/70 px-4 py-3.5">
                    <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
                      {/* Left 7 cols: Plain-English Definition, Metadata & Lineage Jump Links */}
                      <div className="space-y-3 xl:col-span-7">
                        <p className="text-[13px] leading-5 text-muted">
                          {activeInspector.description}
                        </p>

                        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 border-y border-border/70 py-2.5 text-[12px]">
                          {activeInspector.owner && (
                            <div>
                              <dt className="text-[11px] text-faint">Owner</dt>
                              <dd className="font-medium text-fg">{activeInspector.owner}</dd>
                            </div>
                          )}
                          {activeInspector.unit && (
                            <div>
                              <dt className="text-[11px] text-faint">Unit</dt>
                              <dd className="font-medium text-fg">{activeInspector.unit}</dd>
                            </div>
                          )}
                          {activeInspector.source && (
                            <div className="col-span-2 sm:col-span-1">
                              <dt className="text-[11px] text-faint">Source table</dt>
                              <dd className="font-mono text-[11.5px] text-fg">{activeInspector.source}</dd>
                            </div>
                          )}
                          {activeInspector.period && (
                            <div className="col-span-2 sm:col-span-1">
                              <dt className="text-[11px] text-faint">Reporting period</dt>
                              <dd className="text-muted">{activeInspector.period}</dd>
                            </div>
                          )}
                          {activeInspector.dimensions && activeInspector.dimensions.length > 0 && (
                            <div className="col-span-2">
                              <dt className="text-[11px] text-faint">Available dimensions</dt>
                              <dd className="mt-0.5 flex flex-wrap items-center gap-x-2.5 gap-y-0.5 font-mono text-[11.5px] text-muted">
                                {activeInspector.dimensions.map((dim) => (
                                  <span key={dim}>{dim}</span>
                                ))}
                              </dd>
                            </div>
                          )}
                        </dl>

                        {(activeInspector.upstreamIds.length > 0 || activeInspector.downstreamIds.length > 0) && (
                          <div className="flex flex-wrap items-center gap-4 text-[12px]">
                            {activeInspector.upstreamIds.length > 0 && (
                              <div className="flex flex-wrap items-center gap-1.5">
                                <span className="text-[11.5px] text-faint">Upstream:</span>
                                {activeInspector.upstreamIds.map((uid) => {
                                  const refNode = graph.inspectors[uid];
                                  if (!refNode) return null;
                                  return (
                                    <button
                                      key={uid}
                                      type="button"
                                      onClick={() => setSelectedNodeId(uid)}
                                      className="rounded-md border border-border bg-bg px-2 py-0.5 font-mono text-[11px] text-fg transition-colors hover:border-border-strong hover:bg-hover cursor-pointer"
                                    >
                                      {refNode.title}
                                    </button>
                                  );
                                })}
                              </div>
                            )}
                            {activeInspector.downstreamIds.length > 0 && (
                              <div className="flex flex-wrap items-center gap-1.5">
                                <span className="text-[11.5px] text-faint">Used by:</span>
                                {activeInspector.downstreamIds.map((did) => {
                                  const refNode = graph.inspectors[did];
                                  if (!refNode) return null;
                                  return (
                                    <button
                                      key={did}
                                      type="button"
                                      onClick={() => setSelectedNodeId(did)}
                                      className="rounded-md border border-border bg-bg px-2 py-0.5 text-[11.5px] text-fg transition-colors hover:border-border-strong hover:bg-hover cursor-pointer"
                                    >
                                      {refNode.title}
                                    </button>
                                  );
                                })}
                              </div>
                            )}
                          </div>
                        )}
                      </div>

                      {/* Right 5 cols: Live Governed SQL Preview */}
                      <div className="space-y-1.5 xl:col-span-5">
                        {activeSql ? (
                          <>
                            <div className="flex items-center justify-between gap-2">
                              <span className="inline-flex items-center gap-1.5 text-[11.5px] font-medium text-muted">
                                <Code2 className="size-3.5 text-faint" strokeWidth={2} aria-hidden />
                                <span>Governed SQL</span>
                              </span>
                              <button
                                type="button"
                                onClick={() => void copySql(activeInspector.id, activeSql)}
                                data-testid="inspector-copy-sql"
                                className="inline-flex h-6 items-center gap-1 rounded-md px-1.5 text-[11.5px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg cursor-pointer"
                              >
                                {copiedId === activeInspector.id ? (
                                  <>
                                    <Check className="size-3 text-ok" strokeWidth={2.2} aria-hidden />
                                    <span className="text-ok">Copied</span>
                                  </>
                                ) : (
                                  <>
                                    <Copy className="size-3" strokeWidth={2} aria-hidden />
                                    <span>Copy SQL</span>
                                  </>
                                )}
                              </button>
                            </div>
                            <pre className="max-h-36 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-bg p-2.5 font-mono text-[11.5px] leading-5 text-fg">
                              {activeSql}
                            </pre>
                          </>
                        ) : (
                          <div className="rounded-md border border-border/70 bg-bg p-3 text-[12px] text-muted">
                            Select a governed measure or source table to inspect its SQL definition.
                          </div>
                        )}
                      </div>
                    </div>

                    {/* Docked Action Row */}
                    <div className="flex flex-wrap items-center gap-2 border-t border-border/70 pt-3">
                      {activeInspector.tile && (
                        <button
                          type="button"
                          onClick={() => onToggleCanvas(activeInspector.tile!)}
                          data-testid="inspector-pin-btn"
                          className={cn(
                            "inline-flex h-7 items-center gap-1.5 rounded-md border px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
                            canvasIdSet.has(activeInspector.tile.id)
                              ? "border-border-strong bg-surface-2 text-fg"
                              : "border-border bg-bg text-fg hover:border-border-strong hover:bg-hover",
                          )}
                        >
                          {canvasIdSet.has(activeInspector.tile.id) ? (
                            <>
                              <Check className="size-3.5 text-ok" strokeWidth={2.2} aria-hidden />
                              On canvas
                            </>
                          ) : (
                            <>
                              <Plus className="size-3.5" strokeWidth={2.2} aria-hidden />
                              Pin to canvas
                            </>
                          )}
                        </button>
                      )}
                      {activeInspector.kind === "measure" && (
                        <button
                          type="button"
                          onClick={() => setTriggerMeasureName(activeInspector.title)}
                          data-testid="inspector-trigger-btn"
                          className="inline-flex h-7 items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 text-[12px] font-medium text-fg transition-colors hover:border-border-strong hover:bg-hover cursor-pointer"
                        >
                          <Bell className="size-3.5" strokeWidth={1.9} aria-hidden />
                          Set trigger
                        </button>
                      )}
                      {activeInspector.askQuestion && (
                        <Link
                          prefetch={false}
                          href={`/ask?q=${encodeURIComponent(activeInspector.askQuestion)}`}
                          data-testid="inspector-ask-btn"
                          className="inline-flex h-7 items-center gap-1.5 rounded-md bg-fg px-3 text-[12px] font-medium text-bg transition-opacity hover:opacity-90"
                        >
                          Ask about this
                          <ArrowUpRight className="size-3.5" strokeWidth={2} aria-hidden />
                        </Link>
                      )}
                      <Link
                        prefetch={false}
                        href={activeInspector.recordsHref}
                        data-testid="inspector-explore-data-btn"
                        className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-bg px-2.5 text-[12px] font-medium text-muted transition-colors hover:border-border-strong hover:bg-hover hover:text-fg"
                      >
                        Explore data
                        <ExternalLink className="size-3" strokeWidth={2} aria-hidden />
                      </Link>
                      {activeInspector.tile && (
                        <button
                          type="button"
                          onClick={() => onJumpToTile(activeInspector.tile!.id)}
                          className="inline-flex h-7 items-center rounded-md px-2 text-[12px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg cursor-pointer"
                        >
                          View tile
                        </button>
                      )}
                    </div>
                  </div>
                )}
              </aside>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
