"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { getLateList } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDate, formatMoney, objectHref, plural, safeHref } from "@/lib/format";
import { DAY_BANDS, inDayBand, matches, sortLate, supplierCounts, VALUE_BANDS, type DayBand, type LateFilter, type SortKey, type ValueBand } from "@/lib/late";
import type { LatePo } from "@/lib/types";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorNotice } from "@/components/ui/notice";
import { Skeleton } from "@/components/ui/skeleton";

const PAGE = 100;
const MOST = 1000; // the list reads at most this many orders, then says how many it left out

function hrefOf(po: LatePo): string {
  return safeHref(po.open_url) ?? objectHref("PurchaseOrder", po.object_id);
}

function Value({ po }: { po: LatePo }) {
  if (!po.amount) return <span className="text-muted">Not shown</span>;
  const shown = formatMoney(po.amount.amount, po.amount.currency);
  if (!po.amount_suspect) return <>{shown}</>;
  return (
    <span title={`Check the data: ${po.amount_suspect}`} className="text-muted">
      {shown} <span className="text-warn">(check)</span>
    </span>
  );
}

const COLUMNS: { key: SortKey; label: string; right?: boolean }[] = [
  { key: "po", label: "PO" },
  { key: "supplier", label: "Supplier" },
  { key: "due", label: "Needed by" },
  { key: "days", label: "Days late", right: true },
  { key: "value", label: "Value", right: true },
];

/** Whether the sort runs largest or latest first (days late and value do unless flipped). */
function falling(s: { key: SortKey; desc: boolean }): boolean {
  return (s.key === "days" || s.key === "value") !== s.desc;
}

/** Every late purchase order the reader may see: filtered by supplier, days late and value band; sortable. */
export default function LatePage() {
  const [items, setItems] = useState<LatePo[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [asOf, setAsOf] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<LateFilter>({ supplier: null, days: "all", value: "all" });
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "days", desc: false });

  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    setTotal(null);
    (async () => {
      // every page, so filters and sorting cover the whole list, not the first 25
      const all: LatePo[] = [];
      const seen = new Set<string>();
      let offset = 0;
      for (;;) {
        const page = await getLateList(offset, c.signal, PAGE);
        for (const p of page.items ?? []) if (!seen.has(p.object_id)) (seen.add(p.object_id), all.push(p));
        setAsOf(page.as_of ?? null);
        const next = page.next_offset ?? offset + (page.items ?? []).length;
        if (!page.more || next <= offset || all.length >= MOST) {
          setTotal(page.total);
          break;
        }
        offset = next;
      }
      setItems(all);
    })().catch((e) => {
      if (!c.signal.aborted) setError(plainError(e));
    });
    return () => c.abort();
  }, []);
  useEffect(() => load(), [load]);

  const suppliers = useMemo(() => supplierCounts(items), [items]);
  const shown = useMemo(() => sortLate(items.filter((p) => matches(p, filter)), sort.key, sort.desc), [items, filter, sort]);
  const dayCount = (band: DayBand) => items.filter((p) => matches(p, { ...filter, days: "all" }) && inDayBand(p, band)).length;
  const filtered = filter.supplier !== null || filter.days !== "all" || filter.value !== "all";
  const onSort = (key: SortKey) => setSort((s) => (s.key === key ? { key, desc: !s.desc } : { key, desc: false }));

  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-6 sm:px-6">
      <Card data-testid="late-all">
        <CardHeader>
          <CardTitle>Late purchase orders</CardTitle>
          {total !== null && (
            <span className="text-xs text-muted" data-testid="late-all-count">
              {filtered ? `${shown.length} of ${items.length} match` : plural(items.length, "order")}
              {total > items.length ? ` · ${total - items.length} more not read` : ""}
              {asOf ? ` · as of ${formatDate(asOf)}` : ""}
            </span>
          )}
        </CardHeader>
        <CardBody>
          {error ? (
            <ErrorNotice message={error} onRetry={load} />
          ) : total === null ? (
            <div className="space-y-2" role="status" aria-label="Loading">
              <Skeleton className="h-10" />
              <Skeleton className="h-10" />
              <Skeleton className="h-10" />
            </div>
          ) : items.length === 0 ? (
            <p className="text-sm text-muted">No late purchase orders you can see.</p>
          ) : (
            <>
              <div className="mb-4 flex flex-wrap items-center gap-2" data-testid="late-filters">
                <div role="tablist" aria-label="Days late" className="flex flex-wrap gap-1">
                  {DAY_BANDS.map((b) => (
                    <button
                      key={b.id}
                      type="button"
                      role="tab"
                      aria-selected={filter.days === b.id}
                      onClick={() => setFilter((f) => ({ ...f, days: b.id }))}
                      className={cn("rounded-lg border border-border px-2.5 py-1 text-meta cursor-pointer", filter.days === b.id ? "bg-surface-2 font-medium text-fg" : "text-muted hover:bg-surface-2")}
                    >
                      {b.label} {dayCount(b.id)}
                    </button>
                  ))}
                </div>
                <label className="text-meta text-muted">
                  <span className="sr-only">Supplier</span>
                  <select
                    aria-label="Supplier"
                    value={filter.supplier ?? "\u0000all"}
                    onChange={(e) => setFilter((f) => ({ ...f, supplier: e.target.value === "\u0000all" ? null : e.target.value }))}
                    className="rounded-lg border border-border bg-surface px-2 py-1"
                  >
                    <option value={"\u0000all"}>Every supplier</option>
                    {suppliers.map(([name, n]) => (
                      <option key={name || "\u0000none"} value={name}>
                        {name || "No supplier shown"} ({n})
                      </option>
                    ))}
                  </select>
                </label>
                <label className="text-meta text-muted">
                  <span className="sr-only">Value</span>
                  <select
                    aria-label="Value"
                    value={filter.value}
                    onChange={(e) => setFilter((f) => ({ ...f, value: e.target.value as ValueBand }))}
                    className="rounded-lg border border-border bg-surface px-2 py-1"
                  >
                    {VALUE_BANDS.map((b) => (
                      <option key={b.id} value={b.id}>
                        {b.label}
                      </option>
                    ))}
                  </select>
                </label>
                <span className="text-meta text-muted">Values are in each order&apos;s own currency.</span>
              </div>
              {shown.length === 0 ? (
                <p className="text-sm text-muted" data-testid="late-none-match">
                  No late order matches these filters.
                </p>
              ) : (
                <>
                  <table className="hidden w-full text-row sm:table" data-testid="late-table">
                    <caption className="sr-only">Late purchase orders</caption>
                    <thead>
                      <tr className="h-9 border-b border-border text-left text-label text-muted">
                        {COLUMNS.map((c) => (
                          <th key={c.key} scope="col" className={cn("px-2", c.right && "text-right")} aria-sort={sort.key === c.key ? (falling(sort) ? "descending" : "ascending") : "none"}>
                            <button type="button" onClick={() => onSort(c.key)} className="cursor-pointer hover:text-fg">
                              {c.label}
                              {sort.key === c.key ? (falling(sort) ? " ↓" : " ↑") : ""}
                            </button>
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {shown.map((po) => (
                        <tr key={po.object_id} className="h-11 border-b border-border last:border-0 hover:bg-surface-2" data-testid="late-row">
                          <td className="px-2 font-medium">
                            <Link href={hrefOf(po)} className="hover:underline">
                              {po.label}
                            </Link>
                          </td>
                          <td className="px-2">{po.supplier ?? <span className="text-muted">Not shown</span>}</td>
                          <td className="whitespace-nowrap px-2 tabular-nums">{po.due_date ? formatDate(po.due_date) : <span className="text-muted">Not shown</span>}</td>
                          <td className="whitespace-nowrap px-2 text-right font-semibold tabular-nums text-bad">{po.days_late ?? "—"}</td>
                          <td className="whitespace-nowrap px-2 text-right tabular-nums">
                            <Value po={po} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <ul className="-mx-2 divide-y divide-border sm:hidden">
                    {shown.map((po) => (
                      <li key={po.object_id}>
                        <Link href={hrefOf(po)} className="block rounded-lg px-2 py-2.5 hover:bg-surface-2">
                          <span className="flex items-center justify-between gap-2 text-sm font-medium">
                            <span className="min-w-0 truncate">
                              {po.label}
                              {po.supplier ? <span className="font-normal text-muted"> · {po.supplier}</span> : null}
                            </span>
                            <span className="shrink-0 tabular-nums text-bad">{po.days_late ?? "—"} days</span>
                          </span>
                          <span className="block truncate text-xs text-muted">
                            {[po.due_date ? `Needed ${formatDate(po.due_date, { short: true })}` : null, po.amount ? formatMoney(po.amount.amount, po.amount.currency) + (po.amount_suspect ? " (check)" : "") : null].filter(Boolean).join(" · ") || po.reason}
                          </span>
                        </Link>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
