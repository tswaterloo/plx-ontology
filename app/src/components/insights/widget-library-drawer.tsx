"use client";

import { BarChart3, Check, LineChart, Plus, RotateCcw, Search, Table2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { cn } from "@/lib/cn";
import type { InsightInfo } from "@/lib/types";

const PAGE_SIZE = 16;

const DOMAIN_ORDER = [
  "Pinned",
  "Finance",
  "Purchasing",
  "Operations",
  "Quality",
  "Sales",
  "Customers",
  "Customer Service",
  "Decisions",
  "Team",
];

function domainOf(item: InsightInfo): string {
  if (item.saved) return "Pinned";
  return item.group.replace(/^Measures:\s*/i, "") || "Other";
}

function chartKindLabel(item: InsightInfo): string {
  const kind = item.chart?.kind as string | undefined;
  if (kind === "line" || kind === "area" || kind === "combo") return "Trend";
  if (kind === "kpi") return "KPI";
  if (kind === "bar" || kind === "stacked_bar") return "Bar";
  if (kind === "table" || !kind) return "Table";
  return kind;
}

function ChartKindIcon({ kind }: { kind?: string | null }) {
  if (kind === "line" || kind === "area" || kind === "combo") {
    return <LineChart className="size-3.5 shrink-0 text-faint" strokeWidth={1.8} aria-hidden />;
  }
  if (kind === "table" || !kind) {
    return <Table2 className="size-3.5 shrink-0 text-faint" strokeWidth={1.8} aria-hidden />;
  }
  return <BarChart3 className="size-3.5 shrink-0 text-faint" strokeWidth={1.8} aria-hidden />;
}

/**
 * Slide-over Liquid Glass Widget Library drawer for the Dashboard Canvas.
 * Lets users browse by domain, search, and toggle individual widgets onto their canvas
 * cleanly in paginated groups instead of dumping dozens of cards onto the main page.
 */
export function WidgetLibraryDrawer({
  open,
  onClose,
  allItems,
  canvasIdSet,
  onToggleCanvas,
  onResetCurated,
}: {
  open: boolean;
  onClose: () => void;
  allItems: InsightInfo[];
  canvasIdSet: Set<string>;
  onToggleCanvas: (item: InsightInfo) => void;
  onResetCurated: () => void;
}) {
  const [query, setQuery] = useState("");
  const [domain, setDomain] = useState("All");
  const [visibleLimit, setVisibleLimit] = useState(PAGE_SIZE);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  useEffect(() => {
    setVisibleLimit(PAGE_SIZE);
  }, [domain, query, open]);

  const domains = useMemo(() => {
    const counts = new Map<string, number>();
    for (const it of allItems) {
      const d = domainOf(it);
      counts.set(d, (counts.get(d) ?? 0) + 1);
    }
    const sorted = [...counts.keys()].sort((a, b) => {
      const ia = DOMAIN_ORDER.indexOf(a);
      const ib = DOMAIN_ORDER.indexOf(b);
      if (ia >= 0 && ib >= 0) return ia - ib;
      if (ia >= 0) return -1;
      if (ib >= 0) return 1;
      return a.localeCompare(b);
    });
    return [
      { name: "All", count: allItems.length },
      ...sorted.map((name) => ({ name, count: counts.get(name) ?? 0 })),
    ];
  }, [allItems]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return allItems.filter((it) => {
      const d = domainOf(it);
      if (domain !== "All" && d !== domain) return false;
      if (!q) return true;
      return `${it.title} ${it.about} ${it.group} ${it.measure?.name ?? ""} ${it.measure?.owner ?? ""}`
        .toLowerCase()
        .includes(q);
    });
  }, [allItems, domain, query]);

  const paged = useMemo(() => filtered.slice(0, visibleLimit), [filtered, visibleLimit]);
  const remaining = Math.max(0, filtered.length - paged.length);

  const grouped = useMemo(() => {
    const map = new Map<string, InsightInfo[]>();
    for (const it of paged) {
      const d = domainOf(it);
      const list = map.get(d);
      if (list) list.push(it);
      else map.set(d, [it]);
    }
    return [...map.entries()];
  }, [paged]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-label="Widget library"
      data-testid="widget-library-drawer"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="liquid-glass-surface flex h-full w-full max-w-[468px] flex-col border-l border-border-strong shadow-2xl">
        {/* Drawer Header */}
        <div className="flex items-center justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h2 className="text-[15px] font-semibold tracking-[-0.015em] text-fg">
              Widget library
            </h2>
            <p className="text-[12px] text-muted tabular-nums">
              {canvasIdSet.size} active on canvas · {allItems.length} available
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close widget library"
            className="inline-flex size-8 items-center justify-center rounded-md text-muted hover:bg-hover hover:text-fg cursor-pointer"
          >
            <X className="size-4" strokeWidth={2} aria-hidden />
          </button>
        </div>

        {/* Search + Domain Filter */}
        <div className="space-y-2.5 border-b border-border px-5 py-3">
          <label className="flex h-9 items-center gap-2 rounded-lg border border-border bg-bg/80 px-3 text-[13px] focus-within:border-accent">
            <Search className="size-3.5 shrink-0 text-faint" strokeWidth={2} aria-hidden />
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search charts, KPIs, or governed measures"
              aria-label="Search widget library"
              autoFocus
              className="min-w-0 flex-1 bg-transparent text-fg outline-none focus-visible:outline-none placeholder:text-faint"
            />
          </label>
          <div className="flex items-center gap-1 overflow-x-auto pb-0.5" role="group" aria-label="Filter widgets by domain">
            {domains.map((d) => (
              <button
                key={d.name}
                type="button"
                aria-pressed={domain === d.name}
                onClick={() => setDomain(d.name)}
                className={cn(
                  "inline-flex h-6 shrink-0 items-center gap-1 rounded-md px-2 text-[11.5px] font-medium whitespace-nowrap transition-colors cursor-pointer",
                  domain === d.name
                    ? "bg-fg text-bg"
                    : "border border-border/70 bg-bg/60 text-muted hover:border-border-strong hover:text-fg",
                )}
              >
                <span>{d.name}</span>
                <span className={cn("num text-[10.5px]", domain === d.name ? "text-bg/75" : "text-faint")}>
                  {d.count}
                </span>
              </button>
            ))}
          </div>
        </div>

        {/* Categorized Paginated Widget List */}
        <div className="flex-1 overflow-y-auto px-4 py-3">
          {filtered.length === 0 ? (
            <p className="px-3 py-8 text-center text-[13px] text-muted">
              No widgets match this search.
            </p>
          ) : (
            <div className="space-y-4">
              {grouped.map(([groupName, items]) => (
                <div key={groupName} className="space-y-1.5">
                  <div className="flex items-center justify-between px-1.5 text-[11px] font-semibold uppercase tracking-[0.04em] text-faint">
                    <span>{groupName}</span>
                    <span className="num">{items.length}</span>
                  </div>
                  <ul className="divide-y divide-border/60 rounded-lg border border-border/70 bg-bg/65">
                    {items.map((it) => {
                      const active = canvasIdSet.has(it.id);
                      return (
                        <li
                          key={it.id}
                          className="flex items-start justify-between gap-3 px-3 py-2.5 transition-colors hover:bg-hover/60"
                          data-testid={`library-item-${it.id}`}
                        >
                          <div className="min-w-0 flex-1 space-y-0.5">
                            <div className="flex items-center gap-1.5">
                              <ChartKindIcon kind={it.chart?.kind} />
                              <span className="truncate text-[13px] font-medium text-fg">
                                {it.title}
                              </span>
                            </div>
                            <p className="text-[11.5px] text-muted">
                              {chartKindLabel(it)}
                              {it.measure ? ` · ${it.measure.owner} ${it.measure.version}` : ""}
                            </p>
                          </div>
                          <button
                            type="button"
                            onClick={() => onToggleCanvas(it)}
                            aria-pressed={active}
                            data-testid={`library-toggle-${it.id}`}
                            className={cn(
                              "inline-flex h-7 shrink-0 items-center gap-1 rounded-md px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
                              active
                                ? "border border-ok-line bg-ok-soft text-ok hover:opacity-90"
                                : "border border-border bg-surface text-fg hover:border-border-strong hover:bg-row-on",
                            )}
                          >
                            {active ? (
                              <>
                                <Check className="size-3 shrink-0" strokeWidth={2.2} aria-hidden />
                                Added
                              </>
                            ) : (
                              <>
                                <Plus className="size-3 shrink-0" strokeWidth={2.2} aria-hidden />
                                Add
                              </>
                            )}
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              ))}

              {remaining > 0 && (
                <div className="flex items-center justify-between gap-2 pt-1">
                  <span className="text-[11.5px] text-muted tabular-nums">
                    Showing {paged.length} of {filtered.length}
                  </span>
                  <button
                    type="button"
                    onClick={() => setVisibleLimit((n) => n + PAGE_SIZE)}
                    data-testid="library-show-more"
                    className="h-7 rounded-md border border-border bg-bg/80 px-2.5 text-[12px] font-medium text-fg hover:border-border-strong hover:bg-hover cursor-pointer"
                  >
                    Show {Math.min(PAGE_SIZE, remaining)} more
                  </button>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Footer Actions (no "Show all 76" nuke button) */}
        <div className="flex items-center justify-between gap-2 border-t border-border px-5 py-3">
          <button
            type="button"
            onClick={onResetCurated}
            data-testid="library-reset-curated"
            className="inline-flex h-7 items-center gap-1.5 rounded-md border border-border bg-bg/75 px-2.5 text-[12px] font-medium text-muted hover:border-border-strong hover:bg-hover hover:text-fg cursor-pointer"
          >
            <RotateCcw className="size-3 shrink-0" strokeWidth={2} aria-hidden />
            Reset to curated 6
          </button>
          <button
            type="button"
            onClick={onClose}
            className="h-7 rounded-md bg-fg px-3.5 text-[12px] font-medium text-bg hover:opacity-90 cursor-pointer"
          >
            Done
          </button>
        </div>
      </div>
    </div>
  );
}
