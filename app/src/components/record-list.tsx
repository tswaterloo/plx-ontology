"use client";

import { ArrowDown, ArrowUp, ChevronDown, Search, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { StoredAge } from "@/components/ui/stored-age";
import { getRecordList, recordListPath } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { safeHref } from "@/lib/format";
import { useRead } from "@/lib/use-read";
import type { BizCell, RecordList as List } from "@/lib/types";
import { Cell } from "./biz-page";

const PAGE = 25;
const group = (n: number) => n.toLocaleString("en-CA");

/** A column holding amounts, dates or counts reads best newest or largest first. */
function startsDescending(rows: BizCell[][], i: number): boolean {
  return rows.some((r) => r[i]?.money || r[i]?.day || r[i]?.number);
}

/** The cell of a row that says nothing (no value on record) reads as an em-dash, never dot spam. */
function isEmpty(c: BizCell | undefined): boolean {
  if (!c) return true;
  if (c.hidden || c.number != null || c.day || c.money) return false;
  const t = c.text?.trim() ?? "";
  return t === "" || t === "·" || t === "-" || t === "–";
}

const NON_LATE_VERDICTS = new Set([
  "nothing_outstanding",
  "nothing outstanding",
  "stale",
  "probably_closed",
  "probably closed",
  "late (not counted)",
  "neither",
  "closed",
  "cancelled",
  "canceled",
  "received",
  "completed",
  "on_time",
  "on time",
]);

/**
 * Plain-English verdict display with calm monochrome/restrained typography (ZERO noisy colored boxes).
 * Explicitly ensures `nothing_outstanding`, `stale`, and `late (not counted)` are NEVER presented as late.
 */
export function formatVerdict(raw: string): { label: string; title?: string; tone?: string; dot?: string } | null {
  const key = raw.trim().toLowerCase();
  if (!key || key === "neither" || key === "none" || key === "n/a") {
    return { label: "—", tone: "text-faint" };
  }
  if (key === "nothing_outstanding" || key === "nothing outstanding") {
    return {
      label: "Closed",
      title: "Nothing outstanding on this order",
      tone: "text-muted",
    };
  }
  if (key === "stale" || key === "probably_closed" || key === "probably closed") {
    return {
      label: "Stale",
      title: "No recent activity; excluded from active late orders",
      tone: "text-faint",
    };
  }
  if (key === "late (not counted)") {
    return {
      label: "Late (excluded)",
      title: "Excluded from the active late count (e.g. non-counted or pilot record)",
      tone: "text-faint",
    };
  }
  if (key === "late") {
    return { label: "Late", tone: "font-medium text-late", dot: "bg-dot-late" };
  }
  if (key === "on_time" || key === "on time") {
    return { label: "On time", tone: "text-muted", dot: "bg-dot-ok" };
  }
  if (key === "at_risk" || key === "at risk") {
    return { label: "At risk", tone: "font-medium text-risk", dot: "bg-dot-risk" };
  }
  if (key === "unknown") {
    return { label: "Unknown", tone: "text-faint" };
  }
  return null;
}

/** Calm monochrome formatting for status columns (open, closed, cancelled, etc.) with zero colored boxes. */
function statusTone(raw: string): string {
  const key = raw.trim().toLowerCase();
  if (key === "open" || key === "released" || key === "active") return "text-fg font-medium";
  if (key === "closed" || key === "completed" || key === "received" || key === "invoiced" || key === "paid") {
    return "text-muted";
  }
  if (key === "cancelled" || key === "canceled" || key === "void" || key === "draft" || key === "stale") {
    return "text-faint";
  }
  return "text-muted";
}

/** Capitalize raw lowercase status values (e.g. "open" -> "Open", "closed" -> "Closed", "cancelled" -> "Cancelled"). */
export function formatStatusLabel(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return "";
  const normalized = trimmed.replace(/_/g, " ");
  if (normalized !== normalized.toLowerCase()) return normalized;
  return normalized.charAt(0).toUpperCase() + normalized.slice(1);
}

/** True only when a row is genuinely actively late (never true for nothing_outstanding, stale, or excluded rows). */
export function isRowActivelyLate(row: BizCell[], columnKeys: string[]): boolean {
  const vIdx = columnKeys.indexOf("verdict");
  const sIdx = columnKeys.indexOf("status");
  const statusVal = (sIdx >= 0 ? row[sIdx]?.text ?? "" : "").trim().toLowerCase();
  if (["closed", "cancelled", "canceled", "completed", "received", "void"].includes(statusVal)) {
    return false;
  }
  if (vIdx >= 0) {
    const v = (row[vIdx]?.text ?? "").trim().toLowerCase();
    if (!v || NON_LATE_VERDICTS.has(v)) return false;
    return v === "late";
  }
  return false;
}

const UNIT_RATE_COL = /(?:^|_)(?:unit_price|unit_cost|price_per|cost_per|rate)(?:$|_)/i;

type QuickFilterMode = "all" | "open" | "late";
type ListReadOptions = { filters?: Record<string, string>; q?: string; sort?: string; direction?: string; offset?: number; facets?: boolean };

type SavedListState = {
  filters?: Record<string, string>;
  text?: string;
  sort?: { key: string; direction: "asc" | "desc" } | null;
  page?: number;
};

function listStateKey(view: string): string {
  const path = typeof window !== "undefined" ? window.location.pathname : "";
  return `plx.rl:${path}:${view}`;
}

function consumeListState(view: string): SavedListState | null {
  try {
    if (typeof window === "undefined") return null;
    const key = listStateKey(view);
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return null;
    window.sessionStorage.removeItem(key);
    const parsed = JSON.parse(raw) as SavedListState;
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

export function RecordList({
  view,
  initialSort,
  externalQuery,
  onClearExternalQuery,
}: {
  view: string;
  initialSort?: { key: string; direction: "asc" | "desc" };
  externalQuery?: string;
  onClearExternalQuery?: () => void;
}) {
  const router = useRouter();
  const restored = useRef<SavedListState | null | undefined>(undefined);
  if (restored.current === undefined) restored.current = consumeListState(view);
  const [filters, setFilters] = useState<Record<string, string>>(() => restored.current?.filters ?? {});
  const [text, setText] = useState(() => restored.current?.text ?? "");
  const controlledSearch = externalQuery !== undefined;
  const activeText = controlledSearch ? externalQuery : text;
  const [q, setQ] = useState(() => (controlledSearch ? externalQuery.trim() : (restored.current?.text ?? "").trim()));
  const [sort, setSort] = useState<{ key: string; direction: "asc" | "desc" } | null>(() =>
    restored.current && "sort" in restored.current ? (restored.current.sort ?? null) : (initialSort ?? null),
  );
  const [page, setPage] = useState(() => restored.current?.page ?? 0);
  const [offered, setOffered] = useState<List["filters"]>([]);
  const [clientQuickMode, setClientQuickMode] = useState<QuickFilterMode>("all");

  const persistBeforeLeave = () => {
    try {
      if (typeof window === "undefined") return;
      const hasCustomSort = (sort?.key ?? null) !== (initialSort?.key ?? null) || (sort?.direction ?? null) !== (initialSort?.direction ?? null);
      const hasFilters = Object.values(filters).some(Boolean);
      const hasText = !controlledSearch && text.trim() !== "";
      if (page === 0 && !hasFilters && !hasText && !hasCustomSort) return;
      const payload: SavedListState = { filters, text: controlledSearch ? "" : text, sort, page };
      window.sessionStorage.setItem(listStateKey(view), JSON.stringify(payload));
    } catch {
      // sessionStorage unavailable
    }
  };

  useEffect(() => {
    const t = window.setTimeout(() => {
      const next = activeText.trim();
      setQ((was) => {
        if (was !== next) setPage(0);
        return next;
      });
    }, 250);
    return () => window.clearTimeout(t);
  }, [activeText]);

  const narrow = Object.values(filters).some(Boolean) || q !== "";
  const initialRead = page === 0 && !narrow && (sort?.key ?? null) === (initialSort?.key ?? null) && (sort?.direction ?? null) === (initialSort?.direction ?? null);
  const options: ListReadOptions = { filters, q, sort: sort?.key, direction: sort?.direction, offset: page * PAGE, facets: initialRead };
  const path = recordListPath(view, options);
  const { data: got, error: problem, retry, at } = useRead<List>(path, (signal) => getRecordList(view, options, signal));

  useEffect(() => {
    if (got?.view === view && Array.isArray(got.filters) && got.filters.length) setOffered(got.filters);
  }, [got, view]);

  const shown = got && got.view === view ? got : null;
  const rawRows = useMemo(() => shown?.table?.rows ?? [], [shown]);
  const choices = offered.length ? offered : (shown?.filters ?? []);

  // Quick filter capabilities: detect server-side `status` ("Open") and `verdict` ("late") choices
  const columnKeys = shown?.column_keys ?? [];
  const statusFacet = choices.find((f) => f.key === "status");
  const verdictFacet = choices.find((f) => f.key === "verdict");
  const openServerOption = (statusFacet?.options ?? []).find((o) => (o.value ?? "").trim().toLowerCase() === "open");
  // Strictly exact "late" (NEVER "late (not counted)", "nothing_outstanding", or "stale")
  const lateServerOption = (verdictFacet?.options ?? []).find((o) => (o.value ?? "").trim().toLowerCase() === "late");
  const hasQuickFilter = Boolean(openServerOption || lateServerOption || columnKeys.includes("verdict") || columnKeys.includes("status"));

  const activeQuickMode: QuickFilterMode = useMemo(() => {
    if (lateServerOption && filters.verdict === lateServerOption.value) return "late";
    if (openServerOption && filters.status === openServerOption.value && !filters.verdict) return "open";
    return clientQuickMode;
  }, [lateServerOption, openServerOption, filters.verdict, filters.status, clientQuickMode]);

  const applyQuickFilter = (mode: QuickFilterMode) => {
    setPage(0);
    if (mode === "all") {
      setClientQuickMode("all");
      setFilters((cur) => {
        const next = { ...cur };
        delete next.status;
        delete next.verdict;
        return next;
      });
      return;
    }
    if (mode === "open") {
      if (openServerOption) {
        setClientQuickMode("all");
        setFilters((cur) => {
          const next: Record<string, string> = { ...cur, status: openServerOption.value };
          delete next.verdict;
          return next;
        });
      } else {
        setClientQuickMode("open");
      }
      return;
    }
    if (mode === "late") {
      if (lateServerOption) {
        setClientQuickMode("all");
        setFilters((cur) => {
          const next: Record<string, string> = { ...cur, verdict: lateServerOption.value };
          delete next.status;
          return next;
        });
      } else {
        setClientQuickMode("late");
      }
    }
  };

  // Client-side fallback filter when a view lacks server facets for Open / Late
  const rows = useMemo(() => {
    if (!shown) return rawRows;
    if (clientQuickMode === "all") return rawRows;
    const keys = shown.column_keys;
    if (clientQuickMode === "late") {
      return rawRows.filter((r) => isRowActivelyLate(r, keys));
    }
    // clientQuickMode === "open"
    const sIdx = keys.indexOf("status");
    const vIdx = keys.indexOf("verdict");
    return rawRows.filter((r) => {
      const s = (sIdx >= 0 ? r[sIdx]?.text ?? "" : "").trim().toLowerCase();
      const v = (vIdx >= 0 ? r[vIdx]?.text ?? "" : "").trim().toLowerCase();
      if (["closed", "cancelled", "canceled", "completed", "received", "void"].includes(s)) return false;
      if (v === "nothing_outstanding" || v === "nothing outstanding" || v === "stale" || v === "probably_closed") return false;
      return s === "" || s === "open" || s === "released" || s === "active";
    });
  }, [shown, rawRows, clientQuickMode]);

  const busy = !shown || at !== null;
  if (problem) return <LoadProblem message={plainError(problem)} onRetry={retry} />;
  if (!shown) return <Pending label="Reading the list" />;

  const first = shown.offset + 1;
  const last = shown.offset + rows.length;
  const totalPages = Math.max(1, Math.ceil(shown.total / PAGE));
  const active = sort?.key ?? shown.sort;
  const direction = sort?.direction ?? shown.direction;
  const narrowed = q !== "" || Object.values(filters).some(Boolean) || clientQuickMode !== "all";
  const clearAll = () => {
    setPage(0);
    setText("");
    setFilters({});
    setClientQuickMode("all");
    onClearExternalQuery?.();
  };
  const choose = (i: number) => {
    const k = shown.column_keys[i];
    setPage(0);
    setSort(active === k ? { key: k, direction: direction === "asc" ? "desc" : "asc" } : { key: k, direction: startsDescending(rows, i) ? "desc" : "asc" });
  };

  const verdictColIdx = columnKeys.indexOf("verdict");
  const statusColIdx = columnKeys.indexOf("status");

  return (
    <section aria-label={shown.title} data-testid={`record-list-${view}`} aria-busy={busy} className="space-y-3">
      <StoredAge at={at} />

      {/* Compact Operational Filter Bar — every control aligned on one crisp horizontal baseline */}
      <div className="flex flex-wrap items-center justify-between gap-3 text-[12.5px]">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          {hasQuickFilter && (
            <div
              role="group"
              aria-label="Quick filter"
              data-testid="record-quick-filter"
              className="inline-flex h-8 items-center rounded-md border border-border bg-bg p-0.5"
            >
              {(["all", "open", "late"] as const).map((mode) => {
                const label = mode === "all" ? "All" : mode === "open" ? "Open" : "Late";
                const isActive = activeQuickMode === mode;
                return (
                  <button
                    key={mode}
                    type="button"
                    aria-pressed={isActive}
                    onClick={() => applyQuickFilter(mode)}
                    data-testid={`quick-filter-${mode}`}
                    className={cn(
                      "h-6.5 rounded px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
                      isActive ? "bg-surface-2 text-fg" : "text-muted hover:text-fg",
                    )}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
          )}

          {shown.searchable && !controlledSearch && (
            <label className="inline-flex items-center gap-1.5 text-[12px] font-medium text-muted">
              <span className="sr-only">Search this list</span>
              <span className="relative inline-block w-56 max-w-full">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-faint" aria-hidden />
                <input
                  type="search"
                  placeholder="Search this list"
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape" && text) {
                      e.preventDefault();
                      setText("");
                    }
                  }}
                  maxLength={80}
                  autoComplete="off"
                  spellCheck={false}
                  className="h-8 w-full rounded-md border border-border bg-surface pl-8 pr-7 text-[12.5px] font-normal text-fg placeholder:text-faint outline-none transition-colors hover:border-border-strong focus-visible:border-border-strong"
                />
                {text && (
                  <button
                    type="button"
                    onClick={() => setText("")}
                    aria-label="Clear search"
                    className="absolute right-1.5 top-1/2 inline-flex size-5 -translate-y-1/2 cursor-pointer items-center justify-center rounded text-muted transition-colors hover:bg-hover hover:text-fg"
                  >
                    <X className="size-3" aria-hidden />
                  </button>
                )}
              </span>
            </label>
          )}

          {choices.map((f) => (
            <label key={f.key} className="inline-flex items-center gap-1.5 text-[12px] font-medium text-muted">
              <span>{f.label}</span>
              <span className="relative inline-block">
                <select
                  aria-label={f.label}
                  className="h-8 appearance-none cursor-pointer rounded-md border border-border bg-surface pl-2.5 pr-7 text-[12.5px] font-normal text-fg outline-none transition-colors hover:border-border-strong focus-visible:border-border-strong"
                  value={filters[f.key] ?? ""}
                  onChange={(e) => {
                    setPage(0);
                    setClientQuickMode("all");
                    setFilters((cur) => ({ ...cur, [f.key]: e.target.value }));
                  }}
                >
                  {(f.options ?? []).map((o) => {
                    const optLabel =
                      f.key === "verdict" && o.value
                        ? (formatVerdict(o.value)?.label ?? o.label)
                        : f.key === "status" && o.value
                          ? formatStatusLabel(o.label)
                          : o.label;
                    return (
                      <option key={o.value} value={o.value} className="bg-raised text-fg">
                        {optLabel}
                        {o.count != null ? ` (${group(o.count)})` : ""}
                      </option>
                    );
                  })}
                </select>
                <ChevronDown className="pointer-events-none absolute right-2 top-1/2 size-3.5 -translate-y-1/2 text-muted" aria-hidden />
              </span>
            </label>
          ))}

          {narrowed && (
            <button
              type="button"
              onClick={clearAll}
              className="h-8 cursor-pointer rounded-md px-2 text-[12.5px] font-medium text-accent transition-colors hover:bg-hover hover:underline"
            >
              Clear
            </button>
          )}
        </div>

        <p className="text-[12px] tabular-nums text-faint" data-testid="record-list-total">
          {shown.total === 0
            ? "Nothing to show"
            : clientQuickMode !== "all"
              ? `${group(rows.length)} matching on this page (${group(shown.total)} total)`
              : `${group(first)} to ${group(last)} of ${group(shown.total)} ${shown.total === 1 ? "record" : "records"}`}
        </p>
      </div>

      {/* Linear-Style Operational Data Grid */}
      {rows.length === 0 ? (
        <div className="flex flex-wrap items-center gap-3 border-t border-border/80 py-6">
          <p className="text-[13px] text-muted" data-testid="record-list-empty">
            {narrowed ? "No record matches these filters." : "Nothing on record here that you can see."}
          </p>
          {narrowed && (
            <button
              type="button"
              onClick={clearAll}
              className="cursor-pointer rounded-md border border-border bg-bg px-2.5 py-1 text-[12px] font-medium text-fg transition-colors hover:border-border-strong hover:bg-hover"
            >
              Reset filters
            </button>
          )}
        </div>
      ) : (
        <div className="overflow-x-auto border-y border-border/80">
          <table className="w-full border-collapse text-[12.5px]">
            <thead className="sticky top-0 z-[1] bg-bg">
              <tr className="border-b border-border text-left text-[11.5px] text-faint">
                {shown.table.columns.map((c, i) => {
                  const on = columnKeys[i] === active;
                  const numeric = rows.some((r) => r[i]?.money || r[i]?.number);
                  return (
                    <th
                      key={`${columnKeys[i] ?? c}-${i}`}
                      scope="col"
                      aria-sort={on ? (direction === "asc" ? "ascending" : "descending") : "none"}
                      className={cn("py-2 pr-4 last:pr-0 font-medium whitespace-nowrap", numeric && "text-right")}
                    >
                      <button
                        type="button"
                        onClick={() => choose(i)}
                        className={cn(
                          "inline-flex cursor-pointer items-center gap-1 whitespace-nowrap rounded px-0.5 transition-colors hover:text-fg",
                          on && "text-fg",
                        )}
                      >
                        {c}
                        {on && (direction === "asc" ? <ArrowUp className="size-3" aria-hidden /> : <ArrowDown className="size-3" aria-hidden />)}
                      </button>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody className="divide-y divide-border/50">
              {rows.map((r, j) => {
                const open = safeHref(r.find((c) => c.href)?.href);
                const rowVerdictRaw = verdictColIdx >= 0 ? (r[verdictColIdx]?.text ?? "").trim().toLowerCase() : "";
                const rowStatusRaw = statusColIdx >= 0 ? (r[statusColIdx]?.text ?? "").trim().toLowerCase() : "";
                const rowIsNonLate =
                  NON_LATE_VERDICTS.has(rowVerdictRaw) ||
                  ["closed", "cancelled", "canceled", "completed", "received", "void"].includes(rowStatusRaw);

                return (
                  <tr
                    key={`${shown.offset}-${j}`}
                    className={cn("transition-colors", open && "cursor-pointer hover:bg-hover/70")}
                    onClick={(e) => {
                      if (!open) return;
                      persistBeforeLeave();
                      if (!(e.target as HTMLElement).closest("a,button")) router.push(open);
                    }}
                  >
                    {r.map((c, i) => {
                      const colKey = shown.column_keys[i] ?? "";
                      const numeric = rows.some((x) => x[i]?.money || x[i]?.number);
                      const verdict = colKey === "verdict" && c.text && !c.hidden && !c.href ? formatVerdict(c.text) : null;
                      const isStatus = colKey === "status" && c.text && !c.hidden && !c.href;
                      const isDaysLate = colKey === "days_late";

                      // Suppress noisy/stale days_late values on closed/nothing_outstanding/stale orders
                      const mutedZeroDays =
                        isDaysLate &&
                        !c.hidden &&
                        (rowIsNonLate || String(c.number ?? c.text ?? "").trim() === "0");

                      return (
                        <td
                          key={i}
                          className={cn("py-2 pr-4 last:pr-0 align-middle whitespace-nowrap tabular-nums", numeric && "text-right")}
                        >
                          {isEmpty(c) || mutedZeroDays ? (
                            <span className="text-faint" aria-label="none">—</span>
                          ) : verdict ? (
                            <span
                              className={cn("inline-flex max-w-[22ch] items-center gap-1.5 truncate", verdict.tone)}
                              title={verdict.title ?? c.text ?? undefined}
                            >
                              {verdict.dot && <span className={cn("size-1.5 shrink-0 rounded-full", verdict.dot)} aria-hidden />}
                              <span className="truncate">{verdict.label}</span>
                            </span>
                          ) : isStatus ? (
                            <span
                              className={cn("block max-w-[20ch] truncate", statusTone(c.text ?? ""))}
                              title={formatStatusLabel(c.text ?? "") || undefined}
                            >
                              {formatStatusLabel(c.text ?? "")}
                            </span>
                          ) : (
                            <span className="block max-w-[24ch] truncate" title={c.text ?? undefined}>
                              <Cell c={c} numeric={numeric} fixedCents={!UNIT_RATE_COL.test(colKey)} />
                            </span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {(page > 0 || shown.more) && (
        <nav aria-label="Pages" className="flex items-center gap-2 text-[12px]">
          <button
            type="button"
            disabled={busy || page === 0}
            onClick={() => setPage((n) => n - 1)}
            className="inline-flex h-7 cursor-pointer items-center rounded-md border border-border bg-bg px-2.5 font-medium text-fg transition-colors hover:border-border-strong hover:bg-hover disabled:cursor-not-allowed disabled:opacity-45"
          >
            Previous
          </button>
          <span className="px-1.5 tabular-nums text-muted">
            Page {page + 1} of {totalPages}
          </span>
          <button
            type="button"
            disabled={busy || !shown.more}
            onClick={() => setPage((n) => n + 1)}
            className="inline-flex h-7 cursor-pointer items-center rounded-md border border-border bg-bg px-2.5 font-medium text-fg transition-colors hover:border-border-strong hover:bg-hover disabled:cursor-not-allowed disabled:opacity-45"
          >
            Next
          </button>
        </nav>
      )}
    </section>
  );
}
