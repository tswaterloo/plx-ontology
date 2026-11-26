"use client";

import { ArrowDownWideNarrow, ArrowLeft, ArrowUpNarrowWide, ArrowUpRight, Check, ChevronDown, ChevronRight, RefreshCw, X } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { LateOverview, nextStep } from "@/components/late/overview";
import { CloseControl } from "@/components/late/close-control";
import { stepWords, TodayDetail, type QueueRow } from "@/components/today-detail";
import { Button } from "@/components/ui/button";
import { DoneRing, lateEdge, lateFigure, OpenRing, StatusGlyph } from "@/components/ui/glyphs";
import { FeedNote } from "@/components/feed-note";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar, ToolbarButton } from "@/components/ui/toolbar";
import { PageIntro } from "@/components/ui/page-intro";
import { getActionStates, getLateList, getSuggestions } from "@/lib/api";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { listShown } from "@/lib/eta";
import { formatDate, formatMoney, objectHref, plural, recordLabel, safeHref, SYSTEM_LABEL } from "@/lib/format";
import { chaseOf, DAY_BANDS, DEC, inDayBand, inStateBand, lateTotals, matches, readAllLate, sortLate, STATE_BANDS, supplierCounts, VALUE_BANDS, type Chase, type DayBand, type LateFilter, type SortKey, type StateBand, type ValueBand } from "@/lib/late";
import { useListKeys } from "@/lib/list-keys";
import type { ActionResult, LatePo, ProactiveSuggestion } from "@/lib/types";

const PAGE = 100;
const DAY_MS = 24 * 60 * 60 * 1000;
const MOST = 20000; // the list reads every order up to this many (the server's count bound), then says how many it left out

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
    <label className={cn("relative inline-flex h-8 min-w-0 items-center rounded-md border border-border bg-bg transition-colors duration-150 ease-[cubic-bezier(0.2,0,0,1)] hover:border-border-strong hover:bg-hover dark:bg-surface", className)}>
      <select
        aria-label={label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-full w-full min-w-0 cursor-pointer appearance-none truncate rounded-md bg-transparent pr-7 pl-2.5 text-[12.5px] font-medium text-fg outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
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

/** The chase line's words for the supplier date: the date given, why there is none, or the list's state. */
function promiseText(po: LatePo, c: Chase): string | null {
  if (c.promised) return `${c.promiseWords ? "maybe " : "promised "}${formatDate(c.promised, { short: true })}`;
  if (c.promiseWords) return c.promiseWords;
  if (po.confirmation === "none_found") return "no supplier date";
  if (po.confirmation === "found_unverified") return "date unverified";
  if (po.confirmation === "needs_check") return "confirmation to check";
  return null;
}

function Row({
  po,
  on,
  onPick,
  onClose,
  chase,
  detail,
}: {
  po: LatePo;
  on: boolean;
  onPick: (() => void) | null;
  onClose?: () => void;
  chase: Chase;
  detail?: React.ReactNode;
}) {
  const eta = listShown(po);
  const promise = promiseText(po, chase);
  const facts = [
    promise,
    chase.lastWord ? `last word ${formatDate(chase.lastWord.at, { short: true })}, ${SYSTEM_LABEL[chase.lastWord.system] ?? chase.lastWord.system}` : null,
    chase.ask ? `ask ${chase.ask}` : null,
    chase.holds.length ? `holds ${recordLabel(chase.holds[0].type, chase.holds[0].label)}${chase.holds.length > 1 ? ` +${chase.holds.length - 1}` : ""}` : null,
    typeof po.open_lines === "number" && po.open_lines > 0 ? plural(po.open_lines, "open line") : null,
  ].filter(Boolean);
  const next = chase.next ? stepWords(chase.rule, chase.next, po.supplier ?? null) : nextStep(po);
  const needed = po.due_date ? `Needed ${formatDate(po.due_date, { short: true })}` : "No due date";
  const href = hrefOf(po);
  return (
    <li>
      <div
        className={cn(
          "group relative flex items-start gap-2 px-3 py-2.5 transition-colors duration-150 ease-[cubic-bezier(0.2,0,0,1)] sm:px-4",
          typeof po.days_late === "number" && lateEdge(po.days_late),
          on ? "bg-row-on before:absolute before:inset-y-0 before:left-0 before:w-[2px] before:bg-accent" : "hover:bg-hover",
        )}
      >
        <a
          href={href}
          onClick={(e) => {
            if (!onPick || e.metaKey || e.ctrlKey || e.shiftKey) return;
            e.preventDefault();
            if (on && onClose) onClose();
            else onPick();
          }}
          aria-current={on ? "true" : undefined}
          aria-expanded={onPick ? on : undefined}
          data-row-link=""
          data-testid="late-row"
          className="flex min-w-0 flex-1 gap-3 rounded-xs outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          <span className="min-w-0 flex-1">
            <span className="flex items-baseline gap-2">
              <span className="num shrink-0 text-[13.5px] font-semibold leading-5 text-fg" data-testid="late-po">
                {recordLabel("PurchaseOrder", po.label)}
              </span>
              <span className="min-w-0 truncate text-[13.5px] leading-5 text-fg/80" title={po.supplier ?? "No supplier"}>{po.supplier ?? "No supplier"}</span>
              <span className={cn("ml-auto flex shrink-0 items-center gap-1.5 self-center pl-2 text-[12px] leading-4 font-medium num", typeof po.days_late === "number" && lateFigure(po.days_late))}>
                {typeof po.days_late === "number" ? `${po.days_late}d` : "–"}
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
            {facts.length > 0 && (
              <span className="mt-0.5 block truncate text-[12px] leading-4 text-muted tabular-nums" data-testid="late-chase" title={facts.join(" · ")}>
                {facts.join(" · ")}
              </span>
            )}
            {next && (
              <span className="mt-0.5 block truncate text-[12px] leading-4 text-fg/80" data-testid="late-next" title={next}>
                {next}
              </span>
            )}
          </span>
        </a>
        <div className="mt-0.5 flex shrink-0 items-center gap-0.5">
          <Link
            href={href}
            aria-label={`Open ${recordLabel("PurchaseOrder", po.label)}`}
            title="Open PO"
            className="hidden size-6 shrink-0 items-center justify-center rounded-sm text-faint opacity-0 transition-opacity duration-150 hover:bg-hover hover:text-fg focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 group-hover:opacity-100 sm:inline-flex"
          >
            <ArrowUpRight className="size-3.5" strokeWidth={2} aria-hidden />
          </Link>
          {onPick && (
            <span className="inline-flex size-5 items-center justify-center text-faint transition-colors group-hover:text-muted" aria-hidden>
              {on ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
            </span>
          )}
        </div>
      </div>
      {on && detail}
    </li>
  );
}

/** The list at a glance: suppliers, value per currency (exact, never across currencies), what blocks how many. */
function LateGlance({ items }: { items: LatePo[] }) {
  const money = lateTotals(items);
  const suppliers = new Set(items.map((p) => p.supplier ?? "")).size;
  const noDate = items.filter((p) => p.confirmation === "none_found").length;
  const toCheck = items.filter((p) => p.confirmation === "found_unverified" || p.confirmation === "needs_check").length;
  const noValue = items.filter((p) => !p.amount || p.amount_suspect || !DEC.test(String(p.amount.amount).trim())).length;
  return (
    <p className="flex flex-wrap gap-x-4 gap-y-0.5 text-[12px] leading-4 text-muted" data-testid="late-glance">
      <span>
        <span className="num font-semibold text-fg">{suppliers}</span> {suppliers === 1 ? "supplier" : "suppliers"}
      </span>
      {money.map((m) => (
        <span key={m.currency} title={`${plural(m.count, "PO")} in ${m.currency}`}>
          <span className="num font-semibold text-fg">{formatMoney(m.amount, m.currency)}</span> in {plural(m.count, "PO")}
        </span>
      ))}
      {noValue > 0 && <span>{noValue} without a trusted value</span>}
      {noDate > 0 && (
        <span>
          <span className="num font-semibold text-late">{noDate}</span> no supplier date
        </span>
      )}
      {toCheck > 0 && (
        <span>
          <span className="num font-semibold text-fg">{toCheck}</span> date to check
        </span>
      )}
    </p>
  );
}

function ListSkeleton() {
  return <Pending page label="Reading late POs" testId="late-pending" />;
}

/** Every late purchase order the reader may see: filtered by supplier, days late and value band; sorted; one opened beside the list. */
export function LateView() {
  const desktop = useDesktop();
  const [items, setItems] = useState<LatePo[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [asOf, setAsOf] = useState<string | null>(null);
  const [staleSince, setStaleSince] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState<LateFilter>({ supplier: null, days: "all", value: "all", state: "all" });
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "days", desc: false });
  const [picked, setPicked] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<ProactiveSuggestion[]>([]);
  const [states, setStates] = useState<Record<string, ActionResult>>({});
  // the very late orders that show no sign of life: counted always, listed when the group is opened
  const [staleTotal, setStaleTotal] = useState<number | null>(null);
  const [staleItems, setStaleItems] = useState<LatePo[] | null>(null);
  const [staleOpen, setStaleOpen] = useState(false);
  const [staleError, setStaleError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [statusNote, setStatusNote] = useState<string | null>(null);

  const load = useCallback((soft = false) => {
    const c = new AbortController();
    setError(null);
    if (soft) setRefreshing(true);
    else setTotal(null);
    (async () => {
      // every page, so filters and sorting cover the whole list, not the first 25
      const all = await readAllLate((offset, size) => getLateList(offset, c.signal, size), PAGE, MOST);
      setAsOf(all.asOf);
      setStaleSince(all.staleSince);
      setTotal(all.total);
      setItems(all.items);
      setStaleTotal(all.staleTotal);
      setStaleItems(null);
    })()
      .catch((e) => {
        if (c.signal.aborted) return;
        setBusy(noticeKind(e) === "busy");
        setError(plainError(e));
      })
      .finally(() => {
        if (!c.signal.aborted) setRefreshing(false);
      });
    return () => c.abort();
  }, []);
  useEffect(() => load(false), [load]);
  useEffect(() => {
    if (!statusNote) return;
    const t = window.setTimeout(() => setStatusNote(null), 5000);
    return () => window.clearTimeout(t);
  }, [statusNote]);
  // "Late by supplier" links here with ?supplier=; ?sel= reopens a PO
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const want = q.get("supplier");
    if (want) setFilter((f) => ({ ...f, supplier: want }));
    if (q.get("sel")) setPicked(q.get("sel"));
  }, []);
  // the drafted follow-ups and their states, for the PO opened beside the list
  // each row's chase facts (promised date, last word, whom to ask, what it holds) on every width
  useEffect(() => {
    const c = new AbortController();
    getSuggestions(c.signal).then((s) => setSuggestions(s.status === "read" ? (s.items ?? []) : []), () => undefined);
    return () => c.abort();
  }, []);
  useEffect(() => {
    if (!desktop && !picked) return;
    const c = new AbortController();
    getActionStates(c.signal).then((r) => setStates(r.cards ?? {}), () => undefined);
    return () => c.abort();
  }, [desktop, picked]);

  // the stale group is read when it is opened (and again after a close changes it)
  useEffect(() => {
    if (!staleOpen || staleItems !== null || !staleTotal) return;
    const c = new AbortController();
    setStaleError(null);
    readAllLate((offset, size) => getLateList(offset, c.signal, size, true), PAGE, MOST).then(
      (all) => setStaleItems(all.items),
      (e) => {
        if (!c.signal.aborted) setStaleError(plainError(e));
      },
    );
    return () => c.abort();
  }, [staleOpen, staleItems, staleTotal]);

  const suppliers = useMemo(() => supplierCounts(items), [items]);
  const shown = useMemo(() => sortLate(items.filter((p) => matches(p, filter)), sort.key, sort.desc), [items, filter, sort]);
  const dayCount = (band: DayBand) => items.filter((p) => matches(p, { ...filter, days: "all" }) && inDayBand(p, band)).length;
  const filtered = filter.supplier !== null || filter.days !== "all" || filter.value !== "all" || (!!filter.state && filter.state !== "all");
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
    window.history.replaceState(null, "", `/${qs ? `?${qs}` : ""}`);
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

  const clearFilters = useCallback(() => {
    setFilter({ supplier: null, days: "all", value: "all", state: "all" });
  }, []);

  const ready = total !== null && !error;
  const overview = (parts: "all" | "figures" | "chart") =>
    ready && items.length > 0 ? (
      <LateOverview items={items} total={total} picked={filter.supplier} onSupplier={setSupplier} onPick={desktop ? (p) => pick(p.object_id) : undefined} parts={parts} />
    ) : null;

  const count = total !== null && (
    <span className="tabular-nums" data-testid="late-all-count">
      {filtered ? `${shown.length} of ${items.length} match` : total > items.length ? `${items.length} of ${total} POs loaded` : plural(items.length, "PO")}
      {filtered && total > items.length ? ` · ${total} late in all` : ""}
      {/* the sidebar's Data as of is the screen's time: the purchase orders' own day only when it is behind */}
      {asOf && Date.now() - new Date(asOf).getTime() > DAY_MS ? ` · BC as of ${formatDate(asOf, { short: true })}` : ""}
    </span>
  );

  const controls = ready && items.length > 0 && (
    <div className="space-y-2.5" data-testid="late-filters">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-2">
        <div role="tablist" aria-label="Days late" className="flex items-center gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {DAY_BANDS.map((b) => {
            const n = dayCount(b.id);
            // a band with nothing in it is no choice (as Home's chips): only All, or the band picked
            if (n === 0 && b.id !== "all" && filter.days !== b.id) return null;
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
                  "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md px-2.5 text-[12.5px] whitespace-nowrap transition-[background-color,color,transform] duration-150 ease-[cubic-bezier(0.2,0,0,1)] active:scale-[0.98] cursor-pointer",
                  filter.days === b.id ? "bg-selected font-medium text-fg" : "text-muted hover:bg-hover hover:text-fg",
                )}
              >
                {BAND_SHORT[b.id]}
                <span className="text-[11.5px] font-normal num text-faint">{n}</span>
              </button>
            );
          })}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Pick label="Supplier" value={filter.supplier ?? "\u0000all"} onChange={(v) => setSupplier(v === "\u0000all" ? null : v)} className="min-w-[9.5rem] max-w-[14rem]">
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
          {/* the supplier date state filters only where some PO has one: with none known every
              choice but one would list nothing */}
          {(items.some((p) => p.confirmation) || (filter.state ?? "all") !== "all") && (
            <Pick label="Supplier date" value={filter.state ?? "all"} onChange={(v) => setFilter((f) => ({ ...f, state: v as StateBand }))} className="w-[9rem] shrink-0">
              {STATE_BANDS.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.label} ({b.id === "all" ? items.length : items.filter((p) => inStateBand(p, b.id)).length})
                </option>
              ))}
            </Pick>
          )}
          <span className="flex shrink-0 items-center gap-1">
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
              className="flex size-8 items-center justify-center rounded-md border border-border bg-bg text-muted transition-[background-color,border-color,color,transform] duration-150 ease-[cubic-bezier(0.2,0,0,1)] hover:border-border-strong hover:bg-hover hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 active:scale-[0.98] cursor-pointer"
            >
              {falling(sort) ? <ArrowDownWideNarrow className="size-4" aria-hidden /> : <ArrowUpNarrowWide className="size-4" aria-hidden />}
            </button>
          </span>
          {filtered && (
            <button
              type="button"
              onClick={clearFilters}
              data-testid="late-clear-filters"
              className="h-8 rounded-md px-2 text-[12px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg cursor-pointer"
            >
              Clear filters
            </button>
          )}
        </div>
      </div>
      <LateGlance items={filtered ? shown : items} />
    </div>
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-bg">
      <Toolbar
        heading
        parent={{ label: "Home", href: "/" }}
        title="Late POs"
        testId="late-header"
        actions={
          <ToolbarButton disabled={total === null || refreshing} onClick={() => load(true)} title="Refresh late POs" aria-label="Refresh late POs">
            <RefreshCw className={cn(refreshing && "animate-spin")} aria-hidden />
            <span className="max-sm:sr-only">{refreshing ? "Refreshing" : "Refresh"}</span>
          </ToolbarButton>
        }
      />
      <nav aria-label="Breadcrumb" className="border-b border-border/60 px-4 py-2 sm:px-6">
        <Link
          href="/"
          className="inline-flex items-center gap-1.5 text-[12.5px] font-medium text-muted transition-colors hover:text-fg"
        >
          <ArrowLeft className="size-3.5" aria-hidden />
          <span>Home</span>
        </Link>
      </nav>
      <PageIntro path="/?show=late" />
      <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto" data-testid="late-all">
        <div ref={desktop ? undefined : phoneRef} className="mx-auto w-full max-w-[980px] space-y-5 px-4 py-5 pb-12 sm:px-6">
          <StaleNote since={staleSince} />
          {statusNote && (
            <div role="status" className="flex items-center justify-between gap-2 rounded-md border border-border bg-surface/60 px-3 py-2 text-[12.5px] text-fg">
              <span className="flex min-w-0 items-center gap-2">
                <Check className="size-3.5 shrink-0 text-ok" strokeWidth={2.2} aria-hidden />
                <span className="truncate">{statusNote}</span>
              </span>
              <button type="button" aria-label="Dismiss notice" onClick={() => setStatusNote(null)} className="inline-flex size-5 items-center justify-center rounded-sm text-muted hover:bg-hover hover:text-fg cursor-pointer">
                <X className="size-3" aria-hidden />
              </button>
            </div>
          )}
          <FeedNote feed="late_pos" className="text-[12px] text-muted" />
          {ready && items.length > 0 && <div className="@container">{overview("figures")}</div>}
          {controls}
          {error ? (
            <LoadProblem message={error} busy={busy} onRetry={() => load(false)} />
          ) : total === null ? (
            <ListSkeleton />
          ) : items.length === 0 ? (
            <p className="py-8 text-[13px] text-muted">{staleTotal ? "No late POs that show signs of life." : "No late POs."}</p>
          ) : (
            <section className="space-y-2" aria-label="Late POs">
              <div className="flex items-baseline justify-between gap-3 text-[12px] text-muted">
                {count}
                {filter.supplier !== null && (
                  <button type="button" onClick={() => setSupplier(null)} className="inline-flex min-w-0 items-center gap-1 rounded-md text-muted hover:text-fg cursor-pointer" aria-label="Show every supplier">
                    <span className="truncate">{filter.supplier || "No supplier"}</span>
                    <X className="size-3.5 shrink-0" aria-hidden />
                  </button>
                )}
              </div>
              {shown.length === 0 ? (
                <div className="py-6 text-[13px] text-muted" data-testid="late-none-match">
                  <p>No POs match these filters.</p>
                  <Button size="sm" variant="secondary" onClick={clearFilters} className="mt-2">
                    Clear filters
                  </Button>
                </div>
              ) : (
                <div data-testid="late-table" aria-label="Late purchase orders" role="group" className="space-y-4">
                  {groups.map((g) => (
                    <div key={g.name ?? "all"}>
                      {g.name !== null && (
                        <div className="flex items-baseline gap-2 pb-1.5">
                          <h2 className="truncate text-[12px] font-medium text-muted">
                            {g.name || "No supplier"} <span className="font-normal num text-faint">{g.rows.length}</span>
                          </h2>
                        </div>
                      )}
                      <ul className="divide-y divide-border border-y border-border">
                        {g.rows.map((po) => {
                          const isExpanded = po.object_id === picked;
                          return (
                            <Row
                              key={po.object_id}
                              po={po}
                              chase={chaseOf(bySubject.get(po.object_id))}
                              on={isExpanded}
                              onPick={() => pick(po.object_id)}
                              onClose={() => pick(null)}
                              detail={
                                isExpanded ? (
                                  <div className="border-t border-border/60 bg-surface/40">
                                    <div className="flex items-center justify-between gap-2 border-b border-border/60 px-4 py-2">
                                      <CloseControl
                                        key={po.object_id}
                                        order={{ handle: po.sources?.[0]?.handle, object_id: po.object_id }}
                                        onChanged={(res) => {
                                          if (res.closed) {
                                            setItems((cur) => cur.filter((x) => x.object_id !== po.object_id));
                                            setTotal((t) => (t != null ? Math.max(0, t - 1) : t));
                                            setStatusNote(`Closed ${recordLabel("PurchaseOrder", po.label)}`);
                                            pick(null);
                                          }
                                          load(true);
                                        }}
                                      />
                                      <button
                                        type="button"
                                        onClick={() => pick(null)}
                                        aria-label="Close details"
                                        title="Close (Esc)"
                                        className="inline-flex size-7 shrink-0 items-center justify-center rounded-md text-faint hover:bg-hover hover:text-fg cursor-pointer"
                                      >
                                        <X className="size-3.5" aria-hidden />
                                      </button>
                                    </div>
                                    <TodayDetail
                                      key={po.object_id}
                                      row={lateRow(po, bySubject.get(po.object_id))}
                                      state={bySubject.get(po.object_id) ? states[bySubject.get(po.object_id)!.id] : null}
                                      onDone={(r) => setStates((prev) => ({ ...prev, [r.card_id]: r }))}
                                    />
                                  </div>
                                ) : undefined
                              }
                            />
                          );
                        })}
                      </ul>
                    </div>
                  ))}
                </div>
              )}
            </section>
          )}
          {ready && !!staleTotal && (
            <details className="border-t border-border pt-3" open={staleOpen} onToggle={(e) => setStaleOpen((e.currentTarget as HTMLDetailsElement).open)} data-testid="late-stale">
              <summary className="cursor-pointer py-1.5 text-[13px] font-medium text-fg" data-testid="late-stale-summary">
                Probably closed or missed in the data: check and close <span className="num font-normal text-muted">({staleTotal})</span>
              </summary>
              <p className="pb-2 text-[12px] leading-4 text-muted">
                Very late, and nothing shows them alive since their needed-by date: no receipt, no message from the supplier or in Teams, no change to the order. They are not counted above.
              </p>
              {staleError ? (
                <LoadProblem message={staleError} onRetry={() => { setStaleError(null); setStaleItems(null); }} />
              ) : staleItems === null ? (
                <p className="py-2 text-[13px] text-muted">Reading them</p>
              ) : (
                <ul className="divide-y divide-border border-y border-border">
                  {staleItems.map((po) => (
                    <li key={po.object_id} data-testid="late-stale-row">
                      <ul>
                        <Row po={po} chase={chaseOf(undefined)} on={false} onPick={null} />
                      </ul>
                      <div className="px-3 pb-2.5 sm:px-4">
                        <CloseControl order={{ handle: po.sources?.[0]?.handle, object_id: po.object_id }} compact onChanged={() => load(true)} />
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </details>
          )}
          {ready && items.length > 1 && (
            <div className="border-t border-border pt-5">{overview("chart")}</div>
          )}
        </div>
      </div>
    </div>
  );
}
