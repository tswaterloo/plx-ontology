"use client";

import { SectionTabs } from "@/components/shell";
import { ArrowDownWideNarrow, ArrowUpNarrowWide, ChartNoAxesColumn, ChevronDown, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { LateOverview, nextStep } from "@/components/late/overview";
import { stepWords, TodayDetail, type QueueRow } from "@/components/today-detail";
import { DoneRing, lateEdge, lateFigure, OpenRing, StatusGlyph } from "@/components/ui/glyphs";
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

/** The chase line's words for the supplier date: the date given, why there is none, or the list's state. */
function promiseText(po: LatePo, c: Chase): string | null {
  if (c.promised) return `${c.promiseWords ? "maybe " : "promised "}${formatDate(c.promised, { short: true })}`;
  if (c.promiseWords) return c.promiseWords;
  if (po.confirmation === "none_found") return "no supplier date";
  if (po.confirmation === "found_unverified") return "date unverified";
  if (po.confirmation === "needs_check") return "confirmation to check";
  return null;
}

function Row({ po, on, onPick, chase }: { po: LatePo; on: boolean; onPick: (() => void) | null; chase: Chase }) {
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
          "flex gap-3 rounded-[3px] px-3 py-2.5 outline-none transition-[background-color,box-shadow] duration-100",
          typeof po.days_late === "number" && lateEdge(po.days_late),
          on ? "bg-row-on shadow-[var(--lift),inset_2px_0_0_var(--accent)]" : "hover:bg-hover",
          "focus-visible:ring-2 focus-visible:ring-ring/50",
        )}
      >
        <span className="min-w-0 flex-1">
          <span className="flex items-baseline gap-2">
            <span className="num shrink-0 text-[13px] font-semibold leading-[18px]" data-testid="late-po">
              {recordLabel("PurchaseOrder", po.label)}
            </span>
            <span className="min-w-0 truncate text-[13px] leading-[18px] text-fg/80">{po.supplier ?? "No supplier"}</span>
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
            <span className="mt-0.5 block truncate text-[12px] leading-4 text-muted tabular-nums" data-testid="late-chase">
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
    <p className="flex flex-wrap gap-x-4 gap-y-0.5 border-b border-border px-5 py-2 text-[12px] leading-4 text-muted" data-testid="late-glance">
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

  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    setTotal(null);
    (async () => {
      // every page, so filters and sorting cover the whole list, not the first 25
      const all = await readAllLate((offset, size) => getLateList(offset, c.signal, size), PAGE, MOST);
      setAsOf(all.asOf);
      setStaleSince(all.staleSince);
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
  // each row's chase facts (promised date, last word, whom to ask, what it holds) on every width
  useEffect(() => {
    const c = new AbortController();
    getSuggestions(c.signal).then((s) => setSuggestions(s.status === "read" ? (s.items ?? []) : []), () => undefined);
    return () => c.abort();
  }, []);
  useEffect(() => {
    if (!desktop) return;
    const c = new AbortController();
    getActionStates(c.signal).then((r) => setStates(r.cards ?? {}), () => undefined);
    return () => c.abort();
  }, [desktop]);

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
    <div className="shrink-0 border-b border-border" data-testid="late-filters">
      <div role="tablist" aria-label="Days late" className="flex h-10 gap-4 overflow-x-auto px-5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
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
                "relative flex h-full shrink-0 items-center gap-1.5 text-[13px] whitespace-nowrap cursor-pointer",
                filter.days === b.id ? "font-medium text-fg after:absolute after:inset-x-0 after:-bottom-px after:h-[2px] after:bg-accent" : "text-muted hover:text-fg",
              )}
            >
              {BAND_SHORT[b.id]}
              <span className="text-[12px] font-normal num text-faint">{n}</span>
            </button>
          );
        })}
      </div>
      <div className="flex min-h-11 flex-wrap items-center gap-x-0.5 gap-y-0.5 border-t border-border px-3 py-1.5">
        <Pick label="Supplier" value={filter.supplier ?? "\u0000all"} onChange={(v) => setSupplier(v === "\u0000all" ? null : v)} className="min-w-[10rem] flex-1">
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
        heading
        title="Late POs"
        testId="late-header"
        className="bg-list"
        actions={
          desktop && ready && items.length > 0 ? (
            <ToolbarButton onClick={() => pick(null)} aria-pressed={!selected} title="Overview (Esc)" className={cn(!selected && "bg-hover text-fg")}>
              <ChartNoAxesColumn aria-hidden />
              Overview
            </ToolbarButton>
          ) : undefined
        }
      />
      <SectionTabs section="home" />
      <PageIntro path="/?show=late" />
      {!desktop && ready && items.length > 0 && <div className="@container border-b border-border px-5 pt-4 pb-5">{overview("figures")}</div>}
      {controls}
      {ready && items.length > 0 && <LateGlance items={filtered ? shown : items} />}
      <StaleNote since={staleSince} className="px-5 py-1.5" />
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
                            {g.name || "No supplier"} <span className="font-normal num text-faint">{g.rows.length}</span>
                          </h2>
                        </div>
                      )}
                      <ul className="px-2">
                        {g.rows.map((po) => (
                          <Row key={po.object_id} po={po} chase={chaseOf(bySubject.get(po.object_id))} on={desktop && po.object_id === picked} onPick={desktop ? () => pick(po.object_id) : null} />
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
                      <p className="text-[13px] text-muted">Nothing to sum up: no PO is late.</p>
                    ) : error ? (
                      <p className="text-[13px] text-muted">The overview waits until the list can be read.</p>
                    ) : (
                    <Pending label="Reading the overview" />
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
