"use client";

import {
  AlertCircle,
  ArrowUpRight,
  Bell,
  CheckCircle2,
  CircleDashed,
  Clock,
  Code2,
  ExternalLink,
  Flag,
  Loader2,
  MessageSquareWarning,
  Play,
  Plus,
  Search,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { extractKpiSummary } from "@/components/insights/bento-layout";
import { FeedbackTriagePanel } from "@/components/insights/genie-panel";
import { getGaps, getInsight, getObserved, sendNoteFeedback } from "@/lib/api";
import { cn } from "@/lib/cn";
import { ApiError, plainError } from "@/lib/errors";
import { formatDate, formatDateTime } from "@/lib/format";
import type { AskObserved, GapList, InsightInfo, InsightList, InsightResult } from "@/lib/types";

export type MonitorsSubTab = "monitors" | "checks" | "gaps";
export type BenchmarkStatus = "not_run" | "ran" | "error" | "verified" | "flagged";

export interface BenchmarkRunRecord {
  ok: boolean;
  rowCount: number;
  elapsedMs: number;
  ranAt: string;
  headline?: string | null;
  sql?: string | null;
  asOf?: string | null;
  error?: string | null;
}

export interface BenchmarkCheck {
  id: string;
  question: string;
  category: string;
  expectedMeasure: string;
  tileId?: string;
  customRule?: string;
  status: BenchmarkStatus;
  source: "catalog" | "custom";
  owner?: string;
  lastRun?: BenchmarkRunRecord | null;
  lastCheckedAt?: string | null;
}

export const BENCHMARKS_STORAGE_KEY = "plx.insights.benchmarks.v2";
const BENCHMARK_PAGE_SIZE = 12;

interface StoredBenchmarks {
  custom: Array<{
    id: string;
    question: string;
    category: string;
    expectedMeasure: string;
    customRule?: string;
    createdAt: string;
  }>;
  verdicts: Record<string, { status: "verified" | "flagged"; at: string }>;
  runs: Record<string, BenchmarkRunRecord>;
}

const EMPTY_STORED: StoredBenchmarks = { custom: [], verdicts: {}, runs: {} };

const CATEGORIES = ["Finance", "Sales", "Purchasing", "Operations", "Quality", "Customers", "Team", "Custom"] as const;

type MeasureItem = NonNullable<InsightList["measures"]>[number];

export function loadStoredBenchmarks(): StoredBenchmarks {
  if (typeof window === "undefined") return EMPTY_STORED;
  try {
    const raw = window.localStorage.getItem(BENCHMARKS_STORAGE_KEY);
    if (!raw) return EMPTY_STORED;
    const parsed = JSON.parse(raw) as Partial<StoredBenchmarks> | null;
    if (!parsed || typeof parsed !== "object") return EMPTY_STORED;
    const custom = Array.isArray(parsed.custom)
      ? parsed.custom.filter(
          (x) => !!x && typeof x === "object" && typeof x.id === "string" && typeof x.question === "string",
        )
      : [];
    const verdicts: StoredBenchmarks["verdicts"] = {};
    if (parsed.verdicts && typeof parsed.verdicts === "object") {
      for (const [k, v] of Object.entries(parsed.verdicts)) {
        if (v && (v.status === "verified" || v.status === "flagged")) {
          verdicts[k] = { status: v.status, at: typeof v.at === "string" ? v.at : "" };
        }
      }
    }
    const runs: StoredBenchmarks["runs"] = {};
    if (parsed.runs && typeof parsed.runs === "object") {
      for (const [k, v] of Object.entries(parsed.runs)) {
        if (v && typeof v === "object" && typeof v.ranAt === "string") {
          runs[k] = v;
        }
      }
    }
    return { custom, verdicts, runs };
  } catch {
    return EMPTY_STORED;
  }
}

export function saveStoredBenchmarks(data: StoredBenchmarks): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(BENCHMARKS_STORAGE_KEY, JSON.stringify(data));
  } catch {
    // ignore storage quota errors
  }
}

function summarizeInsightResult(item: InsightInfo, res: InsightResult): { headline: string | null; rowCount: number } {
  const rowCount = res.row_count ?? res.rows?.length ?? 0;
  const kpi = extractKpiSummary(item, res);
  if (kpi?.headline) {
    return { headline: kpi.headline, rowCount };
  }
  return { headline: null, rowCount };
}

/**
 * Pure: build honest accuracy checks ONLY from real catalog insights, real measures, and user-added checks.
 * Every check starts as `not_run` until actually executed or manually marked by a human — ZERO fake Checked states, ZERO fabricated SQL.
 */
export function buildAccuracyChecks(
  measures: readonly MeasureItem[],
  items: readonly InsightInfo[],
  stored: StoredBenchmarks,
): BenchmarkCheck[] {
  const out: BenchmarkCheck[] = [];
  const seenIds = new Set<string>();
  const seenMeasureIds = new Set<string>();

  const resolveStatus = (id: string): { status: BenchmarkStatus; lastRun: BenchmarkRunRecord | null; lastCheckedAt: string | null } => {
    const verdict = stored.verdicts[id];
    const run = stored.runs[id] ?? null;
    if (verdict) {
      return { status: verdict.status, lastRun: run, lastCheckedAt: verdict.at };
    }
    if (run) {
      return { status: run.ok ? "ran" : "error", lastRun: run, lastCheckedAt: run.ranAt };
    }
    return { status: "not_run", lastRun: null, lastCheckedAt: null };
  };

  // 1. User-added custom checks first
  for (const c of stored.custom) {
    const st = resolveStatus(c.id);
    seenIds.add(c.id);
    out.push({
      id: c.id,
      question: c.question,
      category: c.category,
      expectedMeasure: c.expectedMeasure,
      customRule: c.customRule,
      status: st.status,
      source: "custom",
      lastRun: st.lastRun,
      lastCheckedAt: st.lastCheckedAt,
    });
  }

  // 2. Real governed measures (linked to their real tile id when one exists)
  for (const m of measures) {
    const id = `measure_check_${m.id}`;
    if (seenIds.has(id)) continue;
    seenIds.add(id);
    seenMeasureIds.add(m.id);
    const tile = items.find((t) => t.measure?.id === m.id || t.id === `measure_${m.id}`);
    const st = resolveStatus(id);
    out.push({
      id,
      question: m.ask?.trim() || `Check ${m.name} (${m.period})`,
      category: m.owner || (tile?.group ?? "").replace(/^Measures:\s*/i, "") || "Operations",
      expectedMeasure: m.name,
      tileId: tile?.id,
      status: st.status,
      source: "catalog",
      owner: m.owner,
      lastRun: st.lastRun,
      lastCheckedAt: st.lastCheckedAt,
    });
  }

  // 3. Real catalog insight tiles (not already covered by measures)
  for (const it of items) {
    if (it.measure && seenMeasureIds.has(it.measure.id)) continue;
    const id = `tile_check_${it.id}`;
    if (seenIds.has(id)) continue;
    seenIds.add(id);
    const st = resolveStatus(id);
    out.push({
      id,
      question: it.question?.trim() || `Verify "${it.title}" against live data`,
      category: it.measure?.owner || (it.group ?? "").replace(/^Measures:\s*/i, "") || "Operations",
      expectedMeasure: it.measure?.name || it.title,
      tileId: it.id,
      status: st.status,
      source: "catalog",
      owner: it.measure?.owner,
      lastRun: st.lastRun,
      lastCheckedAt: st.lastCheckedAt,
    });
  }

  return out;
}

/** Calm inline status label + 6px dot or 12px icon — ZERO colored status boxes. */
function CheckStatusInline({ status, lastRun }: { status: BenchmarkStatus; lastRun?: BenchmarkRunRecord | null }) {
  if (status === "verified") {
    return (
      <span className="inline-flex items-center gap-1.5 text-[12px] font-medium text-ok whitespace-nowrap">
        <CheckCircle2 className="size-3 shrink-0" strokeWidth={2.2} aria-hidden />
        Verified
      </span>
    );
  }
  if (status === "flagged") {
    return (
      <span className="inline-flex items-center gap-1.5 text-[12px] font-medium text-late whitespace-nowrap">
        <Flag className="size-3 shrink-0" strokeWidth={2.2} aria-hidden />
        Flagged
      </span>
    );
  }
  if (status === "error") {
    return (
      <span
        className="inline-flex items-center gap-1.5 text-[12px] font-medium text-late whitespace-nowrap"
        title={lastRun?.error ?? "Run failed"}
      >
        <AlertCircle className="size-3 shrink-0" strokeWidth={2.2} aria-hidden />
        Error
      </span>
    );
  }
  if (status === "ran") {
    return (
      <span className="inline-flex items-center gap-2 text-[12px] text-fg whitespace-nowrap tabular-nums">
        <span className="inline-flex items-center gap-1.5">
          <span className="size-1.5 rounded-full bg-dot-ok shrink-0" aria-hidden />
          Ran
        </span>
        {lastRun ? <span className="text-muted">{lastRun.rowCount} rows</span> : null}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-[12px] text-faint whitespace-nowrap">
      <CircleDashed className="size-3 shrink-0" strokeWidth={2} aria-hidden />
      Not run
    </span>
  );
}

interface MonitorLiveRun {
  running: boolean;
  ok?: boolean;
  rowCount?: number;
  headline?: string | null;
  elapsedMs?: number;
  asOf?: string | null;
  ranAt?: string;
  error?: string | null;
}

/**
 * Monitors & Quality Workspace:
 * 3 crisp, genuinely functional tabs via Liquid Glass switcher:
 *   1. Monitors (Scheduled alerts & watches with REAL working Run now)
 *   2. Accuracy checks (Honest Not run default, real Run & Run visible, real Verify / Flag issue)
 *   3. Data gaps & feedback (Live getGaps() + Ask telemetry + FeedbackTriagePanel)
 */
export function BenchmarksPanel({
  list,
  initialSubTab = "monitors",
  monitorLine,
  onJumpToTile,
}: {
  list: InsightList;
  initialSubTab?: MonitorsSubTab;
  monitorLine?: (item: InsightInfo) => string;
  onJumpToTile?: (id: string) => void;
}) {
  const [subTab, setSubTab] = useState<MonitorsSubTab>(initialSubTab);
  const [stored, setStored] = useState<StoredBenchmarks>(() => loadStoredBenchmarks());
  const [observed, setObserved] = useState<AskObserved | null>(null);
  const [observedAccess, setObservedAccess] = useState<"loading" | "ok" | "restricted" | "error">("loading");
  const [gaps, setGaps] = useState<GapList | null>(null);
  const [gapsAccess, setGapsAccess] = useState<"loading" | "ok" | "restricted" | "error">("loading");
  const [waitingFeedbackCount, setWaitingFeedbackCount] = useState(0);

  // Monitors tab state
  const [monitorRuns, setMonitorRuns] = useState<Record<string, MonitorLiveRun>>({});
  const [watchPrompt, setWatchPrompt] = useState("");
  const activeAbortsRef = useRef<Set<AbortController>>(new Set());

  useEffect(() => {
    const aborts = activeAbortsRef.current;
    return () => {
      for (const ctl of aborts) ctl.abort();
      aborts.clear();
    };
  }, []);

  // Accuracy checks tab state
  const [query, setQuery] = useState("");
  const [categoryFilter, setCategoryFilter] = useState<string>("All");
  const [expandedSql, setExpandedSql] = useState<Record<string, boolean>>({});
  const [runningChecks, setRunningChecks] = useState<Record<string, boolean>>({});
  const [runningBatch, setRunningBatch] = useState(false);
  const [visibleLimit, setVisibleLimit] = useState(BENCHMARK_PAGE_SIZE);
  const [formOpen, setFormOpen] = useState(false);

  // Add custom check form state
  const [newQuestion, setNewQuestion] = useState("");
  const [newCategory, setNewCategory] = useState<string>("Finance");
  const [newMeasure, setNewMeasure] = useState("");
  const [newRule, setNewRule] = useState("");
  const [syncToFeedback, setSyncToFeedback] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    getObserved(ctl.signal).then(
      (got) => {
        setObserved(got);
        setObservedAccess("ok");
      },
      (err) => {
        if (ctl.signal.aborted) return;
        if (err instanceof ApiError && err.status === 403) setObservedAccess("restricted");
        else setObservedAccess("error");
      },
    );
    getGaps(ctl.signal).then(
      (got) => {
        setGaps(got);
        setGapsAccess("ok");
      },
      (err) => {
        if (ctl.signal.aborted) return;
        if (err instanceof ApiError && err.status === 403) setGapsAccess("restricted");
        else setGapsAccess("error");
      },
    );
    return () => ctl.abort();
  }, []);

  const measures = list.measures ?? [];
  const savedItems = useMemo(() => list.items.filter((i) => i.saved), [list.items]);
  const scheduledItems = useMemo(() => savedItems.filter((i) => Boolean(i.schedule)), [savedItems]);
  const catalogExamples = useMemo(() => list.items.filter((i) => !i.saved).slice(0, 5), [list.items]);

  const checks = useMemo(
    () => buildAccuracyChecks(measures, list.items, stored),
    [measures, list.items, stored],
  );

  const availableCategories = useMemo(() => {
    const set = new Set<string>(checks.map((b) => b.category));
    return ["All", ...[...set].sort((a, b) => a.localeCompare(b))];
  }, [checks]);

  const filteredChecks = useMemo(() => {
    const q = query.trim().toLowerCase();
    return checks.filter((b) => {
      if (categoryFilter !== "All" && b.category !== categoryFilter) return false;
      if (!q) return true;
      return `${b.question} ${b.expectedMeasure} ${b.category} ${b.customRule ?? ""}`.toLowerCase().includes(q);
    });
  }, [checks, categoryFilter, query]);

  const pagedChecks = useMemo(() => filteredChecks.slice(0, visibleLimit), [filteredChecks, visibleLimit]);
  const remainingChecks = Math.max(0, filteredChecks.length - pagedChecks.length);

  const telemetry = useMemo(() => {
    const days = observed?.days ?? [];
    const answered = days.reduce((s, d) => s + d.answered, 0);
    const failed = days.reduce((s, d) => s + d.failed, 0);
    const unavailable = days.reduce((s, d) => s + d.unavailable, 0);
    const total = answered + failed + unavailable;
    const passRate = total > 0 ? Math.round((answered / total) * 100) : null;
    const medians = days.map((d) => d.median_ms).filter((v): v is number => typeof v === "number" && v > 0);
    const avgMedianMs = medians.length > 0 ? Math.round(medians.reduce((a, b) => a + b, 0) / medians.length) : null;
    const openGaps = (gaps?.gaps ?? []).filter((g) => !g.defined);
    const verifiedCount = checks.filter((b) => b.status === "verified").length;
    return { answered, failed, total, passRate, avgMedianMs, openGaps, verifiedCount };
  }, [observed, gaps, checks]);

  const updateStored = (updater: (prev: StoredBenchmarks) => StoredBenchmarks) => {
    setStored((prev) => {
      const next = updater(prev);
      saveStoredBenchmarks(next);
      return next;
    });
  };

  /** Live-run a monitor insight via getInsight(item.id) */
  const runMonitorNow = async (item: InsightInfo) => {
    const ctl = new AbortController();
    activeAbortsRef.current.add(ctl);
    setMonitorRuns((prev) => ({ ...prev, [item.id]: { running: true } }));
    const started = performance.now();
    try {
      const res = await getInsight(item.id, ctl.signal, true);
      if (ctl.signal.aborted) return;
      const elapsedMs = Math.max(1, Math.round(performance.now() - started));
      const summary = summarizeInsightResult(item, res);
      setMonitorRuns((prev) => ({
        ...prev,
        [item.id]: {
          running: false,
          ok: true,
          rowCount: summary.rowCount,
          headline: summary.headline,
          elapsedMs,
          asOf: res.drawn?.as_of ?? res.as_of ?? null,
          ranAt: new Date().toISOString(),
        },
      }));
    } catch (e) {
      if (ctl.signal.aborted) return;
      setMonitorRuns((prev) => ({
        ...prev,
        [item.id]: {
          running: false,
          ok: false,
          elapsedMs: Math.max(1, Math.round(performance.now() - started)),
          ranAt: new Date().toISOString(),
          error: plainError(e),
        },
      }));
    } finally {
      activeAbortsRef.current.delete(ctl);
    }
  };

  /** Live-run a single accuracy check via getInsight(tileId) */
  const runCheck = async (check: BenchmarkCheck) => {
    if (!check.tileId) return;
    const item = list.items.find((i) => i.id === check.tileId);
    const ctl = new AbortController();
    activeAbortsRef.current.add(ctl);
    setRunningChecks((prev) => ({ ...prev, [check.id]: true }));
    const started = performance.now();
    try {
      const res = await getInsight(check.tileId, ctl.signal, true);
      if (ctl.signal.aborted) return;
      const elapsedMs = Math.max(1, Math.round(performance.now() - started));
      const summary = item ? summarizeInsightResult(item, res) : { headline: null, rowCount: res.row_count ?? res.rows?.length ?? 0 };
      const record: BenchmarkRunRecord = {
        ok: true,
        rowCount: summary.rowCount,
        elapsedMs,
        ranAt: new Date().toISOString(),
        headline: summary.headline,
        sql: res.sql ?? null,
        asOf: res.drawn?.as_of ?? res.as_of ?? null,
      };
      updateStored((prev) => ({
        ...prev,
        runs: { ...prev.runs, [check.id]: record },
      }));
    } catch (e) {
      if (ctl.signal.aborted) return;
      const record: BenchmarkRunRecord = {
        ok: false,
        rowCount: 0,
        elapsedMs: Math.max(1, Math.round(performance.now() - started)),
        ranAt: new Date().toISOString(),
        error: plainError(e),
      };
      updateStored((prev) => ({
        ...prev,
        runs: { ...prev.runs, [check.id]: record },
      }));
    } finally {
      activeAbortsRef.current.delete(ctl);
      if (!ctl.signal.aborted) {
        setRunningChecks((prev) => ({ ...prev, [check.id]: false }));
      }
    }
  };

  /** Run every tile-backed check currently visible in the Accuracy Checks table */
  const runVisibleChecks = async () => {
    const runnable = pagedChecks.filter((c) => Boolean(c.tileId));
    if (runnable.length === 0) return;
    setRunningBatch(true);
    for (const c of runnable) {
      await runCheck(c);
    }
    setRunningBatch(false);
  };

  const setCheckVerdict = async (b: BenchmarkCheck, verdict: "verified" | "flagged" | "clear") => {
    const at = new Date().toISOString();
    updateStored((prev) => {
      const nextVerdicts = { ...prev.verdicts };
      if (verdict === "clear") delete nextVerdicts[b.id];
      else nextVerdicts[b.id] = { status: verdict, at };
      return { ...prev, verdicts: nextVerdicts };
    });

    if (verdict === "flagged") {
      try {
        await sendNoteFeedback({
          kind: "page",
          note: `Accuracy check flagged: "${b.question}" (expected measure: ${b.expectedMeasure || "unspecified"}).`,
        });
        setNotice(`Flagged "${b.question.slice(0, 64)}" and logged a review note.`);
      } catch (e) {
        setNotice(`Marked flagged locally (${plainError(e)}).`);
      }
    }
  };

  const removeCustom = (id: string) => {
    updateStored((prev) => {
      const nextVerdicts = { ...prev.verdicts };
      const nextRuns = { ...prev.runs };
      delete nextVerdicts[id];
      delete nextRuns[id];
      return {
        custom: prev.custom.filter((c) => c.id !== id),
        verdicts: nextVerdicts,
        runs: nextRuns,
      };
    });
  };

  const handleAddCheck = async (e: React.FormEvent) => {
    e.preventDefault();
    const q = newQuestion.trim();
    if (!q) return;
    setSubmitting(true);
    setNotice(null);
    const created = {
      id: `custom_${Date.now()}`,
      question: q,
      category: newCategory,
      expectedMeasure: newMeasure.trim() || "Custom check",
      customRule: newRule.trim() || undefined,
      createdAt: new Date().toISOString(),
    };
    updateStored((prev) => ({
      ...prev,
      custom: [created, ...prev.custom],
    }));
    if (syncToFeedback) {
      try {
        await sendNoteFeedback({
          kind: "page",
          note: `New accuracy check [${created.category}]: "${created.question}" | Expected: ${created.expectedMeasure}${created.customRule ? ` | Rule: ${created.customRule}` : ""}`,
        });
        setNotice("Added accuracy check and logged it to the feedback queue.");
      } catch {
        setNotice("Added accuracy check in this browser.");
      }
    } else {
      setNotice("Added accuracy check in this browser.");
    }
    setNewQuestion("");
    setNewMeasure("");
    setNewRule("");
    setSubmitting(false);
    setFormOpen(false);
  };

  const prefillFromGap = (gapName: string, kind: "view" | "measure") => {
    setSubTab("checks");
    setFormOpen(true);
    setNewQuestion(`What is the current ${gapName.replace(/_/g, " ")} breakdown by month?`);
    setNewMeasure(gapName);
    setNewRule(`Needs ${kind} definition: ${gapName}`);
    setNewCategory("Custom");
  };

  const trimmedWatch = watchPrompt.trim();
  const runnableVisibleCount = pagedChecks.filter((c) => Boolean(c.tileId)).length;
  const gapsAndFeedbackCount = telemetry.openGaps.length + waitingFeedbackCount;

  const scheduleCadenceLabel = (s: InsightInfo["schedule"]) => {
    if (s === "hourly") return "Hourly";
    if (s === "daily") return "Daily";
    if (s === "weekly") return "Weekly";
    return "Manual";
  };

  return (
    <div className="space-y-5" data-testid="benchmarks-panel">
      {/* Top Bar: Quiet hairline underline sub-tabs + Inline Telemetry */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/80">
        <div
          role="tablist"
          aria-label="Monitors and quality sections"
          className="-mb-px inline-flex items-center gap-4"
          data-testid="monitors-subview-tabs"
        >
          <button
            type="button"
            role="tab"
            aria-selected={subTab === "monitors"}
            onClick={() => setSubTab("monitors")}
            data-testid="subtab-monitors"
            className={cn(
              "inline-flex h-9 items-center gap-1.5 border-b-2 px-1 text-[13px] font-medium transition-colors cursor-pointer",
              subTab === "monitors"
                ? "border-fg text-fg"
                : "border-transparent text-muted hover:border-border-strong hover:text-fg",
            )}
          >
            <Bell className="size-3.5 shrink-0" strokeWidth={1.9} aria-hidden />
            Monitors
            <span className="num text-[11px] opacity-75">{savedItems.length}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={subTab === "checks"}
            onClick={() => setSubTab("checks")}
            data-testid="subtab-accuracy"
            className={cn(
              "inline-flex h-9 items-center gap-1.5 border-b-2 px-1 text-[13px] font-medium transition-colors cursor-pointer",
              subTab === "checks"
                ? "border-fg text-fg"
                : "border-transparent text-muted hover:border-border-strong hover:text-fg",
            )}
          >
            <ShieldCheck className="size-3.5 shrink-0" strokeWidth={1.9} aria-hidden />
            Accuracy checks
            <span className="num text-[11px] opacity-75">{checks.length}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={subTab === "gaps"}
            onClick={() => setSubTab("gaps")}
            data-testid="subtab-gaps-feedback"
            className={cn(
              "inline-flex h-9 items-center gap-1.5 border-b-2 px-1 text-[13px] font-medium transition-colors cursor-pointer",
              subTab === "gaps"
                ? "border-fg text-fg"
                : "border-transparent text-muted hover:border-border-strong hover:text-fg",
            )}
          >
            <MessageSquareWarning className="size-3.5 shrink-0" strokeWidth={1.9} aria-hidden />
            Data gaps &amp; feedback
            {gapsAndFeedbackCount > 0 && (
              <span className="num text-[11px] opacity-75">{gapsAndFeedbackCount}</span>
            )}
          </button>
        </div>

        {/* Quiet Inline Telemetry Readout (zero boxed KPI cards) */}
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 pb-1.5 text-[12px] text-muted tabular-nums">
          <span data-testid="bench-kpi-accuracy">
            {observedAccess === "loading"
              ? "Telemetry loading"
              : telemetry.passRate !== null
                ? `${telemetry.passRate}% Ask answer rate`
                : observedAccess === "restricted"
                  ? "Telemetry: admin only"
                  : "No recent Ask runs"}
          </span>
          {telemetry.avgMedianMs !== null && (
            <>
              <span className="h-3 w-px bg-border" aria-hidden />
              <span data-testid="bench-kpi-speed">{(telemetry.avgMedianMs / 1000).toFixed(1)}s median</span>
            </>
          )}
          <span className="h-3 w-px bg-border" aria-hidden />
          <span data-testid="bench-kpi-gaps">
            {gapsAccess === "restricted"
              ? "Gaps: admin only"
              : `${telemetry.openGaps.length} open ${telemetry.openGaps.length === 1 ? "gap" : "gaps"}`}
          </span>
          <span className="h-3 w-px bg-border" aria-hidden />
          <span data-testid="bench-kpi-verified">
            {telemetry.verifiedCount}/{checks.length} verified
          </span>
        </div>
      </div>

      {/* TAB 1: MONITORS (Scheduled alerts & watches with REAL Run now) */}
      {subTab === "monitors" && (
        <section className="space-y-4" aria-label="Scheduled alerts and watches" data-testid="monitors-tab-panel">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-[15px] font-semibold tracking-[-0.015em] text-fg">
                Scheduled alerts &amp; watches
              </h2>
              <p className="text-[12.5px] text-muted">
                Saved charts and threshold monitors that run live against warehouse views as you.
              </p>
            </div>
            <Link
              prefetch={false}
              href={`/ask?q=${encodeURIComponent("Set up a daily monitor for late purchase orders over 10")}`}
              data-testid="new-monitor-link"
              className="inline-flex h-8 items-center gap-1.5 rounded-md bg-fg px-3 text-[12.5px] font-medium text-bg hover:opacity-90"
            >
              <Plus className="size-3.5 shrink-0" strokeWidth={2.2} aria-hidden />
              New monitor
            </Link>
          </div>

          {/* Quick natural-language monitor bar */}
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-bg p-2 focus-within:border-border-strong">
            <Bell className="ml-1 size-4 shrink-0 text-faint" strokeWidth={1.8} aria-hidden />
            <input
              type="text"
              value={watchPrompt}
              onChange={(e) => setWatchPrompt(e.target.value)}
              placeholder="Tell me when USD monthly revenue drops under 400k, or late POs exceed 15"
              aria-label="Create proactive monitor"
              className="min-w-[220px] flex-1 bg-transparent text-[13px] text-fg outline-none placeholder:text-faint"
            />
            {trimmedWatch.length > 2 ? (
              <Link
                prefetch={false}
                href={`/ask?q=${encodeURIComponent(/^tell me when\b/i.test(trimmedWatch) ? trimmedWatch : `Tell me when ${trimmedWatch}`)}`}
                className="inline-flex h-7 items-center gap-1 rounded-md bg-fg px-3 text-[12px] font-medium text-bg hover:opacity-90"
              >
                Create in Ask
                <ArrowUpRight className="size-3" strokeWidth={2} aria-hidden />
              </Link>
            ) : (
              <span className="inline-flex h-7 items-center rounded-md border border-border px-3 text-[12px] font-medium text-faint">
                Create in Ask
              </span>
            )}
          </div>

          {savedItems.length === 0 ? (
            <div
              className="space-y-4 rounded-lg border border-border/80 bg-surface/50 p-5"
              data-testid="monitors-empty-state"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="space-y-1">
                  <p className="text-[13.5px] font-medium text-fg">No monitors saved yet</p>
                  <p className="text-[12.5px] text-muted">
                    Ask any question and pin the chart, or start from one of your live catalog views below to watch it on a schedule.
                  </p>
                </div>
                <Link
                  prefetch={false}
                  href={`/ask?q=${encodeURIComponent("Track late purchase orders by supplier daily and alert me when new ones appear")}`}
                  className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md bg-fg px-3 text-[12px] font-medium text-bg hover:opacity-90"
                >
                  <Plus className="size-3.5" strokeWidth={2.2} aria-hidden />
                  Create a monitor
                </Link>
              </div>

              {catalogExamples.length > 0 && (
                <div className="space-y-2 border-t border-border/70 pt-3">
                  <div className="text-[11.5px] font-medium uppercase tracking-[0.04em] text-faint">
                    Quick-start from live catalog views
                  </div>
                  <ul className="divide-y divide-border/60">
                    {catalogExamples.map((it) => {
                      const run = monitorRuns[it.id];
                      return (
                        <li key={it.id} className="flex flex-wrap items-center justify-between gap-3 py-2 text-[12.5px]">
                          <div className="min-w-0 flex-1">
                            <span className="font-medium text-fg">{it.title}</span>
                            <span className="ml-2 text-[12px] text-faint">{(it.group ?? "").replace(/^Measures:\s*/i, "")}</span>
                            {run && !run.running && (
                              <div className="mt-0.5 text-[12px] tabular-nums">
                                {run.ok ? (
                                  <span className="inline-flex items-center gap-2 text-muted">
                                    <span className="size-1.5 rounded-full bg-dot-ok" aria-hidden />
                                    {run.headline ? <strong className="font-medium text-fg">{run.headline}</strong> : null}
                                    <span>{run.rowCount} {run.rowCount === 1 ? "row" : "rows"}</span>
                                    <span className="h-3 w-px bg-border" aria-hidden />
                                    <span>{run.elapsedMs}ms</span>
                                  </span>
                                ) : (
                                  <span className="text-late">{run.error}</span>
                                )}
                              </div>
                            )}
                          </div>
                          <div className="flex items-center gap-1.5">
                            <button
                              type="button"
                              disabled={run?.running}
                              onClick={() => void runMonitorNow(it)}
                              data-testid={`run-monitor-${it.id}`}
                              className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-bg px-2.5 text-[12px] font-medium text-fg hover:border-border-strong hover:bg-hover disabled:opacity-50 cursor-pointer"
                            >
                              {run?.running ? (
                                <Loader2 className="size-3 animate-spin" aria-hidden />
                              ) : (
                                <Play className="size-3" strokeWidth={2.2} aria-hidden />
                              )}
                              Run now
                            </button>
                            <Link
                              prefetch={false}
                              href={`/ask?q=${encodeURIComponent(`Monitor "${it.title}" daily and alert me on changes`)}`}
                              className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-bg px-2.5 text-[12px] font-medium text-muted hover:border-border-strong hover:text-fg"
                            >
                              Schedule in Ask
                              <ArrowUpRight className="size-3" aria-hidden />
                            </Link>
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}
            </div>
          ) : (
            <div className="space-y-2">
              <div className="inline-flex items-center gap-2.5 text-[12px] text-muted tabular-nums">
                <span>{scheduledItems.length} scheduled</span>
                <span className="h-3 w-px bg-border" aria-hidden />
                <span>{savedItems.length} total saved</span>
              </div>
              <div className="overflow-x-auto border-y border-border/80">
                <table className="w-full border-collapse text-left text-[12.5px]" data-testid="monitors-table">
                  <thead className="border-b border-border text-[11.5px] text-faint">
                    <tr>
                      <th className="py-2 pr-3 font-medium">Monitor</th>
                      <th className="px-3 py-2 font-medium">Cadence &amp; rule</th>
                      <th className="px-3 py-2 font-medium">Last run / Live check</th>
                      <th className="py-2 pl-3 text-right font-medium">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/60">
                    {savedItems.map((it) => {
                      const run = monitorRuns[it.id];
                      const active = Boolean(it.schedule);
                      return (
                        <tr key={it.id} className="align-top transition-colors hover:bg-hover/40" data-testid={`monitor-row-${it.id}`}>
                          <td className="py-2.5 pr-3">
                            <div className="flex items-center gap-2">
                              <span
                                className={cn("size-1.5 shrink-0 rounded-full", active ? "bg-dot-ok" : "bg-dot-neutral")}
                                aria-hidden
                              />
                              {onJumpToTile ? (
                                <button
                                  type="button"
                                  onClick={() => onJumpToTile(it.id)}
                                  className="font-medium text-fg hover:underline cursor-pointer text-left"
                                >
                                  {it.title}
                                </button>
                              ) : (
                                <span className="font-medium text-fg">{it.title}</span>
                              )}
                            </div>
                          </td>
                          <td className="px-3 py-2.5 text-muted">
                            <div className="inline-flex items-center gap-1.5 text-fg">
                              <Clock className="size-3 text-faint" strokeWidth={1.9} aria-hidden />
                              <span>{scheduleCadenceLabel(it.schedule)}</span>
                            </div>
                            {active && monitorLine && (
                              <div className="mt-0.5 text-[11.5px] text-faint">{monitorLine(it)}</div>
                            )}
                          </td>
                          <td className="px-3 py-2.5 text-[12px] tabular-nums" data-testid={`monitor-result-${it.id}`}>
                            {run?.running ? (
                              <span className="inline-flex items-center gap-1.5 text-muted">
                                <Loader2 className="size-3 animate-spin" aria-hidden />
                                Running live query...
                              </span>
                            ) : run ? (
                              run.ok ? (
                                <div className="space-y-0.5">
                                  <div className="inline-flex items-center gap-2 text-fg">
                                    <span className="size-1.5 rounded-full bg-dot-ok" aria-hidden />
                                    {run.headline && <span className="font-semibold">{run.headline}</span>}
                                    <span className="text-muted">
                                      {run.rowCount} {run.rowCount === 1 ? "row" : "rows"}
                                    </span>
                                    <span className="h-3 w-px bg-border" aria-hidden />
                                    <span className="text-muted">{run.elapsedMs}ms</span>
                                  </div>
                                  {run.asOf && (
                                    <div className="text-[11px] text-faint">As of {formatDate(run.asOf, { short: true })}</div>
                                  )}
                                </div>
                              ) : (
                                <span className="inline-flex items-center gap-1 text-late">
                                  <AlertCircle className="size-3 shrink-0" aria-hidden />
                                  {run.error}
                                </span>
                              )
                            ) : (
                              <span className="text-faint">
                                {it.last_run ? formatDateTime(it.last_run) : active ? "Awaiting first scheduled run" : "Not run yet"}
                              </span>
                            )}
                          </td>
                          <td className="py-2.5 pl-3 text-right whitespace-nowrap">
                            <div className="inline-flex items-center gap-1.5">
                              <button
                                type="button"
                                disabled={run?.running}
                                onClick={() => void runMonitorNow(it)}
                                data-testid={`run-monitor-${it.id}`}
                                className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-bg px-2.5 text-[12px] font-medium text-fg hover:border-border-strong hover:bg-hover disabled:opacity-50 cursor-pointer"
                              >
                                {run?.running ? (
                                  <Loader2 className="size-3 animate-spin" aria-hidden />
                                ) : (
                                  <Play className="size-3" strokeWidth={2.2} aria-hidden />
                                )}
                                Run now
                              </button>
                              <Link
                                prefetch={false}
                                href={`/ask?q=${encodeURIComponent(it.question || `Explain recent changes in "${it.title}"`)}`}
                                className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-bg px-2.5 text-[12px] font-medium text-muted hover:border-border-strong hover:text-fg"
                              >
                                Open in Ask
                                <ArrowUpRight className="size-3" aria-hidden />
                              </Link>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </section>
      )}

      {/* TAB 2: ACCURACY CHECKS (Honest Not run default, real Run & Run visible, Verify / Flag issue) */}
      {subTab === "checks" && (
        <section className="space-y-4" aria-label="Accuracy checks" data-testid="accuracy-checks-panel">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-[15px] font-semibold tracking-[-0.015em] text-fg">
                Accuracy checks
              </h2>
              <p className="text-[12.5px] text-muted">
                Execute live insight queries or test questions in Ask, then mark checks Verified or Flag an issue.
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {runnableVisibleCount > 0 && (
                <button
                  type="button"
                  disabled={runningBatch}
                  onClick={() => void runVisibleChecks()}
                  data-testid="run-visible-checks"
                  className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border-strong bg-bg px-3 text-[12px] font-medium text-fg hover:bg-hover disabled:opacity-50 cursor-pointer"
                >
                  {runningBatch ? (
                    <Loader2 className="size-3.5 animate-spin" aria-hidden />
                  ) : (
                    <Play className="size-3" strokeWidth={2.2} aria-hidden />
                  )}
                  Run visible ({runnableVisibleCount})
                </button>
              )}
              <button
                type="button"
                onClick={() => setFormOpen((v) => !v)}
                aria-expanded={formOpen}
                data-testid="add-benchmark-toggle"
                className="inline-flex h-8 items-center gap-1.5 rounded-md bg-fg px-3 text-[12px] font-medium text-bg hover:opacity-90 cursor-pointer"
              >
                <Plus className="size-3.5 shrink-0" strokeWidth={2.2} aria-hidden />
                Add check
              </button>
            </div>
          </div>

          {notice && (
            <p className="border-l-2 border-border-strong pl-3 py-1 text-[12.5px] text-muted" role="status" data-testid="benchmark-notice">
              {notice}
            </p>
          )}

          {formOpen && (
            <form
              onSubmit={handleAddCheck}
              className="space-y-3 rounded-lg border border-border/80 bg-surface/50 p-4"
              data-testid="add-benchmark-form"
            >
              <div className="grid grid-cols-1 gap-3 md:grid-cols-12">
                <label className="space-y-1 md:col-span-6">
                  <span className="block text-[12px] font-medium text-fg">Question to check</span>
                  <input
                    type="text"
                    required
                    value={newQuestion}
                    onChange={(e) => setNewQuestion(e.target.value)}
                    placeholder="Example: What was total revenue by customer last quarter?"
                    className="h-8 w-full rounded-md border border-border-strong bg-bg px-2.5 text-[13px] text-fg placeholder:text-faint"
                  />
                </label>
                <label className="space-y-1 md:col-span-3">
                  <span className="block text-[12px] font-medium text-fg">Domain</span>
                  <select
                    value={newCategory}
                    onChange={(e) => setNewCategory(e.target.value)}
                    className="h-8 w-full rounded-md border border-border-strong bg-bg px-2.5 text-[13px] text-fg"
                  >
                    {CATEGORIES.map((c) => (
                      <option key={c} value={c}>{c}</option>
                    ))}
                  </select>
                </label>
                <label className="space-y-1 md:col-span-3">
                  <span className="block text-[12px] font-medium text-fg">Expected measure</span>
                  <input
                    type="text"
                    list="benchmark-measure-options"
                    value={newMeasure}
                    onChange={(e) => setNewMeasure(e.target.value)}
                    placeholder="Revenue invoiced"
                    className="h-8 w-full rounded-md border border-border-strong bg-bg px-2.5 text-[13px] text-fg placeholder:text-faint"
                  />
                  <datalist id="benchmark-measure-options">
                    {measures.map((m) => (
                      <option key={m.id} value={m.name} />
                    ))}
                  </datalist>
                </label>
              </div>
              <label className="block space-y-1">
                <span className="block text-[12px] font-medium text-fg">Verification note or SQL (optional)</span>
                <textarea
                  rows={2}
                  value={newRule}
                  onChange={(e) => setNewRule(e.target.value)}
                  placeholder="Optional SQL query or reconciliation rule to compare against"
                  className="w-full rounded-md border border-border-strong bg-bg px-2.5 py-2 font-mono text-[12px] text-fg placeholder:text-faint"
                />
              </label>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <label className="inline-flex items-center gap-2 text-[12px] text-muted cursor-pointer">
                  <input
                    type="checkbox"
                    checked={syncToFeedback}
                    onChange={(e) => setSyncToFeedback(e.target.checked)}
                    className="rounded border-control"
                  />
                  Also log to the feedback queue
                </label>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setFormOpen(false)}
                    className="h-8 rounded-md border border-border px-3 text-[12px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={submitting || !newQuestion.trim()}
                    className="h-8 rounded-md bg-fg px-3 text-[12px] font-medium text-bg hover:opacity-90 disabled:opacity-50 cursor-pointer"
                  >
                    {submitting ? "Saving" : "Save check"}
                  </button>
                </div>
              </div>
            </form>
          )}

          {/* Filter Bar */}
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex h-8 min-w-[220px] flex-1 items-center gap-2 rounded-md border border-border bg-bg px-2.5 text-[12.5px] focus-within:border-border-strong">
              <Search className="size-3.5 shrink-0 text-faint" strokeWidth={2} aria-hidden />
              <input
                type="search"
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setVisibleLimit(BENCHMARK_PAGE_SIZE);
                }}
                placeholder="Filter accuracy checks or measures"
                aria-label="Filter accuracy checks"
                className="min-w-0 flex-1 bg-transparent text-fg outline-none focus-visible:outline-none placeholder:text-faint"
              />
            </label>
            <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Filter by domain">
              {availableCategories.map((cat) => (
                <button
                  key={cat}
                  type="button"
                  aria-pressed={categoryFilter === cat}
                  onClick={() => {
                    setCategoryFilter(cat);
                    setVisibleLimit(BENCHMARK_PAGE_SIZE);
                  }}
                  className={cn(
                    "h-7 rounded-md px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
                    categoryFilter === cat
                      ? "bg-surface-2 text-fg"
                      : "text-muted hover:bg-hover hover:text-fg",
                  )}
                >
                  {cat}
                </button>
              ))}
            </div>
          </div>

          {/* Flush Table of Accuracy Checks */}
          {filteredChecks.length === 0 ? (
            <p className="py-8 text-center text-[13px] text-muted">
              No accuracy checks match this filter.
            </p>
          ) : (
            <div className="overflow-x-auto border-y border-border/80">
              <table className="w-full border-collapse text-left text-[12.5px]" data-testid="accuracy-checks-table">
                <thead className="border-b border-border text-[11.5px] text-faint">
                  <tr>
                    <th className="py-2 pr-3 font-medium">Question &amp; measure</th>
                    <th className="px-3 py-2 font-medium">Domain</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="px-3 py-2 font-medium">Latest run</th>
                    <th className="py-2 pl-3 text-right font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/60">
                  {pagedChecks.map((b) => {
                    const isRunning = Boolean(runningChecks[b.id]);
                    const sqlText = b.lastRun?.sql || b.customRule || null;
                    const sqlOpen = Boolean(expandedSql[b.id] && sqlText);
                    return (
                      <tr key={b.id} className="align-top transition-colors hover:bg-hover/40" data-testid={`benchmark-row-${b.id}`}>
                        <td className="py-2.5 pr-3">
                          <div className="font-medium text-fg">{b.question}</div>
                          <div className="mt-0.5 text-[11.5px] text-faint">
                            Measure: <span className="text-muted">{b.expectedMeasure}</span>
                          </div>
                          {sqlOpen && sqlText && (
                            <pre className="mt-2 max-w-2xl overflow-x-auto whitespace-pre-wrap rounded-md border border-border bg-bg p-2 font-mono text-[11.5px] leading-5 text-fg">
                              {sqlText}
                            </pre>
                          )}
                        </td>
                        <td className="px-3 py-2.5 text-muted whitespace-nowrap">{b.category}</td>
                        <td className="px-3 py-2.5 whitespace-nowrap" data-testid={`check-status-${b.id}`}>
                          <CheckStatusInline status={b.status} lastRun={b.lastRun} />
                        </td>
                        <td className="px-3 py-2.5 text-[12px] text-muted tabular-nums whitespace-nowrap" data-testid={`check-run-meta-${b.id}`}>
                          {isRunning ? (
                            <span className="inline-flex items-center gap-1.5 text-muted">
                              <Loader2 className="size-3 animate-spin" aria-hidden />
                              Running...
                            </span>
                          ) : b.lastRun ? (
                            b.lastRun.ok ? (
                              <div>
                                <div className="inline-flex items-center gap-2 text-fg">
                                  {b.lastRun.headline && (
                                    <>
                                      <span>{b.lastRun.headline}</span>
                                      <span className="h-3 w-px bg-border" aria-hidden />
                                    </>
                                  )}
                                  <span>{b.lastRun.rowCount} {b.lastRun.rowCount === 1 ? "row" : "rows"}</span>
                                  <span className="h-3 w-px bg-border" aria-hidden />
                                  <span className="text-muted">{b.lastRun.elapsedMs}ms</span>
                                </div>
                                <div className="text-[11px] text-faint">{formatDateTime(b.lastRun.ranAt)}</div>
                              </div>
                            ) : (
                              <span className="text-late">{b.lastRun.error}</span>
                            )
                          ) : (
                            <span className="text-faint">—</span>
                          )}
                        </td>
                        <td className="py-2.5 pl-3 text-right whitespace-nowrap">
                          <div className="inline-flex items-center gap-1.5">
                            {b.tileId ? (
                              <button
                                type="button"
                                disabled={isRunning}
                                onClick={() => void runCheck(b)}
                                data-testid={`run-check-${b.id}`}
                                className="inline-flex h-7 items-center gap-1 rounded-md bg-fg px-2.5 text-[11.5px] font-medium text-bg hover:opacity-90 disabled:opacity-50 cursor-pointer"
                              >
                                {isRunning ? (
                                  <Loader2 className="size-3 animate-spin" aria-hidden />
                                ) : (
                                  <Play className="size-3" strokeWidth={2.2} aria-hidden />
                                )}
                                Run
                              </button>
                            ) : (
                              <Link
                                prefetch={false}
                                href={`/ask?q=${encodeURIComponent(b.question)}`}
                                data-testid={`test-in-ask-${b.id}`}
                                className="inline-flex h-7 items-center gap-1 rounded-md bg-fg px-2.5 text-[11.5px] font-medium text-bg hover:opacity-90"
                              >
                                <Play className="size-3" strokeWidth={2.2} aria-hidden />
                                Run in Ask
                              </Link>
                            )}

                            {sqlText && (
                              <button
                                type="button"
                                aria-expanded={sqlOpen}
                                onClick={() => setExpandedSql((prev) => ({ ...prev, [b.id]: !prev[b.id] }))}
                                className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-bg px-2 text-[11.5px] font-medium text-muted hover:border-border-strong hover:text-fg cursor-pointer"
                              >
                                <Code2 className="size-3" strokeWidth={2} aria-hidden />
                                SQL
                              </button>
                            )}

                            <button
                              type="button"
                              onClick={() => void setCheckVerdict(b, b.status === "verified" ? "clear" : "verified")}
                              data-testid={`verify-check-${b.id}`}
                              className={cn(
                                "inline-flex h-7 items-center gap-1 rounded-md border px-2 text-[11.5px] font-medium cursor-pointer",
                                b.status === "verified"
                                  ? "border-border-strong bg-surface-2 text-fg"
                                  : "border-border bg-bg text-muted hover:border-border-strong hover:text-fg",
                              )}
                            >
                              <CheckCircle2 className="size-3" strokeWidth={2} aria-hidden />
                              {b.status === "verified" ? "Verified" : "Verify"}
                            </button>

                            <button
                              type="button"
                              onClick={() => void setCheckVerdict(b, "flagged")}
                              data-testid={`flag-check-${b.id}`}
                              className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-bg px-2 text-[11.5px] font-medium text-muted hover:border-border-strong hover:text-late cursor-pointer"
                            >
                              <Flag className="size-3" strokeWidth={2} aria-hidden />
                              Flag issue
                            </button>

                            {b.tileId && (
                              <Link
                                prefetch={false}
                                href={`/ask?q=${encodeURIComponent(b.question)}`}
                                data-testid={`test-in-ask-${b.id}`}
                                title="Open question in Ask"
                                className="inline-flex h-7 items-center rounded-md px-1.5 text-[11.5px] font-medium text-muted hover:bg-hover hover:text-fg"
                              >
                                Ask
                              </Link>
                            )}

                            {b.source === "custom" && (
                              <button
                                type="button"
                                onClick={() => removeCustom(b.id)}
                                aria-label={`Delete check ${b.question}`}
                                className="inline-flex size-7 items-center justify-center rounded-md text-muted hover:bg-hover hover:text-late cursor-pointer"
                              >
                                <Trash2 className="size-3.5" strokeWidth={2} aria-hidden />
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {remainingChecks > 0 && (
            <div className="flex items-center justify-between gap-2 pt-1">
              <span className="text-[12px] text-muted tabular-nums">
                Showing {pagedChecks.length} of {filteredChecks.length} checks
              </span>
              <button
                type="button"
                onClick={() => setVisibleLimit((n) => n + BENCHMARK_PAGE_SIZE)}
                className="h-7 rounded-md border border-border bg-bg px-3 text-[12px] font-medium text-fg hover:border-border-strong hover:bg-hover cursor-pointer"
              >
                Show {Math.min(BENCHMARK_PAGE_SIZE, remainingChecks)} more
              </button>
            </div>
          )}
        </section>
      )}

      {/* TAB 3: DATA GAPS & FEEDBACK (Flush list of live getGaps + Ask failures + FeedbackTriagePanel) */}
      {subTab === "gaps" && (
        <div className="space-y-6" data-testid="gaps-feedback-panel">
          {/* Open Definition Gaps */}
          <section className="space-y-3" aria-label="Definition gaps">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <div>
                <h2 className="text-[15px] font-semibold tracking-[-0.015em] text-fg">
                  Data &amp; definition gaps
                </h2>
                <p className="text-[12.5px] text-muted">
                  Terms and views people asked for that do not yet have a governed SQL definition.
                </p>
              </div>
              <span className="text-[12px] text-faint tabular-nums">
                {gapsAccess === "ok" ? `${telemetry.openGaps.length} open` : gapsAccess === "restricted" ? "Admin only" : ""}
              </span>
            </div>

            {gapsAccess === "loading" ? (
              <p className="text-[12.5px] text-muted">Checking requested gaps</p>
            ) : gapsAccess === "restricted" ? (
              <p className="text-[12.5px] text-muted">Definition gap tracking is limited to the owner and admins.</p>
            ) : gapsAccess === "error" || !gaps?.on ? (
              <p className="text-[12.5px] text-muted">Gap tracking is not active on this server.</p>
            ) : gaps.gaps.length === 0 ? (
              <p className="py-2 text-[12.5px] text-muted">
                No undefined views or measures have been requested yet.
              </p>
            ) : (
              <div className="overflow-x-auto border-y border-border/80">
                <table className="w-full border-collapse text-left text-[12.5px]">
                  <thead className="border-b border-border text-[11.5px] text-faint">
                    <tr>
                      <th className="py-2 pr-3 font-medium">Requested term</th>
                      <th className="px-3 py-2 font-medium">Kind</th>
                      <th className="px-3 py-2 font-medium">Status</th>
                      <th className="px-3 py-2 text-right font-medium">Asked</th>
                      <th className="py-2 pl-3 text-right font-medium">Action</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/60">
                    {gaps.gaps.slice(0, 20).map((g) => (
                      <tr key={`${g.kind}-${g.name}`} className="hover:bg-hover/40">
                        <td className="py-2 pr-3 font-medium text-fg">{g.name}</td>
                        <td className="px-3 py-2 text-muted">{g.kind}</td>
                        <td className="px-3 py-2">
                          <span className={cn("inline-flex items-center gap-1.5 text-[12px]", g.defined ? "text-ok" : "text-muted")}>
                            <span className={cn("size-1.5 rounded-full", g.defined ? "bg-dot-ok" : "bg-dot-risk")} aria-hidden />
                            {g.defined ? "Defined" : "Open"}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-right text-fg tabular-nums">{g.asked}</td>
                        <td className="py-2 pl-3 text-right">
                          <button
                            type="button"
                            onClick={() => prefillFromGap(g.name, g.kind)}
                            className="text-[12px] font-medium text-accent hover:underline cursor-pointer"
                          >
                            Add check
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* Recent Ask Failures (only shown when present) */}
          {observedAccess === "ok" && observed?.available && observed.failures.length > 0 && (
            <section className="space-y-2.5 border-t border-border pt-6" aria-label="Recent failed Ask questions">
              <div className="flex items-baseline justify-between gap-2">
                <h3 className="text-[14px] font-semibold tracking-[-0.01em] text-fg">Recent failed Ask questions</h3>
                <span className="text-[12px] text-faint tabular-nums">{observed.failures.length} recent</span>
              </div>
              <ul className="divide-y divide-border/60 border-y border-border/80">
                {observed.failures.slice(0, 8).map((f, idx) => (
                  <li key={`${f.at}-${idx}`} className="flex flex-wrap items-center justify-between gap-3 py-2.5 text-[12.5px]">
                    <div className="min-w-0 flex-1">
                      <span className="font-medium text-fg">{f.question || "Untitled question"}</span>
                      {f.error && <span className="ml-2 text-late">({f.error})</span>}
                    </div>
                    {f.question && (
                      <Link
                        prefetch={false}
                        href={`/ask?q=${encodeURIComponent(f.question)}`}
                        className="inline-flex shrink-0 items-center gap-1 text-[12px] font-medium text-accent hover:underline"
                      >
                        Re-test in Ask
                        <ExternalLink className="size-3" aria-hidden />
                      </Link>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {/* Feedback Triage Queue */}
          <FeedbackTriagePanel onWaitingCount={setWaitingFeedbackCount} />
        </div>
      )}
    </div>
  );
}
