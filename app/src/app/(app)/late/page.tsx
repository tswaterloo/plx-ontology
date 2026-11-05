"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useListKeys } from "@/lib/list-keys";
import { getLateList } from "@/lib/api";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { formatDate, formatMoney, objectHref, plural, recordLabel, safeHref } from "@/lib/format";
import { DAY_BANDS, inDayBand, matches, sortLate, supplierCounts, VALUE_BANDS, type DayBand, type LateFilter, type SortKey, type ValueBand } from "@/lib/late";
import type { LatePo } from "@/lib/types";
import { EtaPill } from "@/components/eta-confirmed";
import { LateShape } from "@/components/late-shape";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { listShown } from "@/lib/eta";
import { LoadProblem } from "@/components/ui/notice";
import { PageHeader } from "@/components/ui/page-header";
import { Pending } from "@/components/ui/skeleton";

const PAGE = 100;
const MOST = 1000; // the list reads at most this many orders, then says how many it left out

function hrefOf(po: LatePo): string {
  return safeHref(po.open_url) ?? objectHref("PurchaseOrder", po.object_id);
}

function Value({ po }: { po: LatePo }) {
  if (!po.amount)
    return (
      <span className="text-muted" title="No value on this order">
        <span aria-hidden>–</span>
        <span className="sr-only">No value</span>
      </span>
    );
  const shown = formatMoney(po.amount.amount, po.amount.currency);
  if (!po.amount_suspect) return <>{shown}</>;
  return (
    <span title={`Check the data: ${po.amount_suspect}`} className="inline-flex flex-wrap items-baseline justify-end gap-x-1.5 text-muted">
      <span>{shown}</span>
      <span className="rounded-sm border border-dotted border-suspect-line px-1 text-label text-suspect">Check total</span>
      <span className="sr-only">: {po.amount_suspect}</span>
    </span>
  );
}

/** The row's "ETA confirmed?" answer, linking to the order's own check (firm or lead, and its date). */
function ListEta({ po, className, linked = false, quiet = false }: { po: LatePo; className?: string; linked?: boolean; quiet?: boolean }) {
  const shown = listShown(po);
  if (!shown)
    return linked ? (
      <Link href={hrefOf(po)} className="whitespace-nowrap text-meta text-muted hover:text-accent hover:underline">
        Open PO
      </Link>
    ) : null;
  const pill = <EtaPill shown={shown} className={className} testId="late-eta" quiet={quiet} />;
  if (!linked) return pill;
  return (
    <Link href={hrefOf(po)} className="hover:opacity-80" title="Open the order's confirmation check">
      {pill}
    </Link>
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
  const [busy, setBusy] = useState(false);
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
      if (c.signal.aborted) return;
      setBusy(noticeKind(e) === "busy");
      setError(plainError(e));
    });
    return () => c.abort();
  }, []);
  useEffect(() => load(), [load]);
  // "Who is late" on Today links here with ?supplier=
  useEffect(() => {
    const want = new URLSearchParams(window.location.search).get("supplier");
    if (want) setFilter((f) => ({ ...f, supplier: want }));
  }, []);

  const suppliers = useMemo(() => supplierCounts(items), [items]);
  const shown = useMemo(() => sortLate(items.filter((p) => matches(p, filter)), sort.key, sort.desc), [items, filter, sort]);
  const dayCount = (band: DayBand) => items.filter((p) => matches(p, { ...filter, days: "all" }) && inDayBand(p, band)).length;
  const filtered = filter.supplier !== null || filter.days !== "all" || filter.value !== "all";
  const onSort = (key: SortKey) => setSort((s) => (s.key === key ? { key, desc: !s.desc } : { key, desc: false }));

  const oldest = useMemo(() => sortLate(items, "days", false)[0], [items]);
  const list = useRef<HTMLDivElement>(null);
  useListKeys(list);
  // every row the same ETA answer: the header's line says it; the cells go quiet
  const uniformEta = useMemo(() => shown.length > 3 && shown.every((p) => listShown(p)?.label === listShown(shown[0])?.label), [shown]);
  const maxDays = useMemo(() => Math.max(0, ...items.map((p) => p.days_late ?? 0)), [items]);
  const unconfirmed = useMemo(() => items.filter((p) => listShown(p)).length, [items]);
  const verdict = "Late POs";
  const why =
    total === null || error || items.length === 0
      ? null
      : [
          `${total} past the needed-by date.`,
          oldest && typeof oldest.days_late === "number" ? `Oldest: ${recordLabel("PurchaseOrder", oldest.label)}${oldest.supplier ? ` (${oldest.supplier})` : ""}, ${plural(oldest.days_late, "day")}.` : null,
          unconfirmed > 0 ? `${unconfirmed} with no supplier date.` : null,
        ]
          .filter(Boolean)
          .join(" ");

  return (
    <div className="mx-auto w-full max-w-[1200px] px-4 pt-6 pb-16 sm:px-8 sm:pt-8">
      <PageHeader
        className="mb-4"
        testId="late-header"
        title={verdict}
        sub={why}
      />
      {items.length > 1 && !error && (
        <div className="mb-6">
          <LateShape items={items} onSupplier={(name) => setFilter((f) => ({ ...f, supplier: name }))} picked={filter.supplier} />
        </div>
      )}
      <Card data-testid="late-all" ref={list}>
        <CardHeader>
          <CardTitle>{filtered ? "Matching POs" : "All late POs"}</CardTitle>
          {total !== null && (
            <span className="text-meta text-muted tabular-nums" data-testid="late-all-count">
              {filtered ? `${shown.length} of ${items.length} match` : plural(items.length, "PO")}
              {total > items.length ? ` · ${total - items.length} more not loaded` : ""}
              {asOf ? ` · BC as of ${formatDate(asOf)}` : ""}
            </span>
          )}
        </CardHeader>
        <CardBody>
          {error ? (
            <LoadProblem message={error} busy={busy} onRetry={load} />
          ) : total === null ? (
            <Pending label="Reading late POs from Business Central" />
          ) : items.length === 0 ? (
            <p className="py-4 text-row text-muted">No late POs. A PO shows here the day it passes its needed-by date in Business Central.</p>
          ) : (
            <>
              <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2" data-testid="late-filters">
                <div role="tablist" aria-label="Days late" className="flex flex-wrap rounded-sm border border-border-strong">
                  {DAY_BANDS.map((b) => (
                    <button
                      key={b.id}
                      type="button"
                      role="tab"
                      aria-selected={filter.days === b.id}
                      onClick={() => setFilter((f) => ({ ...f, days: b.id }))}
                      className={cn("border-l border-border-strong px-2.5 py-1 text-meta first:border-l-0 cursor-pointer", filter.days === b.id ? "bg-fg font-medium text-bg" : "text-muted hover:bg-surface hover:text-fg")}
                    >
                      {b.label} <span className={cn("tabular-nums", filter.days === b.id ? "opacity-70" : "text-faint")}>{dayCount(b.id)}</span>
                    </button>
                  ))}
                </div>
                <label className="text-meta text-muted">
                  <span className="sr-only">Supplier</span>
                  <select
                    aria-label="Supplier"
                    value={filter.supplier ?? "\u0000all"}
                    onChange={(e) => setFilter((f) => ({ ...f, supplier: e.target.value === "\u0000all" ? null : e.target.value }))}
                    className="h-7 rounded border border-border-strong bg-surface px-2 text-fg"
                  >
                    <option value={"\u0000all"}>All suppliers</option>
                    {suppliers.map(([name, n]) => (
                      <option key={name || "\u0000none"} value={name}>
                        {name || "No supplier"} ({n})
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
                    className="h-7 rounded border border-border-strong bg-surface px-2 text-fg"
                  >
                    {VALUE_BANDS.map((b) => (
                      <option key={b.id} value={b.id}>
                        {b.label}
                      </option>
                    ))}
                  </select>
                </label>
                <span className="text-meta text-faint sm:ml-auto">Value in the PO&apos;s currency</span>
              </div>
              {shown.length === 0 ? (
                <p className="py-4 text-row text-muted" data-testid="late-none-match">
                  No POs match these filters.
                </p>
              ) : (
                <>
                  <table className="hidden w-full text-row sm:table" data-testid="late-table">
                    <caption className="sr-only">Late purchase orders</caption>
                    <thead>
                      <tr className="h-8 border-y border-border-strong bg-bg-subtle text-left text-meta text-muted">
                        {COLUMNS.map((c) => (
                          <th key={c.key} scope="col" className={cn("px-2 font-semibold first:pl-3", c.right && "text-right")} aria-sort={sort.key === c.key ? (falling(sort) ? "descending" : "ascending") : "none"}>
                            <button type="button" onClick={() => onSort(c.key)} className="cursor-pointer font-medium hover:text-fg">
                              {c.label}
                              {sort.key === c.key ? (falling(sort) ? " ↓" : " ↑") : ""}
                            </button>
                          </th>
                        ))}
                        <th scope="col" className="px-2 font-semibold">
                          Supplier date
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {shown.map((po) => (
                        <tr key={po.object_id} className="h-9 border-b border-border hover:bg-surface has-[a[data-row-link]:focus-visible]:bg-accent-soft" data-testid="late-row">
                          <td className="px-2 pl-3 font-medium">
                            <Link href={hrefOf(po)} className="hover:text-accent hover:underline focus-visible:outline-offset-4" data-row-link>
                              {recordLabel("PurchaseOrder", po.label)}
                            </Link>
                          </td>
                          <td className="px-2">{po.supplier ?? <span className="text-muted">None</span>}</td>
                          <td className="whitespace-nowrap px-2 tabular-nums">{po.due_date ? formatDate(po.due_date) : <span className="text-muted">None</span>}</td>
                          <td className="whitespace-nowrap px-2 text-right tabular-nums text-late">
                            {/* a bar on a shared scale beside the number: the column reads as a picture */}
                            <span className="inline-flex items-center justify-end gap-2">
                              {typeof po.days_late === "number" && maxDays > 0 && (
                                <span className="hidden h-1.5 w-[96px] justify-end md:flex" aria-hidden>
                                  <span className="h-full bg-late-line" style={{ width: `${Math.max(2, (po.days_late / maxDays) * 96)}px` }} />
                                </span>
                              )}
                              <span className="w-8">{po.days_late ?? "–"}</span>
                            </span>
                          </td>
                          <td className="whitespace-nowrap px-2 text-right tabular-nums">
                            <Value po={po} />
                          </td>
                          <td className="px-2">
                            <ListEta po={po} linked quiet={uniformEta} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {uniformEta && listShown(shown[0]) && (
                    <p className="mb-1 text-meta text-muted sm:hidden">Supplier date, all of these: {listShown(shown[0])!.label}</p>
                  )}
                  <ul className="-mx-2 divide-y divide-border sm:hidden">
                    {shown.map((po) => (
                      <li key={po.object_id}>
                        <Link href={hrefOf(po)} className="block px-2 py-3 hover:bg-surface focus-visible:bg-accent-soft focus-visible:-outline-offset-2" data-row-link>
                          <span className="flex items-center justify-between gap-2 text-row font-medium">
                            <span className="min-w-0 truncate">
                              {recordLabel("PurchaseOrder", po.label)}
                              {po.supplier ? <span className="font-normal text-muted"> · {po.supplier}</span> : null}
                            </span>
                            <span className="shrink-0 tabular-nums text-late">{po.days_late ?? "–"} days</span>
                          </span>
                          {listShown(po) && !uniformEta && <ListEta po={po} className="mt-1" />}
                          <span className="mt-0.5 block truncate text-meta text-muted tabular-nums">
                            {[po.due_date ? `Needed ${formatDate(po.due_date, { short: true })}` : null, po.amount ? formatMoney(po.amount.amount, po.amount.currency) + (po.amount_suspect ? " · check total" : "") : null].filter(Boolean).join(" · ") || po.reason}
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
