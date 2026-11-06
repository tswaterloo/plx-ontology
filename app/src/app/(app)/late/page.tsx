"use client";

import { ArrowDownWideNarrow, ArrowUpNarrowWide, ChartNoAxesColumn, ChevronDown, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { LateOverview } from "@/components/late/overview";
import { TodayDetail, type QueueRow } from "@/components/today-detail";
import { AgeRing, DoneRing, OpenRing, StatusGlyph } from "@/components/ui/glyphs";
import { Monogram } from "@/components/ui/monogram";
import { LoadProblem } from "@/components/ui/notice";
import { Toolbar, ToolbarButton } from "@/components/ui/toolbar";
import { getActionStates, getLateList, getSuggestions } from "@/lib/api";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { listShown } from "@/lib/eta";
import { formatDate, formatMoney, objectHref, plural, recordLabel, safeHref } from "@/lib/format";
import { DAY_BANDS, inDayBand, matches, readAllLate, sortLate, supplierCounts, VALUE_BANDS, type DayBand, type LateFilter, type SortKey, type ValueBand } from "@/lib/late";
import { useListKeys } from "@/lib/list-keys";
import type { ActionResult, LatePo, ProactiveSuggestion } from "@/lib/types";

const PAGE = 100;
const MOST = 1000; // the list reads at most this many orders, then says how many it left out

function hrefOf(po: LatePo): string {
  return safeHref(po.open_url) ?? objectHref("PurchaseOrder", po.object_id);
}

/** A late PO as the detail pane reads it (the same shape Today builds). */
function lateRow(p: LatePo, suggestion?: ProactiveSuggestion): QueueRow {
  return {
    key: p.object_id,
    group: "po_late",
    type: "PurchaseOrder",
    objectId: p.object_id,
    title: recordLabel("PurchaseOrder", p.label),
    counterpart: p.supplier ?? null,
    line: p.due_date ? `Needed ${formatDate(p.due_date, { short: true })}` : "No due date",
    days: p.days_late ?? null,
    status: "late",
    href: hrefOf(p),
    late: p,
    suggestion,
  };
}

/** Short tab words; the full band is the tab's name. */
const BAND_SHORT: Record<DayBand, string> = { all: "All", d30: "≤ 30 d", d90: "31–90 d", dmore: "> 90 d" };

const SORTS: { key: SortKey; label: string }[] = [
  { key: "days", label: "Days late" },
  { key: "value", label: "Value" },
  { key: "supplier", label: "Supplier" },
  { key: "due", label: "Needed by" },
  { key: "po", label: "PO number" },
];

/** Whether the sort runs largest or latest first (days late and value do unless flipped). */
function falling(s: { key: SortKey; desc: boolean }): boolean {
  return (s.key === "days" || s.key === "value") !== s.desc;
}

function useDesktop() {
  const [desk, setDesk] = useState(false);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const m = window.matchMedia("(min-width: 1024px)");
    const on = () => setDesk(m.matches);
    on();
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  return desk;
}

/** A native select drawn as a quiet toolbar control. */
function Pick({ label, value, onChange, children, className }: { label: string; value: string; onChange: (v: string) => void; children: React.ReactNode; className?: string }) {
  return (
    <label className={cn("relative inline-flex h-8 min-w-0 items-center rounded-lg hover:bg-hover", className)}>
      <select
        aria-label={label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-full w-full min-w-0 cursor-pointer appearance-none truncate rounded-lg bg-transparent pr-7 pl-2.5 text-[13px] text-fg outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
      >
        {children}
      </select>
      <ChevronDown className="pointer-events-none absolute right-2 size-3.5 text-muted" aria-hidden />
    </label>
  );
}

function Value({ po }: { po: LatePo }) {
  if (!po.amount)
    return (
      <span data-testid="late-value" title="No value on this order">
        <span className="sr-only">No value</span>
      </span>
    );
  const shown = formatMoney(po.amount.amount, po.amount.currency);
  if (!po.amount_suspect) return <span data-testid="late-value">{shown}</span>;
  return (
    <span data-testid="late-value" title={`Check the total: ${po.amount_suspect}`} className="inline-flex items-center gap-1">
      {shown}
      <StatusGlyph status="suspect" size={11} />
      <span className="sr-only">: check the total, {po.amount_suspect}</span>
    </span>
  );
}

function Row({ po, on, onPick }: { po: LatePo; on: boolean; onPick: (() => void) | null }) {
  const eta = listShown(po);
  const needed = po.due_date ? `Needed ${formatDate(po.due_date, { short: true })}` : "No due date";
  return (
    <li>
      <a
        href={hrefOf(po)}
        onClick={(e) => {
          if (!onPick || e.metaKey || e.ctrlKey || e.shiftKey) return;
          e.preventDefault();
          onPick();
        }}
        aria-current={on ? "true" : undefined}
        data-row-link=""
        data-testid="late-row"
        className={cn(
          "flex gap-3 rounded-[10px] px-3 py-2.5 outline-none transition-[background-color,box-shadow] duration-100",
          on ? "bg-row-on shadow-lift" : "hover:bg-hover",
          "focus-visible:ring-2 focus-visible:ring-ring/50",
        )}
      >
        <Monogram name={po.supplier} size={32} />
        <span className="min-w-0 flex-1">
          <span className="flex items-baseline gap-2">
            <span className="shrink-0 text-[13px] font-semibold leading-[18px]" data-testid="late-po">
              {recordLabel("PurchaseOrder", po.label)}
            </span>
            <span className="min-w-0 truncate text-[13px] leading-[18px] text-fg/80">{po.supplier ?? "No supplier"}</span>
            <span className="ml-auto flex shrink-0 items-center gap-1.5 self-center pl-2 text-[12px] leading-4 font-medium tabular-nums">
              {typeof po.days_late === "number" ? (
                <>
                  {po.days_late}d
                  <AgeRing days={po.days_late} size={13} />
                </>
              ) : (
                "–"
              )}
            </span>
          </span>
          <span className="mt-0.5 flex items-center gap-2 text-[12px] leading-4 text-muted tabular-nums">
            <span className="min-w-0 truncate">
              {needed}
              {po.amount && <span aria-hidden> · </span>}
              <Value po={po} />
            </span>
            {eta && (
              <span className="ml-auto shrink-0" title={eta.label} data-testid="late-eta" data-status={eta.status}>
                {po.confirmation === "confirmed" ? <DoneRing size={13} /> : <OpenRing size={13} tone="neutral" />}
                <span className="sr-only">{eta.label}</span>
              </span>
            )}
          </span>
        </span>
      </a>
    </li>
  );
}

function ListSkeleton() {
  const widths = ["w-[62%]", "w-[53%]", "w-[44%]"];
  return (
    <ul className="space-y-1 px-2 pt-2" aria-busy="true" aria-label="Loading late POs">
      {Array.from({ length: 9 }).map((_, i) => (
        <li key={i} className="flex gap-3 px-3 py-2.5">
          <span className="skeleton size-8 rounded-[9px]" />
          <span className="flex-1 space-y-2">
            <span className={cn("skeleton block h-3", widths[i % 3])} />
            <span className={cn("skeleton block h-2.5", i % 2 ? "w-[32%]" : "w-[40%]")} />
          </span>
        </li>
      ))}
    </ul>
  );
}

/** Every late purchase order the reader may see: filtered by supplier, days late and value band; sorted; one opened beside the list. */
export default function LatePage() {
  const desktop = useDesktop();
  const [items, setItems] = useState<LatePo[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [asOf, setAsOf] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState<LateFilter>({ supplier: null, days: "all", value: "all" });
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "days", desc: false });
  const [picked, setPicked] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<ProactiveSuggestion[]>([]);
  const [states, setStates] = useState<Record<string, ActionResult>>({});

  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    setTotal(null);
    (async () => {
      // every page, so filters and sorting cover the whole list, not the first 25
      const all = await readAllLate((offset, size) => getLateList(offset, c.signal, size), PAGE, MOST);
      setAsOf(all.asOf);
      setTotal(all.total);
      setItems(all.items);
    })().catch((e) => {
      if (c.signal.aborted) return;
      setBusy(noticeKind(e) === "busy");
      setError(plainError(e));
    });
    return () => c.abort();
  }, []);
  useEffect(() => load(), [load]);
  // "Late by supplier" links here with ?supplier=; ?sel= reopens a PO
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const want = q.get("supplier");
    if (want) setFilter((f) => ({ ...f, supplier: want }));
    if (q.get("sel")) setPicked(q.get("sel"));
  }, []);
  // the drafted follow-ups and their states, for the PO opened beside the list
  useEffect(() => {
    if (!desktop) return;
    const c = new AbortController();
    getSuggestions(c.signal).then((s) => setSuggestions(s.status === "read" ? (s.items ?? []) : []), () => undefined);
    getActionStates(c.signal).then((r) => setStates(r.cards ?? {}), () => undefined);
    return () => c.abort();
  }, [desktop]);

  const suppliers = useMemo(() => supplierCounts(items), [items]);
  const shown = useMemo(() => sortLate(items.filter((p) => matches(p, filter)), sort.key, sort.desc), [items, filter, sort]);
  const dayCount = (band: DayBand) => items.filter((p) => matches(p, { ...filter, days: "all" }) && inDayBand(p, band)).length;
  const filtered = filter.supplier !== null || filter.days !== "all" || filter.value !== "all";
  const bySubject = useMemo(() => new Map(suggestions.map((s) => [s.subject.object_id, s])), [suggestions]);
  const selected = picked ? (items.find((p) => p.object_id === picked) ?? null) : null;
  // grouped under each supplier when sorted by supplier
  const groups = useMemo(() => {
    if (sort.key !== "supplier") return [{ name: null as string | null, rows: shown }];
    const out: { name: string | null; rows: LatePo[] }[] = [];
    for (const p of shown) {
      const last = out[out.length - 1];
      if (last && last.name === (p.supplier ?? "")) last.rows.push(p);
      else out.push({ name: p.supplier ?? "", rows: [p] });
    }
    return out;
  }, [shown, sort.key]);

  const pick = useCallback((key: string | null) => {
    setPicked(key);
    const q = new URLSearchParams(window.location.search);
    if (key) q.set("sel", key);
    else q.delete("sel");
    const qs = q.toString();
    window.history.replaceState(null, "", `/late${qs ? `?${qs}` : ""}`);
  }, []);
  const setSupplier = useCallback((name: string | null) => setFilter((f) => ({ ...f, supplier: name })), []);

  // desktop: j / k and the arrows move the selection, Enter opens the PO, Esc goes back to the overview
  const listRef = useRef<HTMLDivElement>(null);
  const phoneRef = useRef<HTMLDivElement>(null);
  useListKeys(phoneRef);
  useEffect(() => {
    if (!desktop) return;
    function onKey(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target;
      if (t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      if (document.querySelector('[role="dialog"]')) return;
      if (!shown.length) return;
      const at = shown.findIndex((p) => p.object_id === picked);
      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        pick(shown[Math.min(shown.length - 1, at + 1)].object_id);
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        pick(shown[Math.max(0, at - 1)].object_id);
      } else if (e.key === "Escape" && picked) {
        e.preventDefault();
        pick(null);
      } else if (e.key === "Enter" && selected && !(t instanceof HTMLElement && /^(A|BUTTON)$/.test(t.tagName))) {
        // the detail pane's own "Open PO" link: a client navigation
        const open = document.querySelector<HTMLAnchorElement>('[data-testid="today-detail"] [data-testid="open-record"]');
        if (!open) return;
        e.preventDefault();
        open.click();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [desktop, shown, picked, selected, pick]);
  useEffect(() => {
    listRef.current?.querySelector('[aria-current="true"]')?.scrollIntoView({ block: "nearest" });
  }, [picked]);

  const ready = total !== null && !error;
  const overview = (parts: "all" | "figures" | "chart") =>
    ready && items.length > 0 ? (
      <LateOverview items={items} total={total} picked={filter.supplier} onSupplier={setSupplier} onPick={desktop ? (p) => pick(p.object_id) : undefined} parts={parts} />
    ) : null;

  const count = total !== null && (
    <span className="tabular-nums" data-testid="late-all-count">
      {filtered ? `${shown.length} of ${items.length} match` : plural(items.length, "PO")}
      {total > items.length ? ` · ${total - items.length} more not loaded` : ""}
      {asOf ? ` · BC as of ${formatDate(asOf, { short: true })}` : ""}
    </span>
  );

  const controls = ready && items.length > 0 && (
    <div className="shrink-0 border-b border-border" data-testid="late-filters">
      <div role="tablist" aria-label="Days late" className="flex h-10 gap-4 overflow-x-auto px-5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        {DAY_BANDS.map((b) => {
          const n = dayCount(b.id);
          return (
            <button
              key={b.id}
              type="button"
              role="tab"
              aria-selected={filter.days === b.id}
              aria-label={`${b.label} ${n}`}
              title={b.label}
              onClick={() => setFilter((f) => ({ ...f, days: b.id }))}
              className={cn(
                "relative flex h-full shrink-0 items-center gap-1.5 text-[13px] whitespace-nowrap cursor-pointer",
                filter.days === b.id ? "font-medium text-fg after:absolute after:inset-x-0 after:-bottom-px after:h-[2px] after:rounded-full after:bg-fg" : "text-muted hover:text-fg",
              )}
            >
              {BAND_SHORT[b.id]}
              <span className="text-[12px] font-normal tabular-nums text-faint">{n}</span>
            </button>
          );
        })}
      </div>
      <div className="flex h-11 items-center gap-0.5 border-t border-border px-3">
        <Pick label="Supplier" value={filter.supplier ?? "\u0000all"} onChange={(v) => setSupplier(v === "\u0000all" ? null : v)} className="flex-1">
          <option value={"\u0000all"}>All suppliers</option>
          {suppliers.map(([name, n]) => (
            <option key={name || "\u0000none"} value={name}>
              {name || "No supplier"} ({n})
            </option>
          ))}
        </Pick>
        <Pick label="Value" value={filter.value} onChange={(v) => setFilter((f) => ({ ...f, value: v as ValueBand }))} className="w-[7.5rem] shrink-0">
          {VALUE_BANDS.map((b) => (
            <option key={b.id} value={b.id}>
              {b.label}
            </option>
          ))}
        </Pick>
        <span className="flex shrink-0 items-center">
          <Pick label="Sort by" value={sort.key} onChange={(v) => setSort({ key: v as SortKey, desc: false })} className="w-[7.25rem]">
            {SORTS.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
              </option>
            ))}
          </Pick>
          <button
            type="button"
            onClick={() => setSort((s) => ({ ...s, desc: !s.desc }))}
            aria-label={falling(sort) ? "Sorted largest or latest first" : "Sorted smallest or earliest first"}
            title={falling(sort) ? "Largest first" : "Smallest first"}
            className="flex size-8 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg cursor-pointer"
          >
            {falling(sort) ? <ArrowDownWideNarrow className="size-4" aria-hidden /> : <ArrowUpNarrowWide className="size-4" aria-hidden />}
          </button>
        </span>
      </div>
    </div>
  );

  const list = (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col border-border bg-list lg:w-[400px] lg:flex-none lg:shrink-0 lg:border-r xl:w-[420px]" aria-label="Late POs">
      <Toolbar
        title="Late POs"
        testId="late-header"
        className="bg-[color-mix(in_srgb,var(--list)_90%,transparent)]"
        actions={
          desktop && ready && items.length > 0 ? (
            <ToolbarButton onClick={() => pick(null)} aria-pressed={!selected} title="Overview (Esc)" className={cn(!selected && "bg-hover text-fg")}>
              <ChartNoAxesColumn aria-hidden />
              Overview
            </ToolbarButton>
          ) : undefined
        }
      />
      {!desktop && ready && items.length > 0 && <div className="@container border-b border-border px-5 pt-4 pb-5">{overview("figures")}</div>}
      {controls}
      <div ref={listRef} className="min-h-0 flex-1 pb-6 lg:overflow-y-auto" data-testid="late-all">
        <div ref={desktop ? undefined : phoneRef}>
          {error ? (
            <div className="p-4">
              <LoadProblem message={error} busy={busy} onRetry={load} />
            </div>
          ) : total === null ? (
            <ListSkeleton />
          ) : items.length === 0 ? (
            <p className="px-5 py-10 text-[13px] text-muted">No late POs.</p>
          ) : (
            <>
              <div className="flex h-9 items-end justify-between gap-3 px-5 pb-1.5 text-[12px] text-muted">
                {count}
                {filter.supplier !== null && (
                  <button type="button" onClick={() => setSupplier(null)} className="inline-flex min-w-0 items-center gap-1 rounded-md text-muted hover:text-fg cursor-pointer" aria-label="Show every supplier">
                    <span className="truncate">{filter.supplier || "No supplier"}</span>
                    <X className="size-3.5 shrink-0" aria-hidden />
                  </button>
                )}
              </div>
              {shown.length === 0 ? (
                <p className="px-5 py-6 text-[13px] text-muted" data-testid="late-none-match">
                  No POs match these filters.
                </p>
              ) : (
                <div data-testid="late-table" aria-label="Late purchase orders" role="group">
                  {groups.map((g) => (
                    <div key={g.name ?? "all"}>
                      {g.name !== null && (
                        <div className="sticky top-0 z-10 flex h-9 items-end gap-2 bg-list px-5 pb-1.5">
                          <h2 className="truncate text-[12px] font-medium text-muted">
                            {g.name || "No supplier"} <span className="font-normal tabular-nums text-faint">{g.rows.length}</span>
                          </h2>
                        </div>
                      )}
                      <ul className="px-2">
                        {g.rows.map((po) => (
                          <Row key={po.object_id} po={po} on={desktop && po.object_id === picked} onPick={desktop ? () => pick(po.object_id) : null} />
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
          {!desktop && ready && items.length > 1 && <div className="mt-6 border-t border-border px-5 pt-6">{overview("chart")}</div>}
        </div>
      </div>
    </section>
  );

  return (
    <div className="flex h-full min-h-0 flex-1 lg:h-dvh">
      {list}
      {desktop && (
        <div className="hidden min-w-0 flex-1 lg:flex">
          {selected ? (
            <TodayDetail
              key={selected.object_id}
              row={lateRow(selected, bySubject.get(selected.object_id))}
              state={bySubject.get(selected.object_id) ? states[bySubject.get(selected.object_id)!.id] : null}
              onDone={(r) => setStates((prev) => ({ ...prev, [r.card_id]: r }))}
            />
          ) : (
            <div className="flex min-h-0 flex-1 flex-col bg-bg">
              <div className="flex h-[52px] shrink-0 items-center gap-1 border-b border-border px-5 text-[13px] text-muted">
                Late POs <span className="px-0.5 text-faint">›</span> <span className="font-medium text-fg">Overview</span>
              </div>
              <div className="@container min-h-0 flex-1 overflow-y-auto">
                <div className="max-w-[880px] px-6 pt-6 pb-12 sm:px-8 sm:pt-7">
                  {overview("all") ||
                    (ready ? (
                      <p className="text-[13px] text-muted">No late POs.</p>
                    ) : (
                    <div className="flex gap-10" aria-busy={!ready || undefined}>
                      {[0, 1, 2].map((i) => (
                        <div key={i} className="space-y-2">
                          <div className="skeleton h-3 w-12" />
                          <div className="skeleton h-6 w-24" />
                        </div>
                      ))}
                    </div>
                    ))}
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
