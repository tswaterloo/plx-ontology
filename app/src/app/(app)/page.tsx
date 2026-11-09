"use client";

import { ChevronLeft, ChevronRight, X } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { HiddenNotice } from "@/components/answer-parts";
import { loadHome, loadLate, MobileAccount, MobileSearch } from "@/components/shell";
import { ReplyList } from "@/components/reply-list";
import { TodayDetail, type QueueRow } from "@/components/today-detail";
import { Pending } from "@/components/ui/skeleton";
import { AgeRing, OpenRing } from "@/components/ui/glyphs";
import { Monogram } from "@/components/ui/monogram";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { isPutAway } from "@/lib/actions";
import { getActionStates, getReplies, getBriefing, getBriefingItems, getSuggestions, markBriefingSeen } from "@/lib/api";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { formatDate, formatMoney, objectHref, readable, recordLabel, safeHref } from "@/lib/format";
import { headline, refreshingLine, staleLine, yourMorning } from "@/lib/home";
import type { ReplyTriage, ActionResult, Briefing, BriefingItem, HomeResponse, LatePo, ProactiveSuggestion } from "@/lib/types";

const TZ = "America/Toronto";

/** "{thing} didn't load. Try again." with the incident kept for the Reference line. */
function didntLoad(thing: string, err: unknown): string {
  const msg = plainError(err);
  if (/^The Workbench server failed/.test(msg)) {
    const inc = /incident ([0-9a-f]+)/.exec(msg)?.[1];
    return `Couldn't load ${thing}. Try again.${inc ? ` (incident ${inc})` : ""}`;
  }
  return msg;
}

/** The list's groups, most urgent first; a kind the app doesn't know sorts before "no due date". */
const ORDER = ["po_late", "negative_margin", "customer_order_at_risk", "po_due", "eta_slipped", "vendor_no_reply", "vendor_terms_unapplied", "quantity_mismatch", "lot_on_hold", "lot_near_expiry", "quote_accepted_no_order"];
const SHORT_GROUP: Record<string, string> = { po_late: "Late", po_undated: "No date", po_due: "Due soon" };
/** My work's chips: what the reader does about an item. */
const VERBS = [
  { verb: "reply", label: "Reply" },
  { verb: "approve", label: "Approve" },
  { verb: "chase", label: "Chase" },
  { verb: "decide", label: "Decide" },
] as const;
const VERB_OF_KIND: Record<string, string> = {
  reply_owed: "reply",
  approval_waiting: "approve",
  label_change: "approve",
  late_po: "chase",
  waiting_on_them: "chase",
  customer_chasing: "chase",
};
function verbOf(g: { type: string; rows: QueueRow[] }): string {
  if (g.type === "po_late" || g.type === "po_due" || g.type === "po_undated") return "chase";
  const kind = g.rows.find((r) => r.item?.alert_kind)?.item?.alert_kind;
  return (kind && VERB_OF_KIND[kind]) || "decide";
}

/**
 * Prepared work: drafts an autopilot made for you, each one click to approve. The
 * autopilot engine fills it; with nothing prepared it shows nothing, so My work stays one
 * list.
 */
function PreparedWork({ items }: { items: { id: string; title: string; href: string }[] }) {
  if (items.length === 0) return null;
  return (
    <section aria-label="Prepared for you" className="px-5 pt-3" data-testid="prepared-work">
      <h2 className="text-[12px] font-medium text-muted">Prepared for you</h2>
      <ul className="mt-1 space-y-1">
        {items.map((i) => (
          <li key={i.id}>
            <Link href={i.href} className="text-[13px] hover:underline">
              {i.title}
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

const rankOf = (t: string) => (t === "po_undated" ? 999 : ORDER.indexOf(t) === -1 ? 500 : ORDER.indexOf(t));

function lateRow(p: LatePo, suggestion?: ProactiveSuggestion, item?: BriefingItem): QueueRow {
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
    href: safeHref(p.open_url) ?? objectHref("PurchaseOrder", p.object_id),
    late: p,
    item,
    suggestion,
  };
}

function itemRow(i: BriefingItem, suggestion?: ProactiveSuggestion): QueueRow {
  const type = i.object_type ?? "PurchaseOrder";
  const amount = i.amount && !i.amount_suspect ? formatMoney(i.amount.amount, i.amount.currency) : null;
  const summary = readable(i.summary).replace(/^An open purchase order has no due date recorded$/i, "Open, no due date");
  return {
    key: i.object_id,
    group: i.event_type,
    type,
    objectId: i.object_id,
    title: recordLabel(type, i.subject_label ?? ""),
    counterpart: i.counterpart ?? null,
    line: [summary, amount].filter(Boolean).join(" · "),
    days: i.event_type === "po_undated" ? null : (i.days ?? null),
    status: i.amount_suspect ? "suspect" : i.event_type === "po_undated" ? "risk" : /late|negative/.test(i.event_type) ? "late" : "risk",
    href: safeHref(i.open_url) ?? objectHref(type, i.object_id),
    item: i,
    suggestion,
  };
}

function useDesktop() {
  const [desk, setDesk] = useState(true);
  useEffect(() => {
    const m = window.matchMedia("(min-width: 1024px)");
    const on = () => setDesk(m.matches);
    on();
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  return desk;
}

function Row({ row, on, onPick, actionState, testId }: { row: QueueRow; on: boolean; onPick: () => void; actionState?: ActionResult; testId?: string }) {
  const away = isPutAway(actionState);
  return (
    <li data-testid={testId}>
      <a
        href={row.href}
        onClick={(e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey) return;
          e.preventDefault();
          onPick();
        }}
        aria-current={on ? "true" : undefined}
        data-row-link=""
        data-testid="briefing-item"
        className={cn(
          "flex gap-3 rounded-[10px] px-3 py-2.5 outline-none transition-[background-color,box-shadow] duration-100",
          on ? "bg-row-on shadow-lift" : "hover:bg-hover",
          "focus-visible:ring-2 focus-visible:ring-ring/50",
          away && "opacity-60",
        )}
      >
        <Monogram name={row.counterpart} size={32} />
        <span className="min-w-0 flex-1">
          <span className="flex items-baseline gap-2">
            <span className="shrink-0 text-[13px] font-semibold leading-[18px]">{row.title}</span>
            {row.counterpart && <span className="min-w-0 truncate text-[13px] leading-[18px] text-fg/80">{row.counterpart}</span>}
            <span className="ml-auto flex shrink-0 items-center gap-1.5 self-center pl-2 text-[12px] leading-4 tabular-nums">
              {row.days != null ? (
                <>
                  <span className="font-medium text-fg">{row.days}d</span>
                  {row.status === "late" && <AgeRing days={row.days} size={13} />}
                </>
              ) : row.group === "po_undated" ? (
                <>
                  <span className="text-muted">No date</span>
                  <OpenRing size={13} />
                </>
              ) : null}
            </span>
          </span>
          <span className="mt-0.5 flex items-baseline gap-2 text-[12px] leading-4 text-muted">
            <span className="min-w-0 truncate">{row.line}</span>
            {away && <span className="ml-auto shrink-0 text-faint">Done</span>}
          </span>
        </span>
      </a>
    </li>
  );
}

function ListSkeleton() {
  return (
    <ul className="space-y-1 px-2 pt-2" aria-busy="true" aria-label="Loading">
      {Array.from({ length: 9 }).map((_, i) => (
        <li key={i} className="flex gap-3 px-3 py-2.5">
          <span className="skeleton size-8 rounded-[9px]" />
          <span className="flex-1 space-y-2">
            <span className="skeleton block h-3" style={{ width: `${62 - (i % 3) * 9}%` }} />
            <span className="skeleton block h-2.5" style={{ width: `${40 - (i % 2) * 8}%` }} />
          </span>
        </li>
      ))}
    </ul>
  );
}

function Today() {
  const router = useRouter();
  const params = useSearchParams();
  const desktop = useDesktop();
  const [home, setHome] = useState<HomeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [briefingError, setBriefingError] = useState<string | null>(null);
  const [late, setLate] = useState<LatePo[] | null>(null);
  const [suggestions, setSuggestions] = useState<ProactiveSuggestion[]>([]);
  const [suggestionsRead, setSuggestionsRead] = useState(false);
  const supplier = params.get("supplier");
  const [states, setStates] = useState<Record<string, ActionResult>>({});
  const [pages, setPages] = useState<Record<string, { items: BriefingItem[]; more: boolean; total: number }>>({});
  const [filter, setFilter] = useState<string>(params.get("view") === "reply" ? "reply" : "all");
  // the Reply chip's list: your own mail and chats waiting on your answer (inbox triage)
  const [replies, setReplies] = useState<ReplyTriage | null>(null);
  const [repliesError, setRepliesError] = useState<unknown>(null);
  useEffect(() => {
    const c = new AbortController();
    getReplies(c.signal).then(setReplies, (e) => !c.signal.aborted && setRepliesError(e));
    return () => c.abort();
  }, []);
  const [picked, setPicked] = useState<string | null>(params.get("sel"));
  const listRef = useRef<HTMLDivElement>(null);

  const load = useCallback((fresh = false) => {
    const c = new AbortController();
    setError(null);
    loadHome(fresh)
      .then((h) => !c.signal.aborted && setHome(h))
      .catch((e) => {
        if (c.signal.aborted) return;
        setBusy(noticeKind(e) === "busy");
        setError(didntLoad("Today", e));
      });
    return () => c.abort();
  }, []);
  useEffect(() => load(), [load]);

  const loadBriefing = useCallback(() => {
    const c = new AbortController();
    setBriefingError(null);
    getBriefing(c.signal)
      .then((b) => {
        setBriefing(b);
        if (b.status === "read") markBriefingSeen().catch(() => undefined);
        // each group's next page: the briefing carries only its top items
        for (const g of b.groups ?? [])
          if (g.event_type !== "po_late" && g.count > (g.in_top ?? 0))
            getBriefingItems(g.event_type, 0, "all", c.signal)
              .then((p) => setPages((cur) => ({ ...cur, [g.event_type]: { items: p.items ?? [], more: !!p.more, total: p.total ?? 0 } })))
              .catch(() => undefined);
      })
      .catch((e) => !c.signal.aborted && setBriefingError(didntLoad("the briefing", e)));
    return () => c.abort();
  }, []);
  useEffect(() => loadBriefing(), [loadBriefing]);

  useEffect(() => {
    const c = new AbortController();
    loadLate().then((l) => !c.signal.aborted && setLate(l), () => !c.signal.aborted && setLate([]));
    getSuggestions(c.signal).then((s) => setSuggestions(s.status === "read" ? (s.items ?? []) : []), () => undefined).finally(() => !c.signal.aborted && setSuggestionsRead(true));
    getActionStates(c.signal).then((r) => setStates(r.cards ?? {}), () => undefined);
    return () => c.abort();
  }, []);

  const more = useCallback((eventType: string) => {
    const cur = pages[eventType];
    getBriefingItems(eventType, cur?.items.length ?? 0)
      .then((p) => setPages((x) => ({ ...x, [eventType]: { items: [...(x[eventType]?.items ?? []), ...(p.items ?? [])], more: !!p.more, total: p.total ?? 0 } })))
      .catch(() => undefined);
  }, [pages]);

  const groups = useMemo(() => {
    const bySubject = new Map(suggestions.map((s) => [s.subject.object_id, s]));
    const top = briefing?.status === "read" ? (briefing.items ?? []) : [];
    const itemBy = new Map(top.map((i) => [i.object_id, i]));
    const out: { type: string; label: string; count: number; rows: QueueRow[]; more: boolean }[] = [];
    const na = home?.needs_attention;
    const lateList = late?.length ? late : na?.status === "read" ? (na.late_pos ?? []) : [];
    if (lateList.length) {
      const rows = [...lateList].sort((a, b) => (b.days_late ?? -1) - (a.days_late ?? -1)).map((p) => lateRow(p, bySubject.get(p.object_id), itemBy.get(p.object_id)));
      out.push({ type: "po_late", label: "Late", count: na?.late_total ?? rows.length, rows, more: false });
    }
    // My work holds only the kinds the alert registry sends home (measured >= 90% on 15+
    // and actionable); everything else waits in Watching
    const homeOnly = briefing?.status === "read" && typeof briefing.home_count === "number";
    for (const g of briefing?.status === "read" ? (briefing.groups ?? []) : []) {
      if (g.event_type === "po_late") continue;
      const seen = new Set<string>();
      const rows = [...top.filter((i) => i.event_type === g.event_type), ...(pages[g.event_type]?.items ?? [])]
        .filter((i) => !homeOnly || i.home)
        .filter((i) => (seen.has(i.id) ? false : (seen.add(i.id), true)))
        .map((i) => itemRow(i, bySubject.get(i.object_id)));
      if (rows.length) out.push({ type: g.event_type, label: SHORT_GROUP[g.event_type] ?? g.label, count: homeOnly ? rows.length : g.count, rows, more: !homeOnly && rows.length < g.count });
    }
    return out.sort((a, b) => rankOf(a.type) - rankOf(b.type));
  }, [briefing, late, home, suggestions, pages]);

  const bySupplier = supplier
    ? groups.map((g) => ({ ...g, rows: g.rows.filter((r) => r.counterpart === supplier) })).filter((g) => g.rows.length).map((g) => ({ ...g, count: g.rows.length, more: false }))
    : groups;
  const visible = filter === "all" ? bySupplier : bySupplier.filter((g) => verbOf(g) === filter);
  const flat = useMemo(() => visible.flatMap((g) => g.rows), [visible]);
  const selectedKey = picked ?? (desktop ? flat[0]?.key ?? null : null);
  const selected = flat.find((r) => r.key === selectedKey) ?? groups.flatMap((g) => g.rows).find((r) => r.key === selectedKey) ?? null;

  const pick = useCallback(
    (key: string) => {
      setPicked(key);
      const q = new URLSearchParams();
      if (supplier) q.set("supplier", supplier);
      q.set("sel", key);
      if (desktop) window.history.replaceState(null, "", `/?${q}`);
      else router.push(`/?${q}`);
    },
    [desktop, router, supplier],
  );
  // phone: Back from a record returns to the list
  useEffect(() => {
    if (!desktop) setPicked(params.get("sel"));
  }, [params, desktop]);
  // a supplier picked in the sidebar starts at its first PO
  const lastSupplier = useRef(supplier);
  useEffect(() => {
    if (lastSupplier.current === supplier) return;
    lastSupplier.current = supplier;
    if (desktop) setPicked(null);
    setFilter("all");
  }, [supplier, desktop]);

  // j / k and the arrows move the selection; Enter opens the record
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target;
      if (t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(t.tagName) && e.key === "Enter")) return;
      if (t instanceof HTMLElement && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
      if (!flat.length || !desktop) return;
      const at = flat.findIndex((r) => r.key === selectedKey);
      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        pick(flat[Math.min(flat.length - 1, at + 1)].key);
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        pick(flat[Math.max(0, at - 1)].key);
      } else if (e.key === "Enter" && selected) {
        e.preventDefault();
        router.push(selected.href);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [flat, selectedKey, selected, desktop, pick, router]);
  useEffect(() => {
    listRef.current?.querySelector('[aria-current="true"]')?.scrollIntoView({ block: "nearest" });
  }, [selectedKey]);

  const head = headline(home, briefing);
  const morning = yourMorning(briefing);
  const na = home?.needs_attention;
  const lateTotal = na?.status === "read" ? (na.late_total ?? (na.late_pos ?? []).length) : null;
  const total = (briefing?.status === "read" ? briefing.count : null) ?? groups.reduce((n, g) => n + g.count, 0);
  const loading = !error && groups.length === 0 && (!home || !briefing || late === null);
  const refreshing = briefing?.refreshing || na?.refreshing;
  const done = (r: ActionResult) => setStates((prev) => ({ ...prev, [r.card_id]: r }));
  const showDetail = !desktop && !!picked && !!selected;

  const tabs =
    bySupplier.length > 1 || (replies?.total ?? 0) > 0 ? (
      <div className="flex h-full min-w-0 gap-4 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden" role="tablist" aria-label="Show">
        {[{ type: "all", label: "All", count: bySupplier.reduce((n, g) => n + g.count, 0) }, ...VERBS.map((v) => ({ type: v.verb, label: v.label, count: v.verb === "reply" && replies ? replies.total : bySupplier.filter((g) => verbOf(g) === v.verb).reduce((n, g) => n + g.count, 0) })).filter((v) => v.count > 0)].map((g) => (
          <button
            key={g.type}
            type="button"
            role="tab"
            aria-selected={filter === g.type}
            onClick={() => setFilter(g.type)}
            className={cn(
              "relative flex h-full shrink-0 items-center gap-1.5 text-[13px] whitespace-nowrap cursor-pointer",
              filter === g.type ? "font-medium text-fg after:absolute after:inset-x-0 after:-bottom-px after:h-[2px] after:rounded-full after:bg-fg" : "text-muted hover:text-fg",
            )}
          >
            {g.label}
            <span className="text-[12px] font-normal tabular-nums text-faint">{g.count}</span>
          </button>
        ))}
      </div>
    ) : null;

  const list = (
    <section
      className={cn("flex min-h-0 min-w-0 flex-1 flex-col border-border bg-list lg:w-[368px] lg:flex-none lg:shrink-0 lg:border-r xl:w-[384px]", showDetail && "hidden")}
      aria-label="Needs action"
      data-testid="briefing"
    >
      {/* desktop toolbar: the title and the tabs on the line every pane's toolbar shares */}
      <header className="hidden h-[52px] shrink-0 items-stretch gap-5 border-b border-border px-5 lg:flex" data-testid="home-header">
        <h1 className="self-center text-[15px] font-semibold tracking-[-0.015em]">My work</h1>
        {tabs}
      </header>
      {/* phone: large title, as an iPhone app opens */}
      <header className="lg:hidden">
        <div className="flex h-11 items-center justify-end gap-1 px-3">
          <MobileSearch />
          <MobileAccount />
        </div>
        <h1 className="px-5 text-[30px] leading-9 font-bold tracking-[-0.025em]">My work</h1>
      </header>
      {/* the day and what is late: the phone's line under its title; said to screen readers on a desktop */}
      <p className="px-5 text-[13px] text-muted lg:sr-only" data-testid="headline">
        {new Date().toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", timeZone: TZ })}
        {lateTotal ? ` · ${lateTotal} late` : head && lateTotal === 0 ? ` · ${head.text}` : ""}
      </p>
      <div className="mt-3 flex h-10 shrink-0 border-b border-border px-5 lg:hidden">{tabs}</div>

      {supplier && (
        <div className="flex shrink-0 items-center gap-2 px-5 pt-3" data-testid="supplier-filter">
          <span className="inline-flex h-7 min-w-0 items-center gap-2 rounded-full bg-bg pr-1 pl-1 shadow-lift">
            <Monogram name={supplier} size={20} className="rounded-full" />
            <span className="truncate text-[12px] font-medium">{supplier}</span>
            <button type="button" onClick={() => router.push("/")} aria-label="Show every supplier" className="flex size-5 items-center justify-center rounded-full text-muted hover:bg-hover hover:text-fg cursor-pointer">
              <X className="size-3.5" aria-hidden />
            </button>
          </span>
        </div>
      )}

      <StaleNote since={home?.stale_since ?? briefing?.stale_since} className="px-5 py-1.5" />
      <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto pb-4" data-testid="needs-you" {...(!briefing || !suggestionsRead ? { "data-pending": "" } : {})}>
        {morning && (
          <div className="border-b border-border px-5 py-3" data-testid="your-morning">
            <h2 className="text-[13px] font-semibold">Your morning</h2>
            <p className="mt-0.5 text-[12px] text-muted">{morning.line}</p>
            {morning.items.map((i) => (
              <Link key={i.id} href={i.open_url} className="mt-2 block text-[13px] hover:underline" data-testid="your-morning-item">
                <span className="font-medium">{i.subject_label}</span>
                <span className="text-muted"> · {i.label}</span>
                {(i.why_yours ?? [])[0] && <span className="block text-[12px] text-faint">{(i.why_yours ?? [])[0]}</span>}
              </Link>
            ))}
          </div>
        )}
        <PreparedWork items={[]} />
        {error ? (
          <div className="p-4">
            <LoadProblem message={error} busy={busy} onRetry={() => load(true)} />
          </div>
        ) : loading ? (
          <ListSkeleton />
        ) : filter === "reply" ? (
          <div className="px-5 pt-3" data-testid="my-replies">
            {repliesError ? <LoadProblem message={plainError(repliesError)} busy={noticeKind(repliesError) === "busy"} onRetry={() => window.location.reload()} /> : replies ? <ReplyList triage={replies} /> : <Pending label="Reading your inbox" />}
          </div>
        ) : groups.length === 0 ? (
          <div className="px-5 py-10 text-[13px] text-muted">
            {briefingError ? (
              <LoadProblem message={briefingError} onRetry={loadBriefing} />
            ) : refreshing ? (
              <p data-testid="briefing-refreshing">{refreshingLine(briefing?.checked_on ?? na?.checked_on)}</p>
            ) : briefing && briefing.status !== "read" ? (
              <p data-testid="briefing-unavailable">{briefing.status === "not_configured" ? "The briefing hasn't run on this server." : "Couldn't load the briefing. Try again in a few minutes."}</p>
            ) : (
              <p className="text-fg">Nothing needs action.</p>
            )}
          </div>
        ) : (
          visible.map((g) => (
            <div key={g.type}>
              <div className="sticky top-0 z-10 flex h-9 items-end bg-list px-5 pb-1.5">
                <h2 className="text-[12px] font-medium text-muted">
                  {g.type === "po_late" && !supplier ? (
                    <Link href="/late" className="group inline-flex items-center gap-1 hover:text-fg" data-testid="stat-late" title="Every late PO">
                      {g.label} <span className="font-normal text-faint tabular-nums">{g.count}</span>
                      <ChevronRight className="size-3 text-faint opacity-0 transition-opacity group-hover:opacity-100" aria-hidden />
                    </Link>
                  ) : (
                    <>
                      {g.label} <span className="font-normal text-faint tabular-nums">{g.count}</span>
                    </>
                  )}
                </h2>
              </div>
              <ul className="px-2">
                {g.rows.map((r, n) => (
                  <Row key={r.key} row={r} on={r.key === selectedKey} onPick={() => pick(r.key)} actionState={r.suggestion ? states[r.suggestion.id] : undefined} testId={g.type === "po_late" && n === 0 && !supplier ? "stat-oldest" : undefined} />
                ))}
              </ul>
              {g.more && pages[g.type]?.more !== false && (
                <button type="button" onClick={() => more(g.type)} className="mx-5 mt-1 mb-2 text-[12px] font-medium text-muted hover:text-fg cursor-pointer">
                  Show more ({g.count - g.rows.length})
                </button>
              )}
            </div>
          ))
        )}
        {briefing?.status === "read" && briefing.count > 0 && groups.length > 0 && (
          <Link href="/watching#alerts" className="mx-5 mt-3 inline-flex items-center gap-1 text-[12px] font-medium text-muted hover:text-fg" data-testid="see-all">
            {typeof briefing.watching_count === "number" ? `${briefing.watching_count} more in Watching` : `All ${briefing.count} in Watching`}
            <ChevronRight className="size-3.5" aria-hidden />
          </Link>
        )}
        {refreshing && groups.length > 0 && <p className="px-4 pt-3 text-[12px] text-muted" data-testid="briefing-refreshing">{refreshingLine(briefing?.checked_on ?? na?.checked_on)}</p>}
        {(briefing?.test_records_hidden || na?.test_records_hidden) && groups.length > 0 && (
          <p className="px-4 pt-3 text-[12px] text-faint" data-testid="briefing-tests-hidden">
            {briefing?.test_records_hidden ?? na?.test_records_hidden}
          </p>
        )}
        {na?.status === "read" && <div className="px-4 pt-2"><HiddenNotice hidden={na?.hidden} /></div>}
      </div>
    </section>
  );

  return (
    <div className="flex h-full min-h-0 flex-1 lg:h-dvh">
      {list}
      {showDetail && selected && (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="sticky top-0 z-20 flex h-11 shrink-0 items-center border-b border-border bg-[color-mix(in_srgb,var(--bg)_88%,transparent)] px-1.5 backdrop-blur-xl lg:hidden">
            <button type="button" onClick={() => router.back()} className="flex h-9 items-center gap-0.5 pr-2 text-[15px] text-accent cursor-pointer">
              <ChevronLeft className="size-6" strokeWidth={2} aria-hidden />
              Today
            </button>
            <span className="ml-auto pr-3 text-[13px] text-muted">{selected.title}</span>
          </div>
          <TodayDetail row={selected} state={selected.suggestion ? states[selected.suggestion.id] : null} onDone={done} />
        </div>
      )}
      {desktop && (
        <div className="hidden min-w-0 flex-1 lg:flex">
          {selected ? (
            <TodayDetail key={selected.key} row={selected} state={selected.suggestion ? states[selected.suggestion.id] : null} onDone={done} />
          ) : (
            <div className="flex flex-1 flex-col">
              <div className="h-[52px] shrink-0 border-b border-border" />
              <div className="flex flex-1 items-center justify-center text-[13px] text-faint">{loading ? "" : "Nothing selected"}</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function HomePage() {
  return (
    <Suspense>
      <Today />
    </Suspense>
  );
}
