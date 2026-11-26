"use client";

import {
  Check,
  ChevronDown,
  ChevronRight,
  ClipboardList,
  Clock3,
  DollarSign,
  Mail,
  RefreshCw,
  RotateCcw,
  Server,
  ShieldAlert,
  X,
} from "lucide-react";
import dynamic from "next/dynamic";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "@/components/auth";
import {
  dedupeNeedItems,
  DUE_ORDER,
  DUE_WORDS,
  dueValue,
  financialsSummaryLabel,
  isDecisionOrHoldItem,
  isFinancialsItem,
  isHoldItem,
  isLateSupplyItem,
  isOpsSystemNotice,
  sectionOf,
  SOURCE_ORDER,
  SOURCE_WORDS,
  TZ,
  type HomeSectionId,
} from "@/components/home/home-copy";
import { HomeCustomizePopover } from "@/components/home/home-customize";
import { SECTION_DESCRIPTIONS, SECTION_LABELS, SECTION_ORDER, useHomePrefs } from "@/components/home/home-prefs";
import { HomeSectionCard, NeedRow } from "@/components/home/home-row";
import { TodayScheduleCard, type ScheduleSummary } from "@/components/home/today-schedule-card";
import { MobileAccount, MobileSearch, publishNeedsCount } from "@/components/shell";
import { Button } from "@/components/ui/button";
import { StyledSelect } from "@/components/ui/controls";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { PageIntro } from "@/components/ui/page-intro";
import { Pending } from "@/components/ui/skeleton";
import { getHomeNeeds, setHomeItemState } from "@/lib/api";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { eastern } from "@/lib/format";
import { keyOwner } from "@/lib/home";
import { useStaleRefreshOnReturn } from "@/lib/stale-refresh";
import type { HomeNeedItem, HomeNeeds } from "@/lib/types";
import { useRead } from "@/lib/use-read";

const loadAlerts = () => import("@/components/home/alerts-view");
const loadLate = () => import("@/components/home/late-view");
const AlertsView = dynamic(() => loadAlerts().then((m) => m.AlertsView), {
  loading: () => <Pending label="Reading the alerts" />,
});
const LateView = dynamic(() => loadLate().then((m) => m.LateView), {
  loading: () => <Pending label="Reading the late POs" />,
});

function usePreloadHomeParts() {
  useEffect(() => {
    const idle = () => void Promise.all([loadAlerts(), loadLate()]).catch(() => undefined);
    const ric = (window as Window & { requestIdleCallback?: (f: () => void) => number }).requestIdleCallback;
    if (ric) ric(idle);
    else setTimeout(idle, 1500);
  }, []);
}

const PAGE_ROWS = 100;

// guard-ok: display labels for the four unified Home section filter pills; decides nothing.
const SECTION_CHIP_LABELS: Record<HomeSectionId, string> = {
  replies: "Replies",
  tasks: "Tasks",
  holds_alerts: "Alerts & Supply",
  waiting: "Waiting",
};

const SWITCHER_ORDER: HomeSectionId[] = ["replies", "tasks", "holds_alerts", "waiting"];

type HomeToast = {
  note: string;
  tone?: "ok" | "error";
  undoItemId?: string;
};

type HomeItemSnapshot = {
  item: HomeNeedItem;
  list: "items" | "older_items";
  index: number;
};

type PulseFocus = "decisions" | "late" | null;
type FilterState = { section: HomeSectionId | null; focus: PulseFocus; source: string; due: string };

function greetingFor(now: Date, displayName?: string | null): string {
  const hourParts = new Intl.DateTimeFormat("en-US", {
    hour: "numeric",
    hour12: false,
    timeZone: TZ,
  }).formatToParts(now);
  const hour = Number(hourParts.find((p) => p.type === "hour")?.value ?? now.getHours());
  const salute = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  const first = (displayName ?? "").trim().split(/\s+/)[0];
  return first ? `${salute}, ${first}` : salute;
}

function formattedBriefingDate(now: Date): string {
  return new Intl.DateTimeFormat("en-US", {
    weekday: "long",
    month: "short",
    day: "numeric",
    timeZone: TZ,
  }).format(now);
}

function splitParam(text: string | null): string[] {
  return (text ?? "").split(",").map((x) => x.trim()).filter(Boolean);
}

function partLines(payload: HomeNeeds): string[] {
  const lines: string[] = [];
  if (payload.why) lines.push(payload.why);
  for (const part of payload.parts ?? []) {
    if (part.status !== "ok" && part.status !== "off" && part.note && !payload.why?.includes(part.note)) {
      lines.push(`${part.label}: ${part.note}`);
    }
  }
  return [...new Set(lines)];
}

function sectionFromParams(params: ReturnType<typeof useSearchParams>): { section: HomeSectionId | null; focus: PulseFocus } {
  const rawSection = (params?.get("section") ?? params?.get("bucket") ?? "").trim();
  const rawFocus = (params?.get("focus") ?? "").trim();
  const focus: PulseFocus = rawFocus === "decisions" || rawFocus === "late" ? rawFocus : null;
  if (rawSection === "replies") return { section: "replies", focus: null };
  if (rawSection === "waiting") return { section: "waiting", focus: null };
  if (rawSection === "tasks" || rawSection === "tasks_holds") return { section: "tasks", focus: null };
  if (rawSection === "late_pos") return { section: "holds_alerts", focus: "late" };
  if (rawSection === "holds_alerts" || rawSection === "alerts") return { section: "holds_alerts", focus };

  const kinds = splitParam(params?.get("kind") ?? null);
  if (kinds.length === 0 && params?.get("view") === "reply") return { section: "replies", focus: null };
  if (kinds.length === 1) {
    const k = kinds[0];
    if (k === "reply" || k === "question" || k === "approve") return { section: "replies", focus: null };
    if (k === "waiting") return { section: "waiting", focus: null };
    if (k === "alert") return { section: "holds_alerts", focus: "decisions" };
    if (k === "late_po") return { section: "holds_alerts", focus: "late" };
    if (k === "task" || k === "routine" || k === "gap" || k === "feedback") return { section: "tasks", focus: null };
  }
  return { section: null, focus: null };
}

function initialFilters(params: ReturnType<typeof useSearchParams>): FilterState {
  const { section, focus } = sectionFromParams(params);
  return {
    section,
    focus,
    source: (params?.get("source") ?? "").trim(),
    due: (params?.get("due") ?? "").trim(),
  };
}

function Today() {
  const router = useRouter();
  const params = useSearchParams();
  const { state: authState } = useAuth();

  const needsRead = useRead<HomeNeeds>("/api/home/needs", (signal) => getHomeNeeds(signal));
  const payload = needsRead.data;
  const readError = needsRead.error;
  const error = readError ? plainError(readError) : null;
  const busy = readError ? noticeKind(readError) === "busy" : false;
  const setPayload = needsRead.set;
  const payloadRef = useRef<HomeNeeds | null>(payload);
  payloadRef.current = payload;

  const {
    prefs,
    isCustomized,
    setDensity,
    toggleSectionVisible,
    toggleSectionCollapsed,
    resetPrefs,
  } = useHomePrefs();

  const [now] = useState(() => new Date());
  const [gone, setGone] = useState<Set<string>>(() => new Set());
  const [restoredItems, setRestoredItems] = useState<Map<string, HomeItemSnapshot>>(() => new Map());
  const undoSnapshots = useRef<Map<string, HomeItemSnapshot>>(new Map());
  const itemMutationGen = useRef<Map<string, number>>(new Map());
  const refreshStartedPayload = useRef<HomeNeeds | null>(null);
  const [shownTasks, setShownTasks] = useState(PAGE_ROWS);
  const [shownAlerts, setShownAlerts] = useState(PAGE_ROWS);
  const [holdsOpen, setHoldsOpen] = useState(true);
  const [financialsOpen, setFinancialsOpen] = useState(false);
  const [financialsShown, setFinancialsShown] = useState(PAGE_ROWS);
  const [olderOpen, setOlderOpen] = useState(false);
  const [opsOpen, setOpsOpen] = useState(false);
  const [filters, setFilters] = useState<FilterState>(() => initialFilters(params));
  const [picked, setPicked] = useState<string | null>(() => {
    const matter = params?.get("matter");
    return matter ? (matter.startsWith("matter:") ? matter : `matter:${matter}`) : (params?.get("sel") ?? null);
  });
  const [toast, setToast] = useState<HomeToast | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [scheduleSummary, setScheduleSummary] = useState<ScheduleSummary>({
    todayCount: 0,
    nextTime: null,
    loading: true,
  });
  const handleScheduleSummary = useCallback((next: ScheduleSummary) => {
    setScheduleSummary((prev) =>
      prev.todayCount === next.todayCount && prev.nextTime === next.nextTime && prev.loading === next.loading
        ? prev
        : next,
    );
  }, []);
  const [scheduleHighlighted, setScheduleHighlighted] = useState(false);

  const supplier = params?.get("supplier") ?? null;
  const listRef = useRef<HTMLDivElement>(null);
  const scheduleRef = useRef<HTMLElement>(null);

  const displayName = authState.status === "signed-in" ? authState.me.display_name : null;
  const greeting = useMemo(() => greetingFor(now, displayName), [now, displayName]);
  const todayDateLabel = useMemo(() => formattedBriefingDate(now), [now]);

  const paramKey = params?.toString() ?? "";
  const mountedParams = useRef(paramKey);
  useEffect(() => {
    if (paramKey === mountedParams.current) return;
    mountedParams.current = paramKey;
    setFilters(initialFilters(params));
    const matter = params?.get("matter");
    setPicked(matter ? (matter.startsWith("matter:") ? matter : `matter:${matter}`) : (params?.get("sel") ?? null));
  }, [paramKey, params]);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 6500);
    return () => window.clearTimeout(t);
  }, [toast]);

  useEffect(() => {
    if (!scheduleHighlighted) return;
    const t = window.setTimeout(() => setScheduleHighlighted(false), 1800);
    return () => window.clearTimeout(t);
  }, [scheduleHighlighted]);

  useEffect(() => {
    if (!refreshing) return;
    if (readError || (payload && payload !== refreshStartedPayload.current)) {
      setRefreshing(false);
      return;
    }
    if (needsRead.at === null) {
      const t = window.setTimeout(() => setRefreshing(false), 500);
      return () => window.clearTimeout(t);
    }
  }, [refreshing, payload, readError, needsRead.at]);

  const refresh = needsRead.retry;
  useStaleRefreshOnReturn(payload?.stale_since, refresh);

  // Clean up any restored overlay once the server payload natively includes the item again.
  useEffect(() => {
    if (!payload || restoredItems.size === 0) return;
    const present = new Set<string>([
      ...(payload.items ?? []).map((row) => row.id),
      ...(payload.older_items ?? []).map((row) => row.id),
    ]);
    let changed = false;
    for (const id of restoredItems.keys()) {
      if (present.has(id)) {
        changed = true;
        break;
      }
    }
    if (!changed) return;
    setRestoredItems((cur) => {
      const next = new Map(cur);
      for (const id of next.keys()) {
        if (present.has(id)) next.delete(id);
      }
      return next;
    });
  }, [payload, restoredItems]);

  // Defensive frontend deduplication guarantees users never see stacked duplicate notifications.
  const dedupedCurrent = useMemo(() => {
    const base = [...(payload?.items ?? [])];
    const baseIds = new Set(base.map((row) => row.id));
    for (const [id, snap] of restoredItems) {
      if (snap.list !== "items" || gone.has(id) || baseIds.has(id)) continue;
      base.splice(Math.min(snap.index, base.length), 0, snap.item);
      baseIds.add(id);
    }
    const filtered = base.filter(
      (item) => !gone.has(item.id) && (!supplier || item.person === supplier),
    );
    return dedupeNeedItems(filtered);
  }, [payload, restoredItems, gone, supplier]);

  const dedupedOlder = useMemo(() => {
    const base = [...(payload?.older_items ?? [])];
    const baseIds = new Set(base.map((row) => row.id));
    for (const [id, snap] of restoredItems) {
      if (snap.list !== "older_items" || gone.has(id) || baseIds.has(id)) continue;
      base.splice(Math.min(snap.index, base.length), 0, snap.item);
      baseIds.add(id);
    }
    const filtered = base.filter(
      (item) => !gone.has(item.id) && (!supplier || item.person === supplier),
    );
    return dedupeNeedItems(filtered);
  }, [payload, restoredItems, gone, supplier]);

  const mergedCountById = useMemo(() => {
    const map = new Map<string, number>();
    for (const d of dedupedCurrent) if (d.mergedCount > 0) map.set(d.item.id, d.mergedCount);
    for (const d of dedupedOlder) if (d.mergedCount > 0) map.set(d.item.id, d.mergedCount);
    return map;
  }, [dedupedCurrent, dedupedOlder]);

  const activeCurrent = useMemo(() => dedupedCurrent.map((d) => d.item), [dedupedCurrent]);
  const activeOlder = useMemo(() => dedupedOlder.map((d) => d.item), [dedupedOlder]);

  const unfoldedCurrent = useMemo(
    () => activeCurrent.filter((row) => !isFinancialsItem(row)),
    [activeCurrent],
  );

  const mainCurrent = useMemo(() => {
    const ops = unfoldedCurrent.filter(isOpsSystemNotice);
    const opsIds = ops.length >= 2 ? new Set(ops.map((x) => x.id)) : new Set<string>();
    return unfoldedCurrent.filter((row) => !opsIds.has(row.id));
  }, [unfoldedCurrent]);

  // Sidebar badge, page header count, and "All" filter pill always agree on mainCurrent.length.
  useEffect(() => {
    if (!supplier) {
      if (payload) {
        if (payload.status === "ok") {
          publishNeedsCount(mainCurrent.length);
        } else {
          publishNeedsCount(null);
        }
      } else if (readError) {
        publishNeedsCount(null);
      }
    }
  }, [payload, readError, supplier, mainCurrent.length]);

  // Exact partition counts using sectionOf(), matching the rendered sections 1:1.
  const sectionTotals: Record<HomeSectionId, number> = useMemo(() => {
    const counts: Record<HomeSectionId, number> = {
      replies: 0,
      holds_alerts: 0,
      tasks: 0,
      waiting: 0,
    };
    for (const row of mainCurrent) {
      counts[sectionOf(row)] += 1;
    }
    return counts;
  }, [mainCurrent]);

  const decisionHoldCount = useMemo(
    () => mainCurrent.filter(isDecisionOrHoldItem).length,
    [mainCurrent],
  );
  const lateSupplyCount = useMemo(
    () => mainCurrent.filter(isLateSupplyItem).length,
    [mainCurrent],
  );

  const sourceCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of mainCurrent) counts.set(row.source, (counts.get(row.source) ?? 0) + 1);
    return counts;
  }, [mainCurrent]);

  const dueCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of mainCurrent) {
      const bucket = dueValue(row);
      counts.set(bucket, (counts.get(bucket) ?? 0) + 1);
    }
    return counts;
  }, [mainCurrent]);

  const matches = useCallback(
    (item: HomeNeedItem) => {
      if (filters.section && (isFinancialsItem(item) || sectionOf(item) !== filters.section)) return false;
      if (filters.focus === "decisions" && !isDecisionOrHoldItem(item)) return false;
      if (filters.focus === "late" && !isLateSupplyItem(item)) return false;
      if (filters.source && item.source !== filters.source) return false;
      if (filters.due && dueValue(item) !== filters.due) return false;
      return true;
    },
    [filters],
  );

  const items = useMemo(() => activeCurrent.filter(matches), [activeCurrent, matches]);
  const olderItems = useMemo(() => activeOlder.filter(matches), [activeOlder, matches]);

  // Partition matched items into the four sections plus collapsible financials/ops groups.
  const {
    replyItems,
    waitingItems,
    taskItems,
    alertAndLateItems,
    holdItems,
    financialsItems,
    opsNotices,
  } = useMemo(() => {
    const ops = items.filter(isOpsSystemNotice);
    const opsIds = ops.length >= 2 ? new Set(ops.map((x) => x.id)) : new Set<string>();
    const replies: HomeNeedItem[] = [];
    const waiting: HomeNeedItem[] = [];
    const tasks: HomeNeedItem[] = [];
    const alertsAndLate: HomeNeedItem[] = [];
    const holds: HomeNeedItem[] = [];
    const financials: HomeNeedItem[] = [];
    for (const x of items) {
      if (opsIds.has(x.id)) continue;
      if (isFinancialsItem(x)) {
        financials.push(x);
      } else if (isHoldItem(x)) {
        holds.push(x);
      } else {
        const sec = sectionOf(x);
        if (sec === "replies") replies.push(x);
        else if (sec === "waiting") waiting.push(x);
        else if (sec === "holds_alerts") alertsAndLate.push(x);
        else tasks.push(x);
      }
    }
    return {
      replyItems: replies,
      waitingItems: waiting,
      taskItems: tasks,
      alertAndLateItems: alertsAndLate,
      holdItems: holds,
      financialsItems: financials,
      opsNotices: ops.length >= 2 ? ops : ([] as HomeNeedItem[]),
    };
  }, [items]);

  const sectionCounts: Record<HomeSectionId, number> = useMemo(
    () => ({
      replies: replyItems.length,
      holds_alerts: alertAndLateItems.length + holdItems.length,
      tasks: taskItems.length,
      waiting: waitingItems.length,
    }),
    [replyItems.length, alertAndLateItems.length, holdItems.length, taskItems.length, waitingItems.length],
  );

  const visibleMainCount =
    replyItems.length + waitingItems.length + taskItems.length + alertAndLateItems.length + holdItems.length;

  useEffect(() => {
    setShownTasks(PAGE_ROWS);
    setShownAlerts(PAGE_ROWS);
    setFinancialsShown(PAGE_ROWS);
  }, [filters, supplier]);

  const hasFilters = !!filters.section || !!filters.focus || !!filters.source || !!filters.due;

  // When a user actively clicks a filter, always show matching sections even if hidden/collapsed.
  const isSectionVisible = (id: HomeSectionId) => hasFilters || prefs.visibleSections[id];
  const isSectionOpen = (id: HomeSectionId) => hasFilters || !prefs.collapsedSections[id];

  // guard-ok: exact total, Show more, and Show all controls are rendered beside the list.
  const visibleTasks = taskItems.slice(0, shownTasks);
  // guard-ok: exact total, Show more, and Show all controls are rendered beside the list.
  const visibleAlertsAndLate = alertAndLateItems.slice(0, shownAlerts);
  // guard-ok: exact total, Show more, and Show all controls are rendered inside the expanded Financials block.
  const visibleFinancials = financialsItems.slice(0, financialsShown);

  const selectable = useMemo(
    () => [
      ...(isSectionVisible("replies") && isSectionOpen("replies") ? replyItems : []),
      ...(isSectionVisible("holds_alerts") && isSectionOpen("holds_alerts")
        ? [...visibleAlertsAndLate, ...(holdsOpen ? holdItems : [])]
        : []),
      ...(isSectionVisible("tasks") && isSectionOpen("tasks") ? visibleTasks : []),
      ...(isSectionVisible("waiting") && isSectionOpen("waiting") ? waitingItems : []),
      ...(financialsOpen ? visibleFinancials : []),
      ...(opsOpen ? opsNotices : []),
      ...(olderOpen ? olderItems : []),
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      prefs.visibleSections,
      prefs.collapsedSections,
      hasFilters,
      replyItems,
      visibleAlertsAndLate,
      holdsOpen,
      holdItems,
      visibleTasks,
      waitingItems,
      financialsOpen,
      visibleFinancials,
      opsOpen,
      opsNotices,
      olderOpen,
      olderItems,
    ],
  );
  const allListed = useMemo(() => [...items, ...olderItems], [items, olderItems]);

  const selected = useMemo(
    () => (picked ? (allListed.find((item) => item.id === picked) ?? null) : null),
    [allListed, picked],
  );

  const replaceUrl = useCallback((mutate: (q: URLSearchParams) => void) => {
    if (typeof window === "undefined") return;
    const q = new URLSearchParams(window.location.search);
    mutate(q);
    const qs = q.toString();
    window.history.replaceState(null, "", qs ? `/?${qs}` : "/");
  }, []);

  const pick = useCallback(
    (id: string) => {
      setPicked(id);
      replaceUrl((q) => {
        q.delete("matter");
        q.set("sel", id);
      });
    },
    [replaceUrl],
  );

  const closeDetail = useCallback(() => {
    setPicked(null);
    replaceUrl((q) => {
      q.delete("sel");
      q.delete("matter");
    });
  }, [replaceUrl]);

  const selectSection = useCallback(
    (sec: HomeSectionId | null) => {
      setFilters((cur) => {
        const next = cur.section === sec && cur.focus === null ? null : sec;
        replaceUrl((q) => {
          q.delete("kind");
          q.delete("view");
          q.delete("bucket");
          q.delete("focus");
          if (next) q.set("section", next);
          else q.delete("section");
        });
        return { ...cur, section: next, focus: null };
      });
    },
    [replaceUrl],
  );

  const selectPulseFocus = useCallback(
    (focus: Exclude<PulseFocus, null>) => {
      setFilters((cur) => {
        const nextFocus = cur.section === "holds_alerts" && cur.focus === focus ? null : focus;
        const nextSection: HomeSectionId | null = nextFocus ? "holds_alerts" : null;
        replaceUrl((q) => {
          q.delete("kind");
          q.delete("view");
          q.delete("bucket");
          if (nextSection && nextFocus) {
            q.set("section", nextSection);
            q.set("focus", nextFocus);
          } else {
            q.delete("section");
            q.delete("focus");
          }
        });
        return { ...cur, section: nextSection, focus: nextFocus };
      });
    },
    [replaceUrl],
  );

  const setAllSections = useCallback(() => {
    setFilters((cur) => ({ ...cur, section: null, focus: null }));
    replaceUrl((q) => {
      q.delete("view");
      q.delete("kind");
      q.delete("bucket");
      q.delete("section");
      q.delete("focus");
    });
  }, [replaceUrl]);

  const setSourceFilter = useCallback(
    (source: string) => {
      setFilters((cur) => ({ ...cur, source }));
      replaceUrl((q) => {
        if (source) q.set("source", source);
        else q.delete("source");
      });
    },
    [replaceUrl],
  );

  const setDueFilter = useCallback(
    (due: string) => {
      setFilters((cur) => ({ ...cur, due }));
      replaceUrl((q) => {
        if (due) q.set("due", due);
        else q.delete("due");
      });
    },
    [replaceUrl],
  );

  const clearFilters = useCallback(() => {
    setFilters({ section: null, focus: null, source: "", due: "" });
    replaceUrl((q) => {
      q.delete("kind");
      q.delete("source");
      q.delete("due");
      q.delete("view");
      q.delete("bucket");
      q.delete("section");
      q.delete("focus");
    });
  }, [replaceUrl]);

  const focusSchedule = useCallback(() => {
    setScheduleHighlighted(true);
    scheduleRef.current?.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
    scheduleRef.current?.focus?.();
  }, []);

  const markGone = useCallback(
    (id: string, note?: string) => {
      setGone((cur) => new Set(cur).add(id));
      const cur = payload;
      if (cur) {
        const inMain = (cur.items ?? []).some((row) => row.id === id);
        const inOlder = (cur.older_items ?? []).some((row) => row.id === id);
        if (inMain || inOlder) {
          setPayload({
            ...cur,
            items: (cur.items ?? []).filter((row) => row.id !== id),
            total: inMain ? Math.max(0, cur.total - 1) : cur.total,
            older_items: (cur.older_items ?? []).filter((row) => row.id !== id),
            older_total: inOlder ? Math.max(0, (cur.older_total ?? 0) - 1) : cur.older_total,
          });
        }
      }
      if (picked === id) closeDetail();
      if (note) setToast({ note, tone: "ok" });
    },
    [payload, picked, setPayload, closeDetail],
  );

  // Optimistic universal 1-click Close / Snooze with instant Undo support on every Home row.
  const handleQuickItemState = useCallback(
    (item: HomeNeedItem, nextState: "closed" | "snoozed", title: string) => {
      const id = item.id;
      const gen = (itemMutationGen.current.get(id) ?? 0) + 1;
      itemMutationGen.current.set(id, gen);
      const until = nextState === "snoozed" ? new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString() : undefined;
      const curPayload = payloadRef.current;
      const mainIdx = (curPayload?.items ?? []).findIndex((row) => row.id === id);
      const olderIdx = mainIdx < 0 ? (curPayload?.older_items ?? []).findIndex((row) => row.id === id) : -1;
      const existingSnap = undoSnapshots.current.get(id);
      const snapshot: HomeItemSnapshot =
        mainIdx >= 0
          ? { item: curPayload?.items?.[mainIdx] ?? item, list: "items", index: mainIdx }
          : olderIdx >= 0
            ? { item: curPayload?.older_items?.[olderIdx] ?? item, list: "older_items", index: olderIdx }
            : existingSnap ?? { item, list: "items", index: 0 };
      undoSnapshots.current.set(id, snapshot);

      setRestoredItems((cur) => {
        if (!cur.has(id)) return cur;
        const next = new Map(cur);
        next.delete(id);
        return next;
      });
      setGone((cur) => new Set(cur).add(id));
      if (picked === id) closeDetail();
      setToast({
        note: nextState === "snoozed" ? `Snoozed ${title} for 1 day` : `Closed ${title}`,
        tone: "ok",
        undoItemId: id,
      });
      void setHomeItemState(id, nextState, until).catch((err) => {
        if (itemMutationGen.current.get(id) !== gen) return;
        setGone((cur) => {
          const next = new Set(cur);
          next.delete(id);
          return next;
        });
        setRestoredItems((cur) => {
          const next = new Map(cur);
          next.set(id, snapshot);
          return next;
        });
        setToast({ note: plainError(err), tone: "error" });
      });
    },
    [picked, closeDetail],
  );

  const handleUndoItemState = useCallback(
    (id: string) => {
      const gen = (itemMutationGen.current.get(id) ?? 0) + 1;
      itemMutationGen.current.set(id, gen);
      const saved = undoSnapshots.current.get(id);
      setGone((cur) => {
        const next = new Set(cur);
        next.delete(id);
        return next;
      });
      if (saved) {
        setRestoredItems((cur) => {
          const next = new Map(cur);
          next.set(id, saved);
          return next;
        });
        const cur = payloadRef.current;
        if (cur) {
          if (saved.list === "items" && !(cur.items ?? []).some((row) => row.id === id)) {
            const nextItems = [...(cur.items ?? [])];
            nextItems.splice(Math.min(saved.index, nextItems.length), 0, saved.item);
            setPayload({ ...cur, items: nextItems, total: cur.total + 1 });
          } else if (saved.list === "older_items" && !(cur.older_items ?? []).some((row) => row.id === id)) {
            const nextOlder = [...(cur.older_items ?? [])];
            nextOlder.splice(Math.min(saved.index, nextOlder.length), 0, saved.item);
            setPayload({ ...cur, older_items: nextOlder, older_total: (cur.older_total ?? 0) + 1 });
          }
        }
      }
      setToast(null);
      void setHomeItemState(id, "open")
        .then(() => {
          if (itemMutationGen.current.get(id) !== gen) return;
          refresh(true);
        })
        .catch((err) => {
          if (itemMutationGen.current.get(id) !== gen) return;
          setRestoredItems((cur) => {
            if (!cur.has(id)) return cur;
            const next = new Map(cur);
            next.delete(id);
            return next;
          });
          setGone((cur) => new Set(cur).add(id));
          setToast({ note: plainError(err), tone: "error" });
        });
    },
    [refresh, setPayload],
  );

  // Escape collapses the expanded row; j/k and arrow keys step through rows.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
      if (e.key === "Escape" && picked) {
        const active = document.activeElement;
        if (active instanceof Element && active.closest("[role='dialog']")) return;
        e.preventDefault();
        closeDetail();
        return;
      }
      if (selectable.length === 0) return;
      if (keyOwner(e.target) === "own") return;
      const at = selectable.findIndex((r) => r.id === picked);
      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        pick(selectable[Math.min(selectable.length - 1, at + 1)].id);
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        pick(selectable[Math.max(0, at <= 0 ? 0 : at - 1)].id);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [picked, closeDetail, selectable, pick]);

  const loading = !error && !payload;
  const notes = payload ? partLines(payload) : [];

  const sectionChips = useMemo(
    () => SWITCHER_ORDER.filter((id) => sectionTotals[id] > 0 || filters.section === id),
    [sectionTotals, filters.section],
  );

  const sourceChoices = useMemo(() => {
    const present = new Set([...sourceCounts.keys(), ...(filters.source ? [filters.source] : [])]);
    return [...SOURCE_ORDER.filter((s) => present.has(s)), ...[...present].filter((s) => !SOURCE_ORDER.includes(s))];
  }, [sourceCounts, filters.source]);

  const dueChoices = useMemo(() => {
    const present = new Set([...dueCounts.keys(), ...(filters.due ? [filters.due] : [])]);
    return DUE_ORDER.filter((d) => present.has(d));
  }, [dueCounts, filters.due]);

  const showFilterBar =
    !loading &&
    !error &&
    (sectionChips.length > 0 || sourceChoices.length > 1 || dueChoices.length > 1 || hasFilters);

  const renderRow = (item: HomeNeedItem) => (
    <NeedRow
      key={item.id}
      item={item}
      mergedCount={mergedCountById.get(item.id) ?? 0}
      density={prefs.density}
      on={selected?.id === item.id}
      onPick={() => pick(item.id)}
      onClose={closeDetail}
      onGone={markGone}
      onQuickState={handleQuickItemState}
    />
  );

  const hiddenNonEmptySections = !hasFilters
    ? SECTION_ORDER.filter((id) => !prefs.visibleSections[id] && sectionCounts[id] > 0)
    : [];

  const repliesSection =
    replyItems.length > 0 && isSectionVisible("replies") ? (
      <HomeSectionCard
        title={SECTION_LABELS.replies}
        description={SECTION_DESCRIPTIONS.replies}
        count={replyItems.length}
        icon={<Mail className="size-3.5 shrink-0 text-accent" aria-hidden />}
        open={isSectionOpen("replies")}
        onToggle={() => toggleSectionCollapsed("replies")}
        testId="section-replies"
        toggleTestId="section-replies-toggle"
      >
        <ul className="divide-y divide-border/50" data-testid="section-replies-list">
          {replyItems.map(renderRow)}
        </ul>
      </HomeSectionCard>
    ) : null;

  const waitingSection =
    waitingItems.length > 0 && isSectionVisible("waiting") ? (
      <HomeSectionCard
        title={SECTION_LABELS.waiting}
        description={SECTION_DESCRIPTIONS.waiting}
        count={waitingItems.length}
        icon={<Clock3 className="size-3.5 shrink-0 text-muted" aria-hidden />}
        open={isSectionOpen("waiting")}
        onToggle={() => toggleSectionCollapsed("waiting")}
        testId="section-waiting"
        toggleTestId="section-waiting-toggle"
      >
        <ul className="divide-y divide-border/50" data-testid="section-waiting-list">
          {waitingItems.map(renderRow)}
        </ul>
      </HomeSectionCard>
    ) : null;

  const tasksSection =
    taskItems.length > 0 && isSectionVisible("tasks") ? (
      <HomeSectionCard
        title={SECTION_LABELS.tasks}
        description={SECTION_DESCRIPTIONS.tasks}
        count={taskItems.length}
        icon={<ClipboardList className="size-3.5 shrink-0 text-muted" aria-hidden />}
        open={isSectionOpen("tasks")}
        onToggle={() => toggleSectionCollapsed("tasks")}
        testId="section-tasks"
        toggleTestId="section-tasks-toggle"
      >
        <ul className="divide-y divide-border/50" data-testid="section-tasks-list">
          {visibleTasks.map(renderRow)}
        </ul>
        {visibleTasks.length < taskItems.length && (
          <div className="flex items-center gap-2 border-t border-border/50 px-3 py-2 text-[12px] text-muted" data-testid="needs-paging">
            <span className="num">
              Showing {visibleTasks.length} of {taskItems.length}
            </span>
            <Button size="xs" variant="ghost" onClick={() => setShownTasks((n) => n + PAGE_ROWS)}>
              Show {Math.min(PAGE_ROWS, taskItems.length - visibleTasks.length)} more
            </Button>
            <Button size="xs" variant="ghost" onClick={() => setShownTasks(taskItems.length)}>
              Show all
            </Button>
          </div>
        )}
      </HomeSectionCard>
    ) : null;

  const holdsAlertsSection =
    (alertAndLateItems.length > 0 || holdItems.length > 0) && isSectionVisible("holds_alerts") ? (
      alertAndLateItems.length === 0 ? (
        <section className="min-w-0" data-testid="holds-group">
          <div className="flex items-center gap-2 border-b border-border/80 px-1 py-2">
            <button
              type="button"
              aria-expanded={holdsOpen}
              onClick={() => setHoldsOpen((open) => !open)}
              data-testid="holds-toggle"
              className="group/hdr flex min-w-0 flex-1 items-center gap-2 text-left transition-colors cursor-pointer"
            >
              {holdsOpen ? (
                <ChevronDown className="size-3.5 shrink-0 text-faint transition-colors group-hover/hdr:text-fg" aria-hidden />
              ) : (
                <ChevronRight className="size-3.5 shrink-0 text-faint transition-colors group-hover/hdr:text-fg" aria-hidden />
              )}
              <h2 className="shrink-0 text-[13.5px] font-semibold tracking-[-0.01em] text-fg">
                QC Holds ({holdItems.length})
              </h2>
              <span className="hidden truncate text-[12.5px] font-normal text-faint sm:inline">
                Quality holds and rejected lots
              </span>
            </button>
          </div>
          {holdsOpen && (
            <ul className="divide-y divide-border/50 pt-1" data-testid="holds-list">
              {holdItems.map(renderRow)}
            </ul>
          )}
        </section>
      ) : (
        <HomeSectionCard
          title={SECTION_LABELS.holds_alerts}
          description={SECTION_DESCRIPTIONS.holds_alerts}
          count={alertAndLateItems.length + holdItems.length}
          open={isSectionOpen("holds_alerts")}
          onToggle={() => toggleSectionCollapsed("holds_alerts")}
          testId="section-holds-alerts"
          toggleTestId="section-holds-alerts-toggle"
        >
          <ul className="divide-y divide-border/50" data-testid="section-alerts-list">
            {visibleAlertsAndLate.map(renderRow)}
          </ul>
          {visibleAlertsAndLate.length < alertAndLateItems.length && (
            <div className="flex items-center gap-2 border-t border-border/50 px-3 py-2 text-[12px] text-muted" data-testid="alerts-paging">
              <span className="num">
                Showing {visibleAlertsAndLate.length} of {alertAndLateItems.length}
              </span>
              <Button size="xs" variant="ghost" onClick={() => setShownAlerts((n) => n + PAGE_ROWS)}>
                Show {Math.min(PAGE_ROWS, alertAndLateItems.length - visibleAlertsAndLate.length)} more
              </Button>
              <Button size="xs" variant="ghost" onClick={() => setShownAlerts(alertAndLateItems.length)}>
                Show all
              </Button>
            </div>
          )}
          {holdItems.length > 0 && (
            <div className="mt-2 border-t border-border/60 pt-1" data-testid="holds-group">
              <button
                type="button"
                aria-expanded={holdsOpen}
                onClick={() => setHoldsOpen((open) => !open)}
                data-testid="holds-toggle"
                className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-[12.5px] font-medium text-muted transition-colors hover:bg-hover/70 hover:text-fg cursor-pointer"
              >
                {holdsOpen ? (
                  <ChevronDown className="size-3.5 shrink-0 text-muted" aria-hidden />
                ) : (
                  <ChevronRight className="size-3.5 shrink-0 text-muted" aria-hidden />
                )}
                <ShieldAlert className="size-3.5 shrink-0 text-risk" aria-hidden />
                <span>QC Holds ({holdItems.length})</span>
                <span className="ml-auto text-[11.5px] font-normal text-muted">{holdsOpen ? "Hide" : "Show"}</span>
              </button>
              {holdsOpen && (
                <ul className="divide-y divide-border/50" data-testid="holds-list">
                  {holdItems.map(renderRow)}
                </ul>
              )}
            </div>
          )}
        </HomeSectionCard>
      )
    ) : null;

  const financialsSection =
    financialsItems.length > 0 ? (
      <section className="min-w-0" data-testid="financials-group">
        <button
          type="button"
          aria-expanded={financialsOpen}
          onClick={() => setFinancialsOpen((open) => !open)}
          data-testid="financials-toggle"
          className="group/hdr flex w-full items-center gap-2 border-b border-border/80 px-1 py-2 text-left text-[12.5px] font-medium text-muted transition-colors hover:border-border-strong hover:text-fg cursor-pointer"
        >
          {financialsOpen ? (
            <ChevronDown className="size-3.5 shrink-0 text-muted" aria-hidden />
          ) : (
            <ChevronRight className="size-3.5 shrink-0 text-muted" aria-hidden />
          )}
          <DollarSign className="size-3.5 shrink-0 text-muted" aria-hidden />
          <span className="min-w-0 truncate">{financialsSummaryLabel(financialsItems)}</span>
          <span className="ml-auto shrink-0 text-[11.5px] font-normal text-muted">{financialsOpen ? "Hide" : "Show"}</span>
        </button>
        {financialsOpen && (
          <div className="pt-1">
            <ul className="divide-y divide-border/50" data-testid="financials-list">
              {visibleFinancials.map(renderRow)}
            </ul>
            {visibleFinancials.length < financialsItems.length && (
              <div className="flex items-center gap-2 border-t border-border/50 px-3 py-2 text-[12px] text-muted" data-testid="financials-paging">
                <span className="num">
                  Showing {visibleFinancials.length} of {financialsItems.length}
                </span>
                <Button size="xs" variant="ghost" onClick={() => setFinancialsShown((n) => n + PAGE_ROWS)}>
                  Show {Math.min(PAGE_ROWS, financialsItems.length - visibleFinancials.length)} more
                </Button>
                <Button size="xs" variant="ghost" onClick={() => setFinancialsShown(financialsItems.length)}>
                  Show all
                </Button>
              </div>
            )}
          </div>
        )}
      </section>
    ) : null;

  const opsSection =
    opsNotices.length > 0 ? (
      <section className="min-w-0" data-testid="ops-notices-group">
        <button
          type="button"
          aria-expanded={opsOpen}
          onClick={() => setOpsOpen((open) => !open)}
          data-testid="ops-notices-toggle"
          className="group/hdr flex w-full items-center gap-2 border-b border-border/80 px-1 py-2 text-left text-[12.5px] font-medium text-muted transition-colors hover:border-border-strong hover:text-fg cursor-pointer"
        >
          {opsOpen ? (
            <ChevronDown className="size-3.5 shrink-0 text-muted" aria-hidden />
          ) : (
            <ChevronRight className="size-3.5 shrink-0 text-muted" aria-hidden />
          )}
          <Server className="size-3.5 shrink-0 text-muted" aria-hidden />
          <span>System and ops notices ({opsNotices.length})</span>
          <span className="ml-auto text-[11.5px] font-normal text-muted">{opsOpen ? "Hide" : "Show"}</span>
        </button>
        {opsOpen && (
          <ul className="divide-y divide-border/50 pt-1" data-testid="ops-notices-list">
            {opsNotices.map(renderRow)}
          </ul>
        )}
      </section>
    ) : null;

  const olderSection =
    olderItems.length > 0 && payload ? (
      <section className="min-w-0" data-testid="older-needs">
        <button
          type="button"
          aria-expanded={olderOpen}
          onClick={() => setOlderOpen((open) => !open)}
          data-testid="older-needs-toggle"
          className="group/hdr flex w-full items-center gap-2 border-b border-border/80 px-1 py-2 text-left text-[12.5px] font-medium text-muted transition-colors hover:border-border-strong hover:text-fg cursor-pointer"
        >
          {olderOpen ? (
            <ChevronDown className="size-3.5 shrink-0 text-muted" aria-hidden />
          ) : (
            <ChevronRight className="size-3.5 shrink-0 text-muted" aria-hidden />
          )}
          <span>Older than {payload.older_after_days} days ({olderItems.length})</span>
          <span className="ml-auto text-[11.5px] font-normal text-muted">{olderOpen ? "Hide" : "Show"}</span>
        </button>
        {olderOpen && (
          <ul className="divide-y divide-border/50 pt-1" data-testid="older-needs-list">
            {olderItems.map(renderRow)}
          </ul>
        )}
      </section>
    ) : null;

  return (
    <section
      className="flex min-h-0 flex-1 flex-col bg-bg"
      aria-label="Home daily briefing"
      data-testid="briefing"
    >
      {/* Mobile top utility bar */}
      <header className="bg-bg lg:hidden">
        <div className="flex h-11 items-center justify-end gap-1 px-3">
          <MobileSearch />
          <MobileAccount />
        </div>
      </header>

      <PageIntro path="/" />

      <div
        ref={listRef}
        className="min-h-0 flex-1 overflow-y-auto"
        data-testid="needs-you"
        {...(loading ? { "data-pending": "" } : {})}
      >
        <div className="mx-auto w-full max-w-[1360px] space-y-6 px-4 pt-6 pb-14 sm:px-6">
          {/* Zone 1: Clean Architectural Greeting Header (zero floating search/ask form) */}
          <div className="flex flex-wrap items-end justify-between gap-3" data-testid="home-header">
            <div className="min-w-0">
              <h1 className="font-display text-[26px] leading-8 font-semibold tracking-[-0.03em] text-fg sm:text-[28px]">
                {greeting}
              </h1>
              <p className="mt-1 flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[13px] text-muted">
                <span>{todayDateLabel}</span>
                {!loading && payload && payload.status === "ok" && (
                  <span title="Open items needing your attention">
                    <span className="num font-medium text-fg" data-testid="needs-total">
                      {hasFilters ? visibleMainCount : mainCurrent.length}
                    </span>{" "}
                    open
                  </span>
                )}
                {payload?.as_of && (
                  <span className="num text-faint">Updated {eastern(payload.as_of)}</span>
                )}
              </p>
            </div>

            <div className="flex shrink-0 items-center gap-1.5">
              <HomeCustomizePopover
                prefs={prefs}
                sectionCounts={sectionCounts}
                isCustomized={isCustomized}
                onDensityChange={setDensity}
                onToggleSection={toggleSectionVisible}
                onReset={resetPrefs}
              />
              <button
                type="button"
                disabled={loading || refreshing}
                onClick={() => {
                  refreshStartedPayload.current = payload;
                  setRefreshing(true);
                  refresh(true);
                }}
                aria-label="Refresh Home"
                title="Refresh"
                data-testid="home-refresh"
                className="inline-flex h-7 items-center gap-1.5 rounded-md border border-border bg-transparent px-2.5 text-[12px] font-medium text-muted transition-colors hover:border-border-strong hover:bg-hover hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 active:scale-[0.98] disabled:pointer-events-none disabled:opacity-50 cursor-pointer"
              >
                <RefreshCw className={cn("size-3.5", refreshing && "animate-spin")} aria-hidden />
                <span className="hidden sm:inline">{refreshing ? "Refreshing" : "Refresh"}</span>
              </button>
            </div>
          </div>

          {/* Zone 2: Flush Hairline Executive Metric Strip (pure numbers + crisp labels, zero cards) */}
          <div
            className="grid grid-cols-2 divide-y divide-border border-y border-border lg:grid-cols-4 lg:divide-x lg:divide-y-0"
            data-testid="home-pulse-strip"
            role="group"
            aria-label="Executive pulse"
          >
            <button
              type="button"
              onClick={() => selectSection("replies")}
              aria-pressed={filters.section === "replies" && filters.focus === null}
              data-testid="pulse-tile-replies"
              className={cn(
                "group relative px-4 py-3 text-left transition-colors cursor-pointer first:pl-1",
                filters.section === "replies" && filters.focus === null
                  ? "bg-surface/65"
                  : "hover:bg-hover",
              )}
            >
              {filters.section === "replies" && filters.focus === null && (
                <span className="absolute inset-x-0 bottom-0 h-[2px] bg-accent" aria-hidden />
              )}
              <div className="text-[12px] font-medium text-muted">Needs reply</div>
              <div className="font-display num mt-0.5 text-[24px] leading-8 font-semibold tracking-[-0.03em] text-fg">
                {loading ? "-" : sectionTotals.replies}
              </div>
              <p className="truncate text-[11.5px] text-faint">
                {loading ? "Checking queue" : sectionTotals.replies === 0 ? "All caught up" : "Awaiting your response"}
              </p>
            </button>

            <button
              type="button"
              onClick={() => selectPulseFocus("decisions")}
              aria-pressed={filters.section === "holds_alerts" && filters.focus === "decisions"}
              data-testid="pulse-tile-decisions"
              className={cn(
                "group relative px-4 py-3 text-left transition-colors cursor-pointer",
                filters.section === "holds_alerts" && filters.focus === "decisions"
                  ? "bg-surface/65"
                  : "hover:bg-hover",
              )}
            >
              {filters.section === "holds_alerts" && filters.focus === "decisions" && (
                <span className="absolute inset-x-0 bottom-0 h-[2px] bg-accent" aria-hidden />
              )}
              <div className="text-[12px] font-medium text-muted">Decisions &amp; holds</div>
              <div className={cn("font-display num mt-0.5 text-[24px] leading-8 font-semibold tracking-[-0.03em]", decisionHoldCount > 0 ? "text-risk" : "text-fg")}>
                {loading ? "-" : decisionHoldCount}
              </div>
              <p className="truncate text-[11.5px] text-faint">
                {loading ? "Checking alerts" : decisionHoldCount === 0 ? "No active holds" : "Holds and exceptions"}
              </p>
            </button>

            <button
              type="button"
              onClick={() => selectPulseFocus("late")}
              aria-pressed={filters.section === "holds_alerts" && filters.focus === "late"}
              data-testid="pulse-tile-late"
              className={cn(
                "group relative px-4 py-3 text-left transition-colors cursor-pointer",
                filters.section === "holds_alerts" && filters.focus === "late"
                  ? "bg-surface/65"
                  : "hover:bg-hover",
              )}
            >
              {filters.section === "holds_alerts" && filters.focus === "late" && (
                <span className="absolute inset-x-0 bottom-0 h-[2px] bg-accent" aria-hidden />
              )}
              <div className="text-[12px] font-medium text-muted">Late supply</div>
              <div className={cn("font-display num mt-0.5 text-[24px] leading-8 font-semibold tracking-[-0.03em]", lateSupplyCount > 0 ? "text-late" : "text-fg")}>
                {loading ? "-" : lateSupplyCount}
              </div>
              <p className="truncate text-[11.5px] text-faint">
                {loading ? "Checking supply" : lateSupplyCount === 0 ? "On schedule" : "Overdue purchase orders"}
              </p>
            </button>

            <button
              type="button"
              onClick={focusSchedule}
              data-testid="pulse-tile-schedule"
              className={cn(
                "group relative px-4 py-3 text-left transition-colors cursor-pointer last:pr-1",
                scheduleHighlighted
                  ? "bg-surface/65"
                  : "hover:bg-hover",
              )}
            >
              {scheduleHighlighted && (
                <span className="absolute inset-x-0 bottom-0 h-[2px] bg-accent" aria-hidden />
              )}
              <div className="text-[12px] font-medium text-muted">Today&apos;s schedule</div>
              <div className="font-display num mt-0.5 text-[24px] leading-8 font-semibold tracking-[-0.03em] text-fg">
                {scheduleSummary.loading ? "-" : scheduleSummary.todayCount}
              </div>
              <p className="truncate text-[11.5px] text-faint">
                {scheduleSummary.loading
                  ? "Reading calendar"
                  : scheduleSummary.nextTime
                    ? `Next at ${scheduleSummary.nextTime}`
                    : scheduleSummary.todayCount > 0
                      ? "Done for today"
                      : "Clear calendar"}
              </p>
            </button>
          </div>

          {/* Zone 3: Asymmetric 2-Column Executive Split (~64% Priority Stream / ~36% Today's Schedule) */}
          <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-[minmax(0,1.78fr)_minmax(310px,1fr)] xl:grid-cols-[minmax(0,1.82fr)_minmax(340px,1fr)]">
            <div className="min-w-0 space-y-5">
              {(supplier || showFilterBar) && (
                <div className="flex flex-wrap items-center justify-between gap-2.5">
                  {supplier && (
                    <div className="flex items-center gap-2" data-testid="supplier-filter">
                      <span className="inline-flex h-8 min-w-0 items-center gap-2 rounded-lg border border-border bg-surface pr-1.5 pl-3">
                        <span className="truncate text-[12.5px] font-medium text-fg">{supplier}</span>
                        <button
                          type="button"
                          onClick={() => router.push("/")}
                          aria-label="Show every supplier"
                          className="flex size-5 items-center justify-center rounded-sm text-muted hover:bg-hover hover:text-fg cursor-pointer"
                        >
                          <X className="size-3.5" aria-hidden />
                        </button>
                      </span>
                    </div>
                  )}

                  {showFilterBar && (
                    <div className="flex min-w-0 flex-1 flex-wrap items-center justify-between gap-2.5" data-testid="needs-filters">
                      {sectionChips.length > 0 && (
                        <div
                          className="liquid-glass-pill inline-flex flex-wrap items-center gap-0.5 rounded-xl p-1"
                          role="group"
                          aria-label="Filter by section"
                          data-testid="section-filters"
                        >
                          <button
                            type="button"
                            aria-pressed={!filters.section && !filters.focus}
                            onClick={setAllSections}
                            data-testid="filter-section-all"
                            className={cn(
                              "inline-flex h-7 items-center gap-1.5 rounded-lg px-2.5 text-[12.5px] font-medium transition-colors cursor-pointer",
                              !filters.section && !filters.focus
                                ? "liquid-glass-active"
                                : "text-muted hover:bg-hover/60 hover:text-fg",
                            )}
                          >
                            All
                            <span className={cn("num text-[11.5px]", !filters.section && !filters.focus ? "opacity-80" : "text-faint")}>
                              {mainCurrent.length}
                            </span>
                          </button>
                          {sectionChips.map((sec) => {
                            const on = filters.section === sec && filters.focus === null;
                            const count = sectionTotals[sec];
                            return (
                              <button
                                key={sec}
                                type="button"
                                aria-pressed={on}
                                onClick={() => selectSection(sec)}
                                data-testid={`filter-section-${sec}`}
                                className={cn(
                                  "inline-flex h-7 items-center gap-1.5 rounded-lg px-2.5 text-[12.5px] font-medium transition-colors cursor-pointer",
                                  on
                                    ? "liquid-glass-active"
                                    : "text-muted hover:bg-hover/60 hover:text-fg",
                                )}
                              >
                                {SECTION_CHIP_LABELS[sec]}
                                <span className={cn("num text-[11.5px]", on ? "opacity-80" : "text-faint")}>{count}</span>
                              </button>
                            );
                          })}
                        </div>
                      )}

                      {(sourceChoices.length > 1 || dueChoices.length > 1 || hasFilters) && (
                        <div className="flex flex-wrap items-center gap-1.5">
                          {sourceChoices.length > 1 && (
                            <StyledSelect
                              ariaLabel="Filter by source"
                              value={filters.source}
                              onChange={setSourceFilter}
                              options={[
                                { value: "", label: "All sources" },
                                ...sourceChoices.map((src) => ({
                                  value: src,
                                  label: `${SOURCE_WORDS[src] ?? src} (${sourceCounts.get(src) ?? 0})`,
                                })),
                              ]}
                            />
                          )}
                          {dueChoices.length > 1 && (
                            <StyledSelect
                              ariaLabel="Filter by due date"
                              value={filters.due}
                              onChange={setDueFilter}
                              options={[
                                { value: "", label: "Any timing" },
                                ...dueChoices.map((bucket) => ({
                                  value: bucket,
                                  label: `${DUE_WORDS[bucket] ?? bucket} (${dueCounts.get(bucket) ?? 0})`,
                                })),
                              ]}
                            />
                          )}
                          {hasFilters && (
                            <button
                              type="button"
                              onClick={clearFilters}
                              className="h-8 rounded-md px-2.5 text-[12px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
                              data-testid="clear-filters"
                            >
                              Clear
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}

              <StaleNote since={payload?.stale_since} onReturn={!!payload?.stale_since} />

              {toast && (
                <div
                  role="status"
                  data-testid="home-action-status"
                  className="liquid-glass-surface flex items-center justify-between gap-3 rounded-xl px-3.5 py-2 text-[12.5px] text-fg plx-panel-animate"
                >
                  <span className="flex min-w-0 items-center gap-2">
                    <Check
                      className={cn("size-3.5 shrink-0", toast.tone === "error" ? "text-late" : "text-ok")}
                      strokeWidth={2.2}
                      aria-hidden
                    />
                    <span className="truncate">{toast.note}</span>
                  </span>
                  <div className="flex shrink-0 items-center gap-1.5">
                    {toast.undoItemId && (
                      <button
                        type="button"
                        onClick={() => handleUndoItemState(toast.undoItemId!)}
                        data-testid="home-action-undo"
                        className="inline-flex h-6 items-center gap-1 rounded-md border border-border-strong bg-bg/70 px-2 text-[12px] font-medium text-fg transition-colors hover:bg-hover cursor-pointer"
                      >
                        <RotateCcw className="size-3" strokeWidth={2} aria-hidden />
                        Undo
                      </button>
                    )}
                    <button
                      type="button"
                      aria-label="Dismiss notice"
                      onClick={() => setToast(null)}
                      className="inline-flex size-5 shrink-0 items-center justify-center rounded-sm text-muted hover:bg-hover hover:text-fg cursor-pointer"
                    >
                      <X className="size-3" aria-hidden />
                    </button>
                  </div>
                </div>
              )}

              {notes.length > 0 && (
                <div className="space-y-0.5 rounded-lg border border-border bg-surface/60 px-3.5 py-2 text-[12px] text-muted" data-testid="needs-state" aria-live="polite">
                  {notes.map((line) => (
                    <p key={line}>{line}</p>
                  ))}
                </div>
              )}

              {error ? (
                <div className="py-2">
                  <LoadProblem message={error} busy={busy} onRetry={refresh} />
                </div>
              ) : loading ? (
                <div className="py-6">
                  <Pending label="Reading what needs you" />
                </div>
              ) : (
                <>
                  {items.length === 0 ? (
                    <div className="rounded-xl border border-border/80 bg-surface/50 py-12 text-center text-[13px] text-muted" data-testid="home-empty">
                      <p>
                        {hasFilters
                          ? "Nothing matches these filters."
                          : payload?.status === "ok"
                            ? olderItems.length > 0
                              ? "Nothing recent needs you right now."
                              : "Nothing needs you right now."
                            : "The list could not be read completely; see the note above."}
                      </p>
                      {hasFilters && (
                        <div className="mt-3">
                          <Button size="sm" variant="secondary" onClick={clearFilters}>
                            Clear filters
                          </Button>
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="space-y-6" data-testid="home-grouped-view">
                      {repliesSection}
                      {holdsAlertsSection}
                      {tasksSection}
                      {waitingSection}
                      {financialsSection}
                      {opsSection}
                      {olderSection}
                    </div>
                  )}

                  {items.length === 0 && olderSection}

                  {hiddenNonEmptySections.length > 0 && (
                    <div className="flex items-center justify-between gap-2 rounded-lg border border-border/80 bg-surface/60 px-3.5 py-2 text-[12px] text-muted" data-testid="home-hidden-sections-note">
                      <span>
                        {hiddenNonEmptySections.length === 1
                          ? "1 section is hidden in your layout."
                          : `${hiddenNonEmptySections.length} sections are hidden in your layout.`}
                      </span>
                      <button
                        type="button"
                        onClick={resetPrefs}
                        className="font-medium text-fg underline decoration-border-strong underline-offset-4 hover:decoration-fg cursor-pointer"
                      >
                        Show all sections
                      </button>
                    </div>
                  )}
                </>
              )}
            </div>

            <TodayScheduleCard
              cardRef={scheduleRef}
              highlighted={scheduleHighlighted}
              onSummaryChange={handleScheduleSummary}
            />
          </div>
        </div>
      </div>
    </section>
  );
}

/** Home opens the Executive Daily Briefing; Alerts (?show=alerts) and Late POs (?show=late) remain intact for deep links. */
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
