"use client";

import { ArrowDown, ArrowUp } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
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

/** The cell of a row that says nothing (no value on record) reads as a dash, never a blank. */
function isEmpty(c: BizCell | undefined): boolean {
  return !c || (!c.hidden && !c.text && !c.number && !c.day && !c.money);
}

/** Plain-English verdict display so internal tags ("neither", "late (not counted)") never confuse readers. */
export function formatVerdict(raw: string): { label: string; title?: string; tone?: string } | null {
  const key = raw.trim().toLowerCase();
  if (key === "neither") return { label: "–", tone: "text-faint" };
  if (key === "late (not counted)") {
    return {
      label: "Late (excluded)",
      title: "Excluded from the late PO count (e.g. non-counted or pilot record)",
      tone: "text-muted",
    };
  }
  if (key === "late") return { label: "Late", tone: "font-medium text-late" };
  if (key === "on_time" || key === "on time") return { label: "On time", tone: "text-ok" };
  if (key === "at_risk" || key === "at risk") return { label: "At risk", tone: "font-medium text-risk" };
  if (key === "unknown") return { label: "Unknown", tone: "text-muted" };
  return null;
}

const UNIT_RATE_COL = /(?:^|_)(?:unit_price|unit_cost|price_per|cost_per|rate)(?:$|_)/i;

type ListReadOptions = { filters?: Record<string, string>; q?: string; sort?: string; direction?: string; offset?: number; facets?: boolean };

/**
 * One Records list of a business view, from the metadata the server sends with it: its
 * columns (money with its currency, dates, counts), the filters the data offers (each
 * choice with how many it holds), text search, sort by any column, and pages of 25 with a
 * link on every row to the record. Changing a filter, the text or the sort starts again
 * from the first row (a parent keys the list by its view, so another view starts clean); a failed read says so with Retry, never an empty list.
 * A list already read in this session (or kept by the desktop shell) is drawn at once with its
 * age while it is read again.
 */
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
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [text, setText] = useState("");
  const controlledSearch = externalQuery !== undefined;
  const activeText = controlledSearch ? externalQuery : text;
  const [q, setQ] = useState(controlledSearch ? externalQuery.trim() : "");
  const [sort, setSort] = useState<{ key: string; direction: "asc" | "desc" } | null>(initialSort ?? null);
  const [page, setPage] = useState(0);
  const [offered, setOffered] = useState<List["filters"]>([]);

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
    if (got?.view === view && got.filters.length) setOffered(got.filters);
  }, [got, view]);

  const shown = got && got.view === view ? got : null;
  const rows = useMemo(() => shown?.table.rows ?? [], [shown]);
  const choices = offered.length ? offered : (shown?.filters ?? []);
  const busy = !shown || at !== null;
  if (problem) return <LoadProblem message={plainError(problem)} onRetry={retry} />;
  if (!shown) return <Pending label="Reading the list" />;

  const first = shown.offset + 1;
  const last = shown.offset + rows.length;
  const active = sort?.key ?? shown.sort;
  const direction = sort?.direction ?? shown.direction;
  const narrowed = q !== "" || Object.values(filters).some(Boolean);
  const choose = (i: number) => {
    const k = shown.column_keys[i];
    setPage(0);
    setSort(active === k ? { key: k, direction: direction === "asc" ? "desc" : "asc" } : { key: k, direction: startsDescending(rows, i) ? "desc" : "asc" });
  };

  return (
    <section aria-label={shown.title} data-testid={`record-list-${view}`} aria-busy={busy}>
      <StoredAge at={at} />
      <div className="flex flex-wrap items-end gap-x-4 gap-y-2 text-[13px]">
        {shown.searchable && !controlledSearch && (
          <label className="flex flex-col gap-1 text-[12px] text-muted">
            Search this list
            <input
              type="search"
              value={text}
              onChange={(e) => setText(e.target.value)}
              maxLength={80}
              autoComplete="off"
              spellCheck={false}
              className="h-8 w-56 rounded-md border border-border bg-bg px-2 text-[13px] text-fg outline-none focus-visible:border-accent"
            />
          </label>
        )}
        {choices.map((f) => (
          <label key={f.key} className="flex flex-col gap-1 text-[12px] text-muted">
            {f.label}
            <select
              aria-label={f.label}
              className="h-8 cursor-pointer rounded-md border border-border bg-bg px-2 text-[13px] text-fg"
              value={filters[f.key] ?? ""}
              onChange={(e) => {
                setPage(0);
                setFilters((cur) => ({ ...cur, [f.key]: e.target.value }));
              }}
            >
              {f.options.map((o) => {
                const vLabel = f.key === "verdict" && o.value ? (formatVerdict(o.value)?.label ?? o.label) : o.label;
                return (
                  <option key={o.value} value={o.value}>
                    {vLabel}
                    {o.count != null ? ` (${group(o.count)})` : ""}
                  </option>
                );
              })}
            </select>
          </label>
        ))}
        {narrowed && (
          <button
            type="button"
            onClick={() => {
              setPage(0);
              setText("");
              setFilters({});
              onClearExternalQuery?.();
            }}
            className="h-8 cursor-pointer text-[13px] text-accent hover:underline"
          >
            Clear
          </button>
        )}
      </div>
      <p className="mt-3 text-[12px] tabular-nums text-muted" data-testid="record-list-total">
        {shown.total === 0 ? "Nothing to show" : `${group(first)} to ${group(last)} of ${group(shown.total)} ${shown.total === 1 ? "record" : "records"}`}
      </p>
      {rows.length === 0 ? (
        <p className="mt-3 text-[13px] text-muted" data-testid="record-list-empty">
          {narrowed ? "No record matches these filters." : "Nothing on record here that you can see."}
        </p>
      ) : (
        <div className="mt-2 overflow-x-auto">
          <table className="w-full border-collapse text-[12.5px]">
            <thead>
              <tr className="border-b border-border text-left text-[12px] text-muted">
                {shown.table.columns.map((c, i) => {
                  const on = shown.column_keys[i] === active;
                  const numeric = rows.some((r) => r[i]?.money || r[i]?.number);
                  return (
                    <th key={c} scope="col" aria-sort={on ? (direction === "asc" ? "ascending" : "descending") : "none"} className={cn("py-1.5 pr-3 last:pr-0 font-normal", numeric && "text-right")}>
                      <button type="button" onClick={() => choose(i)} className={cn("inline-flex cursor-pointer items-center gap-1 whitespace-nowrap hover:text-fg", on && "text-fg")}>
                        {c}
                        {on && (direction === "asc" ? <ArrowUp className="size-3" aria-hidden /> : <ArrowDown className="size-3" aria-hidden />)}
                      </button>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, j) => {
                const open = safeHref(r.find((c) => c.href)?.href);
                return (
                  <tr
                    key={`${shown.offset}-${j}`}
                    className={cn("border-b border-border/60", open && "cursor-pointer hover:bg-hover")}
                    onClick={(e) => {
                      if (open && !(e.target as HTMLElement).closest("a,button")) router.push(open);
                    }}
                  >
                    {r.map((c, i) => {
                      const colKey = shown.column_keys[i] ?? "";
                      const numeric = rows.some((x) => x[i]?.money || x[i]?.number);
                      const verdict = colKey === "verdict" && c.text && !c.hidden && !c.href ? formatVerdict(c.text) : null;
                      return (
                        <td key={i} className={cn("py-1.5 pr-3 last:pr-0 align-top whitespace-nowrap", numeric && "text-right")}>
                          {isEmpty(c) ? (
                            <span className="text-faint" aria-label="none">–</span>
                          ) : verdict ? (
                            <span className={cn("block max-w-[22ch] truncate", verdict.tone)} title={verdict.title ?? c.text ?? undefined}>
                              {verdict.label}
                            </span>
                          ) : (
                            // a long name is cut at the column's width (its full text on hover)
                            <span className="block max-w-[22ch] truncate" title={c.text ?? undefined}>
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
        <nav aria-label="Pages" className="mt-3 flex items-center gap-4 text-[13px]">
          <button type="button" disabled={busy || page === 0} onClick={() => setPage((n) => n - 1)} className="cursor-pointer text-accent hover:underline disabled:cursor-default disabled:text-faint disabled:no-underline">
            Previous
          </button>
          <button type="button" disabled={busy || !shown.more} onClick={() => setPage((n) => n + 1)} className="cursor-pointer text-accent hover:underline disabled:cursor-default disabled:text-faint disabled:no-underline">
            Next
          </button>
        </nav>
      )}
    </section>
  );
}
