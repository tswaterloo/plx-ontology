"use client";

import { useReaderWords } from "@/components/view-as";
import { ChevronLeft, ChevronRight, X } from "lucide-react";
import Link from "next/link";
import { PageIntro } from "@/components/ui/page-intro";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { HiddenNotice } from "@/components/answer-parts";
import { ALERTS_HREF, LATE_HREF, loadHome, loadLateRead, MobileAccount, MobileSearch, publishNeedsCount, SectionTabs } from "@/components/shell";
import { ReplyCard, ReplyList } from "@/components/reply-list";
import type { QueueRow } from "@/components/today-detail";
import dynamic from "next/dynamic";
import { LIMIT_MS, Pending, SlowNote, TimedOut, useSlow } from "@/components/ui/skeleton";
import { lateEdge, lateFigure, OpenRing } from "@/components/ui/glyphs";
import { FeedNote } from "@/components/feed-note";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { NeedsMe } from "@/components/needs-me";
import { HomeAsk } from "@/components/home-start";
import { AssistQueue, type PreparedCount } from "@/components/home/assist-queue";
import { YourCommitments } from "@/components/your-commitments";
import { InsightNewsList } from "@/components/insights/news";
import { YourTurn } from "@/components/memory-people";
import { SignedInFirstWeek } from "@/components/first-week";
import { isPutAway } from "@/lib/actions";
import { useStaleRefreshOnReturn } from "@/lib/stale-refresh";
import { getActionStates, getReplies, onHomeBundleFresh, startHomeBundle, getBriefing, getBriefingItems, getSuggestions, markBriefingSeen } from "@/lib/api";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { replyLine, withoutCarried } from "@/components/briefing";
import { formatDate, formatMoney, objectHref, readable, recordLabel, safeHref } from "@/lib/format";
import { headline, keyOwner, refreshingLine } from "@/lib/home";
import type { ReplyTriage, ActionResult, Briefing, BriefingItem, HomeResponse, LatePo, ProactiveSuggestion } from "@/lib/types";

// Parts Home does not draw at first paint load as their own chunks: the detail pane (drawn once
// the briefing is read, so its code is fetched on mount, beside those reads) and the Alerts and
// Late POs tabs (fetched when the browser is idle). Home's first JavaScript is that much less.
const loadDetail = () => import("@/components/today-detail");
const loadAlerts = () => import("@/components/home/alerts-view");
const loadLate = () => import("@/components/home/late-view");
const TodayDetail = dynamic(() => loadDetail().then((m) => m.TodayDetail), { loading: () => <Pending label="Opening" /> });
const AlertsView = dynamic(() => loadAlerts().then((m) => m.AlertsView), { loading: () => <Pending label="Reading the alerts" /> });
const LateView = dynamic(() => loadLate().then((m) => m.LateView), { loading: () => <Pending label="Reading the late POs" /> });
function usePreloadHomeParts() {
  useEffect(() => {
    void loadDetail();
    const idle = () => void Promise.all([loadAlerts(), loadLate()]).catch(() => undefined);
    const ric = (window as Window & { requestIdleCallback?: (f: () => void) => number }).requestIdleCallback;
    if (ric) ric(idle);
    else setTimeout(idle, 1500);
  }, []);
}

const SEEN_AFTER_MS = 6000; // the visit is marked seen after this at the latest, cards settled or not
const TZ = "America/Toronto";

/** "Couldn't load {thing}." with the incident kept for the Reference line (the notice carries the retry). */
function didntLoad(thing: string, err: unknown): string {
  const msg = plainError(err);
  if (/^The Workbench server failed/.test(msg)) {
    const inc = /incident ([0-9a-f]+)/.exec(msg)?.[1];
    return `Couldn't load ${thing}.${inc ? ` (incident ${inc})` : ""}`;
  }
  return msg;
}

/** The list's groups, most urgent first; a kind the app doesn't know sorts before "no due date". */
/** Alerts of one kind Home lists before "Show N more": it stays a short list of what needs
 * you (a kind with dozens open would bury the rest); every one is in Alerts. */
export const HOME_ROWS = 3;
const ORDER = ["po_late", "negative_margin", "customer_order_at_risk", "po_due", "eta_slipped", "vendor_no_reply", "vendor_terms_unapplied", "quantity_mismatch", "lot_on_hold", "lot_near_expiry", "quote_accepted_no_order"];
const SHORT_GROUP: Record<string, string> = { po_late: "Late", po_undated: "No date", po_due: "Due soon", lot_on_hold: "Quality holds" };
/** The alert kind a group's items belong to (the registry's), if its rows say. */
const kindOfGroup = (g: { rows: QueueRow[] }): string | null => g.rows.find((r) => r.item?.alert_kind)?.item?.alert_kind ?? null;
/** My work's chips: what the reader does about an item. */
const VERBS = [
  { verb: "confirm", label: "Confirm" },
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
const rankOf = (t: string) => (t === "po_undated" ? 999 : ORDER.indexOf(t) === -1 ? 500 : ORDER.indexOf(t));

/** A late PO's supplier date, in the row's few words. */
const CONFIRM_WORDS: Record<string, string> = { confirmed: "supplier confirmed", none_found: "no supplier date", found_unverified: "date unverified", needs_check: "check the confirmation" };

function lateRow(p: LatePo, suggestion?: ProactiveSuggestion, item?: BriefingItem): QueueRow {
  return {
    key: p.object_id,
    group: "po_late",
    type: "PurchaseOrder",
    objectId: p.object_id,
    title: recordLabel("PurchaseOrder", p.label),
    counterpart: p.supplier ?? null,
    line: [
      p.due_date ? `Needed ${formatDate(p.due_date, { short: true })}` : "No due date",
      p.amount && !p.amount_suspect ? formatMoney(p.amount.amount, p.amount.currency) : p.amount_suspect ? "check the total" : null,
      CONFIRM_WORDS[p.confirmation ?? ""] ?? null,
    ].filter(Boolean).join(" · "),
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
  const plain = withoutCarried(readable(i.summary));
  const summary = replyLine(plain) ?? plain.replace(/^An open purchase order has no due date recorded$/i, "Open, no due date");
  return {
    key: i.object_id,
    group: i.event_type,
    type,
    objectId: i.object_id,
    title: recordLabel(type, i.subject_label ?? ""),
    counterpart: i.counterpart ?? null,
    line: i.section ? summary : [summary, amount].filter(Boolean).join(" · "),
    figure: i.section ? amount : null,
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
    <li className="cv-auto cv-row" data-testid={testId}>
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
          "flex gap-3 rounded-[3px] px-3 py-2.5 outline-none transition-[background-color,box-shadow] duration-100",
          row.status === "late" && row.days != null && lateEdge(row.days),
          on ? "bg-row-on shadow-[var(--lift),inset_2px_0_0_var(--accent)]" : "hover:bg-hover",
          "focus-visible:ring-2 focus-visible:ring-ring/50",
          away && "opacity-60",
        )}
      >
        <span className="min-w-0 flex-1">
          <span className="flex items-baseline gap-2">
            <span className="num shrink-0 text-[13px] font-semibold leading-[18px]">{row.title}</span>
            {row.counterpart && <span className="min-w-0 truncate text-[13px] leading-[18px] text-fg/80">{row.counterpart}</span>}
            <span className="ml-auto flex shrink-0 items-center gap-1.5 self-center pl-2 text-[12px] leading-4 tabular-nums">
              {row.days != null ? (
                <>
                  <span className={cn("num font-medium", row.status === "late" ? lateFigure(row.days) : "text-fg")}>{row.days}d</span>
                </>
              ) : row.group === "po_undated" ? (
                <>
                  <span className="text-muted">No date</span>
                  <OpenRing size={13} />
                </>
              ) : row.figure ? (
                <span className="num font-medium text-fg">{row.figure}</span>
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

function ListSkeleton({ onRetry }: { onRetry: () => void }) {
  const over = useSlow(LIMIT_MS);
  if (over) return <TimedOut label="Reading your work" onRetry={onRetry} className="px-5 pt-3" />;
  return (
    <>
    <SlowNote className="px-5 pt-3" />
    <ul className="space-y-1 px-2 pt-2" aria-busy="true" aria-label="Loading your work">
      {Array.from({ length: 9 }).map((_, i) => (
        <li key={i} className="flex gap-3 px-3 py-2.5">
          <span className="skeleton size-8 rounded-[3px]" />
          <span className="flex-1 space-y-2">
            <span className="skeleton block h-3" style={{ width: `${62 - (i % 3) * 9}%` }} />
            <span className="skeleton block h-2.5" style={{ width: `${40 - (i % 2) * 8}%` }} />
          </span>
        </li>
      ))}
    </ul>
    </>
  );
}

function Today() {
  const w = useReaderWords();
  const router = useRouter();
  const params = useSearchParams();
  const desktop = useDesktop();
  const [home, setHome] = useState<HomeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [briefingError, setBriefingError] = useState<string | null>(null);
  const [late, setLate] = useState<LatePo[] | null>(null);
  // every late order the server counts, which can be more than Today reads (the rest are on Late orders)
  const [lateAll, setLateAll] = useState<number | null>(null);
  const [suggestions, setSuggestions] = useState<ProactiveSuggestion[]>([]);
  const supplier = params.get("supplier");
  const [states, setStates] = useState<Record<string, ActionResult>>({});
  const [pages, setPages] = useState<Record<string, { items: BriefingItem[]; more: boolean; total: number }>>({});
  const [opened, setOpened] = useState<Record<string, boolean>>({});
  const [filter, setFilter] = useState<string>(params.get("view") === "reply" ? "reply" : "all");
  // the Reply chip's list: your own mail and chats waiting on your answer (inbox triage)
  const [replies, setReplies] = useState<ReplyTriage | null>(null);
  const [repliesError, setRepliesError] = useState<unknown>(null);
  // Home's first reads come in ONE answer (/api/home/bundle), the last one kept in this tab shown
  // at once: this effect comes before every read below. When the fresh bundle arrives after
  // the kept one was shown, each read is made again (answered from the fresh bundle).
  const [bundleRound, setBundleRound] = useState(0);
  // a layout effect: it runs before any child's read effect (needs-me's approvals read was
  // sent on its own, ahead of the bundle that carries it)
  useLayoutEffect(() => {
    startHomeBundle();
    return onHomeBundleFresh(() => setBundleRound((n) => n + 1));
  }, []);
  useEffect(() => {
    const c = new AbortController();
    getReplies(c.signal).then(setReplies, (e) => !c.signal.aborted && setRepliesError(e));
    return () => c.abort();
  }, [bundleRound]);
  // how many items the queue holds (AssistQueue reports it once read; null until then or on a failed read)
  const [queued, setQueued] = useState<PreparedCount | null>(null);
  const [picked, setPicked] = useState<string | null>(params.get("sel"));
  const [replyPick, setReplyPick] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const load = useCallback((fresh = false) => {
    const c = new AbortController();
    setError(null);
    loadHome(fresh)
      .then((h) => !c.signal.aborted && setHome(h))
      .catch((e) => {
        if (c.signal.aborted) return;
        setBusy(noticeKind(e) === "busy");
        setError(didntLoad("your work", e));
      });
    return () => c.abort();
  }, []);
  useEffect(() => load(bundleRound > 0), [load, bundleRound]);
  // a stale answer is asked for again from the server, not from the read already held
  const reloadHome = useCallback(() => void load(true), [load]);
  useStaleRefreshOnReturn(home?.stale_since, reloadHome);

  // The visit is marked seen once the briefing is read AND the suggestion cards have settled
  // (or SEEN_AFTER_MS passed): the mark makes the kept briefing and suggestions stale, so a
  // mark sent first sent the cards' own read back to the server to be made again behind a poll
  // the page then waited on.
  const briefingRead = useRef(false);
  const suggestionsSettled = useRef(false);
  const seenSent = useRef(false);
  const markSeen = useCallback(() => {
    if (!briefingRead.current || !suggestionsSettled.current || seenSent.current) return;
    seenSent.current = true;
    void markBriefingSeen().catch(() => undefined);
  }, []);
  useEffect(() => {
    const t = setTimeout(() => {
      suggestionsSettled.current = true;
      markSeen();
    }, SEEN_AFTER_MS);
    return () => {
      clearTimeout(t);
      // leaving before the cards settled still counts as the visit: the briefing was read
      suggestionsSettled.current = true;
      markSeen();
    };
  }, [markSeen]);

  const loadBriefing = useCallback(() => {
    const c = new AbortController();
    setBriefingError(null);
    getBriefing(c.signal, "all", true)
      .then((b) => {
        setBriefing(b);
        // each group's next page comes with the briefing (the server picks the kinds Home
        // can show: only the kinds sent home, of each only HOME_ROWS before "Show more"):
        // no second request, so the visit is marked seen at once (pages are cut from the
        // briefing the server kept, and the mark makes that stale)
        if (b.pages?.length)
          setPages((cur) => {
            const next = { ...cur };
            for (const p of b.pages ?? []) next[p.event_type] = { items: p.items ?? [], more: !!p.more, total: p.total ?? 0 };
            return next;
          });
        if (b.status === "read") {
          briefingRead.current = true;
          markSeen();
        }
      })
      .catch((e) => !c.signal.aborted && setBriefingError(didntLoad("the briefing", e)));
    return () => c.abort();
  }, [markSeen]);
  useEffect(() => loadBriefing(), [loadBriefing, bundleRound]);

  useEffect(() => {
    const c = new AbortController();
    loadLateRead(bundleRound > 0).then(
      (l) => {
        if (c.signal.aborted) return;
        setLate(l.items);
        setLateAll(l.total);
      },
      () => !c.signal.aborted && setLate([]),
    );
    // the suggestion cards only add to rows already listed ("ETA confirmed?", a draft): the
    // list never waits for them (their first read can take a minute on a cold server)
    getSuggestions(c.signal)
      .then((s) => !c.signal.aborted && setSuggestions(s.status === "read" ? (s.items ?? []) : []), () => undefined)
      .finally(() => {
        suggestionsSettled.current = true;
        markSeen();
      });
    getActionStates(c.signal).then((r) => setStates(r.cards ?? {}), () => undefined);
    return () => c.abort();
  }, [markSeen, bundleRound]);

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
    const out: { type: string; label: string; count: number; rows: QueueRow[]; more: boolean; section?: string }[] = [];
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
      // a gathered section lists its biggest few as the briefing sent them, never pages of the rest
      const rows = [...top.filter((i) => i.event_type === g.event_type), ...(g.section ? [] : (pages[g.event_type]?.items ?? []))]
        .filter((i) => !homeOnly || i.home)
        .filter((i) => (seen.has(i.id) ? false : (seen.add(i.id), true)))
        .map((i) => itemRow(i, bySubject.get(i.object_id)));
      // the group's count is every alert of its kind firing for you (the count Alerts shows),
      // never just the rows read so far: Home said 25 below cost while Alerts said 65
      if (rows.length) out.push({ type: g.event_type, label: SHORT_GROUP[g.event_type] ?? g.label, count: Math.max(g.count, rows.length), rows, more: rows.length < g.count && !g.section, section: g.section ?? undefined });
    }
    return out.sort((a, b) => rankOf(a.type) - rankOf(b.type));
  }, [briefing, late, home, suggestions, pages]);

  // what needs a person, one item each; the groups the registry gathers into a section
  // (Financials: a count and the biggest few, the full list in Alerts) stand apart from it
  const needsGroups = useMemo(() => groups.filter((g) => !g.section), [groups]);
  const financialGroups = useMemo(() => groups.filter((g) => g.section === "financials"), [groups]);
  // a kind with prepared cards of its own (a hold that blocks an order) lists them in the
  // kind's one section, first, not in a second section beside it
  const [heldN, setHeldN] = useState<Record<string, number>>({});
  const mergedKinds = useMemo(() => (supplier ? [] : [...new Set(needsGroups.map(kindOfGroup).filter((k): k is string => !!k))]), [needsGroups, supplier]);
  const bySupplier = supplier
    ? needsGroups.map((g) => ({ ...g, rows: g.rows.filter((r) => r.counterpart === supplier) })).filter((g) => g.rows.length).map((g) => ({ ...g, count: g.rows.length, more: false }))
    : needsGroups;
  const capped = bySupplier.map((g) =>
    g.type === "po_late" || opened[g.type] || g.rows.length <= HOME_ROWS ? { ...g, folded: 0 } : { ...g, rows: g.rows.slice(0, HOME_ROWS), folded: g.rows.length - HOME_ROWS },
  );
  const visible = filter === "all" ? capped : capped.filter((g) => verbOf(g) === filter);
  const financialShown = useMemo(() => (filter === "all" && !supplier ? financialGroups : []), [filter, supplier, financialGroups]);
  const flat = useMemo(() => [...visible, ...financialShown].flatMap((g) => g.rows), [visible, financialShown]);
  const selectedKey = picked ?? (desktop ? flat[0]?.key ?? null : null);
  const selected = flat.find((r) => r.key === selectedKey) ?? groups.flatMap((g) => g.rows).find((r) => r.key === selectedKey) ?? null;

  // a desktop's detail pane shows the chosen message when replies are listed; with no row
  // chosen and nothing else to open, the first message opens by itself
  const replyLines = desktop && !supplier && (filter === "all" || filter === "reply") ? (replies?.lines ?? []) : [];
  const replyShown = replyLines.find((l) => l.id === replyPick) ?? (replyLines.length && (filter === "reply" || !selected) ? replyLines[0] : null);
  const shownRow = replyShown ? null : selected;

  const pick = useCallback(
    (key: string) => {
      setReplyPick(null);
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
      if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
      const t = e.target;
      // a link, button, field or open dialog keeps its own keys (Enter on a focused link opens that link)
      const owner = keyOwner(t);
      if (owner === "own") return;
      const rowLink = owner === "row" && t instanceof Element ? t.closest("[data-row-link]") : null;
      if (!flat.length || !desktop) return;
      const at = flat.findIndex((r) => r.key === selectedKey);
      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        pick(flat[Math.min(flat.length - 1, at + 1)].key);
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        pick(flat[Math.max(0, at - 1)].key);
      } else if (e.key === "Enter" && (rowLink || selected)) {
        e.preventDefault();
        // on a focused row, Enter opens that row's own record
        router.push(rowLink?.getAttribute("href") ?? selected!.href);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [flat, selectedKey, selected, desktop, pick, router]);
  useEffect(() => {
    listRef.current?.querySelector('[aria-current="true"]')?.scrollIntoView({ block: "nearest" });
  }, [selectedKey]);

  const head = headline(home, briefing);
  const na = home?.needs_attention;
  const lateTotal = na?.status === "read" ? (na.late_total ?? (na.late_pos ?? []).length) : null;
  // the late orders, replies and prepared work show as soon as they are read; the alerts
  // (a slower first read) say they are still being read, never a page of skeleton rows
  const loading = !error && groups.length === 0 && (!home || late === null);
  const alertsReading = !briefing && !briefingError && !error;
  const refreshing = briefing?.refreshing || na?.refreshing;
  // drafts to confirm count under Confirm; alerts waiting on you (nothing to confirm) under Decide
  const prepared = queued?.confirm ?? 0;
  const preparedAlerts = queued?.alerts ?? 0;
  // replies or prepared items listed below: no "nothing needs action" line above them
  const othersListed = filter === "all" && !supplier && ((replies?.total ?? 0) > 0 || prepared + preparedAlerts > 0);
  // the one count, shared with the sidebar: late, alerts, replies you owe and prepared items;
  // unknown (null) until every part is read, and while any part failed (never a partial number)
  const everyRead = !loading && !error && !!briefing && briefing.status === "read" && late !== null && replies !== null && !repliesError && queued !== null;
  const total = everyRead ? needsGroups.reduce((n, g) => n + g.count, 0) + (replies?.total ?? 0) + prepared + preparedAlerts : null;
  useEffect(() => {
    if (!supplier) publishNeedsCount(total);
  }, [total, supplier]);
  const done = (r: ActionResult) => setStates((prev) => ({ ...prev, [r.card_id]: r }));
  const showDetail = !desktop && !!picked && !!selected;
  // nothing to open beside the list: it takes the whole width
  const wide = desktop && !loading && !shownRow && !replyShown;

  const verbChips = VERBS.map((v) => ({ type: v.verb, label: v.label, count: v.verb === "confirm" ? (supplier ? 0 : prepared) : v.verb === "reply" && replies ? replies.total : bySupplier.filter((g) => verbOf(g) === v.verb).reduce((n, g) => n + g.count, 0) + (v.verb === "decide" && !supplier ? preparedAlerts : 0) })).filter((v) => v.count > 0);
  // one kind of thing waiting: "All 8" beside "Decide 8" is the same list twice, so no chips then
  const tabs =
    verbChips.length > 1 || (verbChips.length > 0 && filter !== "all") ? (
      <div className="flex h-full min-w-0 gap-4 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden" role="tablist" aria-label="Show">
        {[{ type: "all", label: "All", count: supplier ? (loading ? null : bySupplier.reduce((n, g) => n + g.count, 0)) : total }, ...verbChips].map((g) => (
          <button
            key={g.type}
            type="button"
            role="tab"
            aria-selected={filter === g.type}
            onClick={() => setFilter(g.type)}
            className={cn(
              "relative flex h-full shrink-0 items-center gap-1.5 text-[13px] whitespace-nowrap cursor-pointer",
              filter === g.type ? "font-medium text-fg after:absolute after:inset-x-0 after:-bottom-px after:h-[2px] after:bg-accent" : "text-muted hover:text-fg",
            )}
          >
            {g.label}
            {/* no count before the list is read: never "All 0" then the items (owner walk 11-17) */}
            {g.count !== null && <span className="text-[12px] font-normal num text-faint">{g.count}</span>}
          </button>
        ))}
      </div>
    ) : null;

  const list = (
    <section
      className={cn("flex min-h-0 min-w-0 flex-1 flex-col border-border bg-list", wide ? "lg:max-w-none" : "lg:w-[368px] lg:flex-none lg:shrink-0 lg:border-r xl:w-[384px]", showDetail && "hidden")}
      aria-label="Needs action"
      data-testid="briefing"
    >
      {/* desktop toolbar: the title and the tabs on the line every pane's toolbar shares */}
      <header className="hidden h-[52px] shrink-0 items-stretch gap-5 border-b border-border px-5 lg:flex" data-testid="home-header">
        <h1 className="self-center text-[15px] font-semibold tracking-[-0.015em]">Home</h1>
        {tabs}
      </header>
      {/* phone: large title, as an iPhone app opens */}
      <header className="lg:hidden">
        <div className="flex h-11 items-center justify-end gap-1 px-3">
          <MobileSearch />
          <MobileAccount />
        </div>
        <h1 className="px-5 text-[30px] leading-9 font-bold tracking-[-0.025em]">Home</h1>
      </header>
      <SectionTabs section="home" />
      {/* the day and what is late: the phone's line under its title; said to screen readers on a desktop */}
      <p className="px-5 text-[13px] text-muted lg:sr-only" data-testid="headline">
        {new Date().toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", timeZone: TZ })}
        {lateTotal ? ` · ${lateTotal} late` : head && lateTotal === 0 ? ` · ${head.text}` : ""}
      </p>
      <PageIntro path="/" className="px-5 pt-1 lg:px-5 lg:pt-3" />
      {tabs ? <div className="mt-3 flex h-10 shrink-0 border-b border-border px-5 lg:hidden">{tabs}</div> : <div className="h-3 lg:hidden" aria-hidden />}

      {supplier && (
        <div className="flex shrink-0 items-center gap-2 px-5 pt-3" data-testid="supplier-filter">
          <span className="inline-flex h-7 min-w-0 items-center gap-2 rounded-[3px] bg-bg pr-1 pl-2 shadow-lift">
            <span className="truncate text-[12px] font-medium">{supplier}</span>
            <button type="button" onClick={() => router.push("/")} aria-label="Show every supplier" className="flex size-5 items-center justify-center rounded text-muted hover:bg-hover hover:text-fg cursor-pointer">
              <X className="size-3.5" aria-hidden />
            </button>
          </span>
        </div>
      )}

      {!supplier && <HomeAsk className="shrink-0 px-5 pt-3" />}
      <StaleNote since={home?.stale_since ?? briefing?.stale_since} onReturn={!!home?.stale_since} className="shrink-0 px-5 py-1.5" />
      <FeedNote feed="reply_drafts" className="shrink-0 px-5 py-1.5 text-[12px] text-risk" />
      <FeedNote feed="autopilot" className="shrink-0 px-5 py-1.5 text-[12px] text-risk" />
      <FeedNote feed="late_pos" className="shrink-0 px-5 py-1.5 text-[12px] text-risk" />
      <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto pb-4" data-testid="needs-you" {...(!briefing && !briefingError && !error ? { "data-pending": "" } : {})}>
        <SignedInFirstWeek />
        {/* prepared work is its own chip (Confirm): listed under All and Confirm, never under
            another verb, so All is the sum of the chips; kept mounted so its count stays read */}
        {!supplier && !error && (
          <div hidden={filter !== "all" && filter !== "confirm" && filter !== "decide"}>
            <AssistQueue onCount={setQueued} except={mergedKinds} only={filter === "confirm" ? "confirm" : filter === "decide" ? "alerts" : undefined} />
          </div>
        )}
        {/* sections no chip counts: under All only (a chip lists what its count says) */}
        {filter === "all" && !supplier && (
          <>
            <NeedsMe />
            <YourCommitments />
            <InsightNewsList />
            <YourTurn />
          </>
        )}
        {error ? (
          <div className="p-4">
            <LoadProblem message={error} busy={busy} onRetry={() => load(true)} />
          </div>
        ) : loading ? (
          <ListSkeleton onRetry={() => window.location.reload()} />
        ) : filter === "reply" ? (
          <div className="px-5 pt-3" data-testid="my-replies">
            {repliesError ? <LoadProblem message={plainError(repliesError)} busy={noticeKind(repliesError) === "busy"} onRetry={() => window.location.reload()} /> : replies ? <ReplyList triage={replies} selectedId={replyShown?.id} onSelect={desktop ? setReplyPick : undefined} states={states} onDone={done} /> : <Pending label="Reading your inbox" />}
          </div>
        ) : needsGroups.length === 0 && !(othersListed && !briefingError && briefing?.status === "read") ? (
          <div className="px-5 py-10 text-[13px] text-muted">
            {briefingError ? (
              <LoadProblem message={briefingError} onRetry={loadBriefing} />
            ) : alertsReading ? (
              <Pending label="Reading your alerts" testId="alerts-reading" />
            ) : refreshing ? (
              <p data-testid="briefing-refreshing">{refreshingLine(briefing?.checked_on ?? na?.checked_on)}</p>
            ) : briefing && briefing.status !== "read" ? (
              <p data-testid="briefing-unavailable">{briefing.status === "not_configured" ? "The briefing hasn't run on this server." : "Couldn't load the briefing. Try again in a few minutes."}</p>
            ) : (
              total === 0 ? <p className="text-fg" data-testid="home-empty">Nothing needs you right now.</p> : null
            )}
          </div>
        ) : (
          visible.map((g) => (
            <div key={g.type}>
              <div className="sticky top-0 z-10 flex h-9 items-end bg-list px-5 pb-1.5">
                <h2 className="text-[12px] font-medium text-muted">
                  {g.type === "po_late" && !supplier ? (
                    <Link href={LATE_HREF} className="group inline-flex items-center gap-1 hover:text-fg" data-testid="stat-late" title="Every late PO">
                      {g.label} <span className="font-normal text-faint num">{g.count}</span>
                      <ChevronRight className="size-3 text-faint opacity-0 transition-opacity group-hover:opacity-100" aria-hidden />
                    </Link>
                  ) : (
                    <>
                      {g.label} <span className="font-normal text-faint num">{g.count + (mergedKinds.includes(kindOfGroup(g) ?? "") ? (heldN[kindOfGroup(g) ?? ""] ?? 0) : 0)}</span>
                    </>
                  )}
                </h2>
              </div>
              {!supplier && (() => {
                const kind = kindOfGroup(g);
                if (!kind || needsGroups.find((x) => kindOfGroup(x) === kind)?.type !== g.type) return null;
                const held = heldN[kind] ?? 0;
                return (
                  <div hidden={filter !== "all" && filter !== "decide"}>
                    {held > 0 && <p className="px-5 pt-0.5 text-[12px] text-muted">Holding up an order</p>}
                    <div className="px-5">
                      <AssistQueue kind={kind} onKind={(n) => setHeldN((x) => (x[kind] === n ? x : { ...x, [kind]: n }))} />
                    </div>
                    {held > 0 && <p className="px-5 pt-2 text-[12px] text-muted">Not holding an order we found</p>}
                  </div>
                );
              })()}
              <ul className="px-2">
                {g.rows.map((r, n) => (
                  <Row key={r.key} row={r} on={!replyShown && r.key === selectedKey} onPick={() => pick(r.key)} actionState={r.suggestion ? states[r.suggestion.id] : undefined} testId={g.type === "po_late" && n === 0 && !supplier ? "stat-oldest" : undefined} />
                ))}
              </ul>
              {g.type === "po_late" && late && lateAll !== null && lateAll > late.length && (
                <Link href={LATE_HREF} className="mx-5 mt-1 mb-2 block text-[12px] font-medium text-muted hover:text-fg" data-testid="late-partial">
                  Showing the first {late.length} of {lateAll} late orders; all of them are on Late orders
                </Link>
              )}
              {g.folded > 0 && (
                <button
                  type="button"
                  onClick={() => {
                    setOpened((x) => ({ ...x, [g.type]: true }));
                    if (!pages[g.type] && (briefing?.groups ?? []).some((b) => b.event_type === g.type && b.count > (b.in_top ?? 0))) more(g.type);
                  }}
                  className="mx-5 mt-1 mb-2 text-[12px] font-medium text-muted hover:text-fg cursor-pointer"
                  data-testid="home-folded"
                >
                  Show {Math.max(g.folded, g.count - g.rows.length)} more
                </button>
              )}
              {g.folded === 0 && g.more && pages[g.type]?.more !== false && (
                <button type="button" onClick={() => more(g.type)} className="mx-5 mt-1 mb-2 text-[12px] font-medium text-muted hover:text-fg cursor-pointer">
                  Show more ({g.count - g.rows.length})
                </button>
              )}
            </div>
          ))
        )}
        {filter === "all" && !supplier && !error && !loading && financialGroups.length > 0 && (
          <section aria-label="Financials" data-testid="home-financials">
            <div className="sticky top-0 z-10 flex h-9 items-end bg-list px-5 pb-1.5">
              <h2 className="text-[12px] font-medium text-muted">
                Financials <span className="font-normal text-faint num">{financialGroups.reduce((n, g) => n + g.count, 0)}</span>
              </h2>
            </div>
            {financialGroups.map((g) => {
              const kind = g.rows.find((r) => r.item?.alert_kind)?.item?.alert_kind;
              return (
                <div key={g.type} className="pb-2" data-testid={`financials-${g.type}`}>
                  <div className="flex items-baseline gap-2 px-5 pt-1 text-[12px] text-muted">
                    <span className="text-fg/80">{g.label}</span>
                    <span className="num text-faint">{g.count}</span>
                    <Link href={kind ? `${ALERTS_HREF}#kind-${kind}` : ALERTS_HREF} className="ml-auto font-medium hover:text-fg">
                      {g.count > g.rows.length ? `All ${g.count}` : "In Alerts"}
                    </Link>
                  </div>
                  <ul className="px-2">
                    {g.rows.map((r) => (
                      <Row key={r.key} row={r} on={!replyShown && r.key === selectedKey} onPick={() => pick(r.key)} actionState={r.suggestion ? states[r.suggestion.id] : undefined} />
                    ))}
                  </ul>
                </div>
              );
            })}
          </section>
        )}
        {alertsReading && groups.length > 0 && <Pending label="Reading your alerts" className="px-5 pt-2" testId="alerts-reading" />}
        {briefing?.status === "read" && briefing.count > 0 && briefing.watching_count !== 0 && groups.length > 0 && (
          <Link href={`${ALERTS_HREF}#alerts`} className="mx-5 mt-3 inline-flex items-center gap-1 text-[12px] font-medium text-muted hover:text-fg" data-testid="see-all">
            {typeof briefing.watching_count === "number" ? `${briefing.watching_count} more in Alerts` : `All ${briefing.count} in Alerts`}
            <ChevronRight className="size-3.5" aria-hidden />
          </Link>
        )}
        {(briefing?.test_records_hidden || na?.test_records_hidden) && groups.length > 0 && (
          <p className="px-4 pt-3 text-[12px] text-faint" data-testid="briefing-tests-hidden">
            {briefing?.test_records_hidden ?? na?.test_records_hidden}
          </p>
        )}
        {na?.status === "read" && <div className="px-4 pt-2"><HiddenNotice hidden={na?.hidden} /></div>}
        {filter === "all" && !supplier && !error && !loading && (
          <>
            {replies && replies.total > 0 && (
              <div className="px-5 pt-3" data-testid="home-replies">
                <h2 className="pb-1 text-[12px] font-medium text-muted">
                  Reply <span className="font-normal text-faint num">{replies.total}</span>
                </h2>
                <ReplyList triage={replies} selectedId={replyShown?.id} onSelect={desktop ? setReplyPick : undefined} states={states} onDone={done} />
              </div>
            )}
            {repliesError && <div className="px-5 pt-3"><LoadProblem message={plainError(repliesError)} busy={noticeKind(repliesError) === "busy"} onRetry={() => window.location.reload()} /></div>}
          </>
        )}
      </div>
    </section>
  );

  return (
    <div className="flex h-full min-h-0 flex-1 lg:h-dvh">
      {list}
      {showDetail && selected && (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="sticky top-0 z-20 flex h-11 shrink-0 items-center border-b border-border bg-bg px-1.5 lg:hidden">
            {/* back to the list; a link opened straight to an item has no list behind it, so go to it */}
            <button type="button" onClick={() => (window.history.length > 1 ? router.back() : router.replace("/"))} className="flex h-9 items-center gap-0.5 pr-2 text-[15px] text-accent cursor-pointer">
              <ChevronLeft className="size-6" strokeWidth={2} aria-hidden />
              Home
            </button>
            <span className="ml-auto pr-3 text-[13px] text-muted">{selected.title}</span>
          </div>
          <TodayDetail row={selected} state={selected.suggestion ? states[selected.suggestion.id] : null} onDone={done} />
        </div>
      )}
      {desktop && !wide && (
        <div className="hidden min-w-0 flex-1 lg:flex">
          {replyShown ? (
            <div className="flex min-w-0 flex-1 flex-col" data-testid="reply-detail">
              <div className="h-[52px] shrink-0 border-b border-border" />
              <div className="min-h-0 flex-1 overflow-y-auto px-8 py-6">
                <ReplyCard key={replyShown.id} className="max-w-xl" line={replyShown} state={replyShown.card_id ? (states[replyShown.card_id] ?? null) : null} onDone={done} />
              </div>
            </div>
          ) : selected ? (
            <TodayDetail key={selected.key} row={selected} state={selected.suggestion ? states[selected.suggestion.id] : null} onDone={done} />
          ) : (
            <div className="flex flex-1 flex-col">
              <div className="h-[52px] shrink-0 border-b border-border" />
              <div className="flex-1 overflow-y-auto px-8 py-8">
                {!loading && !error && visible.length > 0 && <p className="mt-8 text-[12px] text-faint">Choose an item on the left to open it here.</p>}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Home is one page with three tabs (the owner, 11-18: simpler tabs): what needs you, the
 * alerts (?show=alerts, was /watching) and the late POs (?show=late, was /late). */
function HomeTab() {
  usePreloadHomeParts();
  const show = useSearchParams()?.get("show");
  if (show === "alerts") return <AlertsView />;
  if (show === "late") return <LateView />;
  return <Today />;
}

export default function HomePage() {
  return (
    <Suspense>
      <HomeTab />
    </Suspense>
  );
}
