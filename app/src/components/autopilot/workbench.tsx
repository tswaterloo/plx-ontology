"use client";

import { Bot, CheckCircle2, CheckSquare, ChevronDown, ChevronRight, CircleAlert, ClipboardList, FileText, Mail, MessageSquare, Plus, Search, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertCard } from "@/components/autopilot/alert";
import { JobCard as PreparedJobCard } from "@/components/autopilot/job";
import { GiveJob, jobBadgeOf, JobCard, StatusIndicator, useAssistJobs, type JobBadgeInfo } from "@/components/autopilot/jobs";
import { cleanHeadline, formatSender, noteParts, threadTopicKey } from "@/components/autopilot/lines";
import { PreparedCard } from "@/components/autopilot/prepared";
import { PreparedFeedback } from "@/components/autopilot/prepared-feedback";
import { TodoCard } from "@/components/autopilot/todo";
import { FeedNote } from "@/components/feed-note";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { decideAutopilotItem, getAutopilotDrafts, type AssistJob, type DraftList, type PreparedItem } from "@/lib/api";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { useStaleRefresh } from "@/lib/stale-refresh";
import { useRead } from "@/lib/use-read";

type QueueBucket = "ready" | "input" | "waiting";
type QueueFilter = "all" | "ready" | "input" | "jobs";
type QueueSort = "priority" | "newest" | "due";

type QueueEntry =
  | { key: string; kind: "job"; job: AssistJob; sortAt: string; bucket: QueueBucket; priorityScore: number; dueScore: number; searchText: string }
  | { key: string; kind: "prepared"; item: PreparedItem; sortAt: string; bucket: QueueBucket; priorityScore: number; dueScore: number; collapsedCount: number; searchText: string };

function isWaitingItem(item: PreparedItem): boolean {
  const s = (item.summary ?? "").trim();
  const sub = (item.subject ?? "").trim();
  if (/^No answer yet\b/i.test(s) || /^No answer yet\b/i.test(sub)) return true;
  if (item.plan_status === "deferred" && !item.body?.trim() && item.kind === "todo" && (item.todos ?? []).length === 0) return true;
  return false;
}

function preparedBadgeOf(item: PreparedItem): JobBadgeInfo {
  if (item.plan_status === "working") return { label: "Drafting", tone: "new" };
  if (item.plan_status === "failed") return { label: "Needs input", tone: "risk" };
  if (item.body && item.body.trim() !== "") return { label: "Ready", tone: "ok" };
  if (item.kind === "job" && (item.jobs ?? []).length > 0) return { label: "Ready", tone: "ok" };
  if (item.kind === "alert") return { label: "Alert", tone: "late" };
  if (item.kind === "your_call") return { label: "Needs input", tone: "risk" };
  if (isWaitingItem(item)) return { label: "Waiting", tone: "neutral" };
  if (/^You said:/i.test((item.summary ?? "").trim())) return { label: "Follow up", tone: "risk" };
  if ((item.todos ?? []).length > 0) return { label: "Needs input", tone: "risk" };
  return { label: "Waiting", tone: "neutral" };
}

function bucketOfPrepared(item: PreparedItem): { bucket: QueueBucket; priority: number } {
  if (item.plan_status === "working") return { bucket: "ready", priority: 100 };
  if (item.body && item.body.trim() !== "") return { bucket: "ready", priority: 95 };
  if (item.kind === "job" && (item.jobs ?? []).length > 0) return { bucket: "ready", priority: 90 };
  if (item.kind === "alert") return { bucket: "input", priority: 80 };
  if (item.kind === "your_call") return { bucket: "input", priority: 75 };
  if (item.plan_status === "failed") return { bucket: "input", priority: 70 };
  if (isWaitingItem(item)) return { bucket: "waiting", priority: 20 };
  if (/^You said:/i.test((item.summary ?? "").trim())) return { bucket: "input", priority: 65 };
  if ((item.todos ?? []).length > 0) return { bucket: "input", priority: 60 };
  return { bucket: "waiting", priority: 15 };
}

function bucketOfJob(job: AssistJob): { bucket: QueueBucket; priority: number } {
  if (job.status === "running" || job.status === "queued" || job.status === "stopping") {
    return { bucket: "ready", priority: 105 };
  }
  const pieces = job.pieces ?? [];
  const hasPendingPieces = pieces.some((p) => p.status === "prepared" && !p.decision);
  const hasProblemPieces = pieces.some((p) => p.status !== "prepared" && p.status !== "nothing");
  const hasWithheld = (job.unread ?? []).length > 0 || (job.unread_reads ?? []).length > 0;
  if (hasPendingPieces && !hasProblemPieces && !hasWithheld) {
    return { bucket: "ready", priority: 98 };
  }
  if (job.status === "failed" || hasProblemPieces || hasWithheld) {
    return { bucket: "input", priority: 78 };
  }
  if (hasPendingPieces) {
    return { bucket: "ready", priority: 92 };
  }
  return { bucket: "waiting", priority: 10 };
}

function preparedHeadlineOf(item: PreparedItem): string {
  const sender = formatSender(item.sender).name;
  const sub = cleanHeadline(item.subject ?? "");
  const sum = cleanHeadline(item.summary ?? "");
  const ask = cleanHeadline(item.ask ?? "");
  if (item.kind === "alert") return sub || sum || "Alert";
  if (sub && !/^no answer yet/i.test(sub)) return sub;
  if (sum) return sum;
  if (ask) return ask;
  return sender || "Prepared item";
}

function preparedPreviewOf(item: PreparedItem, headline: string): string {
  const norm = (s: string) => cleanHeadline(s).toLowerCase();
  const h = norm(headline);
  const candidates = [
    item.summary ?? "",
    item.ask ?? "",
    item.message ?? "",
    noteParts(item.note ?? "").text,
  ];
  for (const raw of candidates) {
    const cleaned = raw.replace(/\s+/g, " ").trim();
    if (!cleaned) continue;
    const stripped = cleanHeadline(cleaned);
    if (!stripped) continue;
    const c = norm(stripped);
    if (!c || c === h || h.includes(c)) continue;
    return stripped;
  }
  return "";
}

function PreparedIcon({ item }: { item: PreparedItem }) {
  const cls = "size-3.5 shrink-0 text-muted";
  if (item.channel === "teams") return <MessageSquare className={cls} aria-hidden />;
  if (item.channel === "mail") return <Mail className={cls} aria-hidden />;
  if (item.kind === "todo") return <CheckSquare className={cls} aria-hidden />;
  return <FileText className={cls} aria-hidden />;
}

function isOtherPersonItem(item: PreparedItem): boolean {
  const { labels } = noteParts(item.note ?? "");
  return labels.some((l) => /this is on .+?'s list, not yours/i.test(l));
}

function itemRichness(item: PreparedItem): number {
  let score = 0;
  if (item.body && item.body.trim()) score += 4;
  if ((item.jobs ?? []).length > 0) score += 3;
  if ((item.todos ?? []).length > 0) score += 2;
  if (item.summary && item.summary.trim()) score += 1;
  return score;
}

/** Deduplicate exact duplicates and collapse RE:/Fwd: thread siblings so identical thread cards
 * never stack back-to-back in the queue. Always keeps the deep-linked item and prefers items with
 * a ready draft or newest timestamp. */
function dedupePrepared(items: PreparedItem[], keepId?: string | null): { item: PreparedItem; collapsedCount: number }[] {
  const byKey = new Map<string, { item: PreparedItem; collapsedCount: number; order: number }>();
  const unkeyed: { item: PreparedItem; collapsedCount: number; order: number }[] = [];

  items.forEach((item, idx) => {
    const isLinked = Boolean(keepId && item.item_id === keepId);
    const topic = threadTopicKey(item.subject || item.summary || "");
    const who = formatSender(item.sender).name.toLowerCase();
    const ownerScope = isOtherPersonItem(item) ? "other" : "mine";

    if (!topic) {
      unkeyed.push({ item, collapsedCount: 1, order: idx });
      return;
    }

    // Group identical thread topics across RE:/Fwd: siblings on the same channel/scope.
    const sig = `${ownerScope}|${item.channel}|${topic}|${item.kind === "alert" ? item.kind : who || "any"}`;
    const existing = byKey.get(sig);
    if (!existing) {
      byKey.set(sig, { item, collapsedCount: 1, order: idx });
      return;
    }

    const existingLinked = Boolean(keepId && existing.item.item_id === keepId);
    const keepNew =
      isLinked ||
      (!existingLinked &&
        (itemRichness(item) > itemRichness(existing.item) ||
          (itemRichness(item) === itemRichness(existing.item) && (item.created_at || "") > (existing.item.created_at || ""))));

    if (keepNew) {
      byKey.set(sig, { item, collapsedCount: existing.collapsedCount + 1, order: existing.order });
    } else {
      existing.collapsedCount += 1;
    }
  });

  return [...byKey.values(), ...unkeyed]
    .sort((a, b) => a.order - b.order)
    .map(({ item, collapsedCount }) => ({ item, collapsedCount }));
}

function dueUrgencyMs(item: PreparedItem): number {
  // Items waiting longest or marked alert/your_call/commitment surface earliest under "Due soonest".
  const ts = Date.parse(item.created_at ?? "");
  const base = Number.isNaN(ts) ? Date.now() : ts;
  if (item.kind === "alert") return base - 7 * 86_400_000;
  if (item.kind === "your_call" || /^You said:/i.test((item.summary ?? "").trim())) return base - 3 * 86_400_000;
  return base;
}

/**
 * Linear + Claude Desktop 2-pane Assist Inbox:
 * - Clean understated Claude-style top job composer.
 * - Priority-ordered, deduplicated, scannable left queue with quiet 6px status dots, search,
 *   filters, sort control, calm Mine/Team switcher, and j/k keyboard navigation.
 * - Open-canvas right reading workspace with 19px headline, 14.5px prose, single primary CTA,
 *   and elevated surfaces reserved for real deliverable artifacts.
 */
export function AssistWorkbench({ linked }: { linked: string | null }) {
  const [all, setAll] = useState(false);
  const [scope, setScope] = useState<"mine" | "everyone">("mine");
  const [filter, setFilter] = useState<QueueFilter>("all");
  const [sortBy, setSortBy] = useState<QueueSort>("priority");
  const [search, setSearch] = useState("");
  const [composerOpen, setComposerOpen] = useState(false);
  const [waitingUserToggle, setWaitingUserToggle] = useState<boolean | null>(null);

  const { jobs, error: jobsError, forbidden: jobsForbidden, replace: replaceJob } = useAssistJobs();
  const { data, error, retry, set: setDrafts } = useRead<DraftList>(
    all ? "assist-drafts-all" : "assist-drafts",
    (signal) => getAutopilotDrafts(signal, all),
  );
  useStaleRefresh(data?.stale_since, retry);

  const [gone, setGone] = useState<Set<string>>(() => new Set());
  const [toast, setToast] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const removedItems = useRef<Map<string, { item: PreparedItem; index: number }>>(new Map());
  const dataRef = useRef<DraftList | null>(data);
  dataRef.current = data;

  const showToast = useCallback((message: string) => {
    setActionError(null);
    setToast(message);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 4000);
  }, []);

  useEffect(() => {
    return () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    };
  }, []);

  const onGone = useCallback((id: string) => {
    setGone((s) => new Set(s).add(id));
  }, []);

  const onOptimistic = useCallback(
    (id: string, message: string) => {
      setGone((s) => new Set(s).add(id));
      showToast(message);
      const cur = dataRef.current;
      if (cur) {
        const index = cur.items.findIndex((i) => i.item_id === id);
        if (index >= 0) {
          removedItems.current.set(id, { item: cur.items[index], index });
          setDrafts({ ...cur, items: cur.items.filter((i) => i.item_id !== id) });
        }
      }
    },
    [setDrafts, showToast],
  );

  const onRestore = useCallback(
    (id: string, errorMessage: string) => {
      setGone((s) => {
        const next = new Set(s);
        next.delete(id);
        return next;
      });
      const saved = removedItems.current.get(id);
      removedItems.current.delete(id);
      const cur = dataRef.current;
      if (saved && cur && !cur.items.some((i) => i.item_id === id)) {
        const items = [...cur.items];
        items.splice(Math.min(saved.index, items.length), 0, saved.item);
        setDrafts({ ...cur, items });
      }
      if (toastTimer.current) clearTimeout(toastTimer.current);
      setToast(null);
      setActionError(errorMessage);
      setSelectedKey(`item:${id}`);
    },
    [setDrafts],
  );

  const allPreparedDeduped = useMemo(
    () => dedupePrepared((data?.items ?? []).filter((i) => !gone.has(i.item_id)), linked),
    [data?.items, gone, linked],
  );

  // If a deep-linked item lives on another person's list or in older items, reveal it automatically.
  useEffect(() => {
    if (!linked || !data) return;
    const match = allPreparedDeduped.find((d) => d.item.item_id === linked);
    if (match && isOtherPersonItem(match.item) && scope === "mine") {
      setScope("everyone");
    } else if (!match && !all && (data.older ?? 0) > 0) {
      setAll(true);
    }
  }, [linked, data, allPreparedDeduped, scope, all]);

  const otherCount = useMemo(
    () => allPreparedDeduped.filter((d) => isOtherPersonItem(d.item)).length,
    [allPreparedDeduped],
  );

  const scopedPrepared = useMemo(() => {
    if (scope === "everyone" || otherCount === 0) return allPreparedDeduped;
    return allPreparedDeduped.filter((d) => !isOtherPersonItem(d.item));
  }, [allPreparedDeduped, otherCount, scope]);

  const rawEntries = useMemo<QueueEntry[]>(() => {
    const list: QueueEntry[] = [
      ...(jobs ?? []).map((job): QueueEntry => {
        const { bucket, priority } = bucketOfJob(job);
        const ts = Date.parse(job.requested_at ?? "");
        return {
          key: `job:${job.job_id}`,
          kind: "job",
          job,
          sortAt: job.requested_at ?? "",
          bucket,
          priorityScore: priority,
          dueScore: Number.isNaN(ts) ? Date.now() : ts,
          searchText: `${job.text} ${job.summary ?? ""}`.toLowerCase(),
        };
      }),
      ...scopedPrepared.map(({ item, collapsedCount }): QueueEntry => {
        const { bucket, priority } = bucketOfPrepared(item);
        const sender = formatSender(item.sender);
        return {
          key: `item:${item.item_id}`,
          kind: "prepared",
          item,
          sortAt: item.created_at ?? "",
          bucket,
          priorityScore: priority,
          dueScore: dueUrgencyMs(item),
          collapsedCount,
          searchText: `${item.subject ?? ""} ${item.summary ?? ""} ${item.ask ?? ""} ${sender.name} ${sender.email}`.toLowerCase(),
        };
      }),
    ];
    return list;
  }, [jobs, scopedPrepared]);

  const filterCounts = useMemo(() => {
    let ready = 0;
    let input = 0;
    let jobCount = 0;
    for (const e of rawEntries) {
      if (e.bucket === "ready") ready += 1;
      if (e.bucket === "input") input += 1;
      if (e.kind === "job") jobCount += 1;
    }
    return { all: rawEntries.length, ready, input, jobs: jobCount };
  }, [rawEntries]);

  const filteredEntries = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rawEntries.filter((e) => {
      if (filter === "ready" && e.bucket !== "ready") return false;
      if (filter === "input" && e.bucket !== "input") return false;
      if (filter === "jobs" && e.kind !== "job") return false;
      if (q && !e.searchText.includes(q)) return false;
      return true;
    });
  }, [rawEntries, filter, search]);

  const orderedEntries = useMemo(() => {
    const copy = [...filteredEntries];
    if (sortBy === "newest") {
      copy.sort((a, b) => b.sortAt.localeCompare(a.sortAt));
      return copy;
    }
    if (sortBy === "due") {
      copy.sort((a, b) => a.dueScore - b.dueScore);
      return copy;
    }
    // Priority (best first): ready -> input -> waiting, then priorityScore desc, then newest
    const rank: Record<QueueBucket, number> = { ready: 0, input: 1, waiting: 2 };
    copy.sort((a, b) => {
      if (rank[a.bucket] !== rank[b.bucket]) return rank[a.bucket] - rank[b.bucket];
      if (b.priorityScore !== a.priorityScore) return b.priorityScore - a.priorityScore;
      return b.sortAt.localeCompare(a.sortAt);
    });
    return copy;
  }, [filteredEntries, sortBy]);

  const grouped = useMemo(() => {
    const ready: QueueEntry[] = [];
    const input: QueueEntry[] = [];
    const waiting: QueueEntry[] = [];
    for (const e of orderedEntries) {
      if (e.bucket === "ready") ready.push(e);
      else if (e.bucket === "input") input.push(e);
      else waiting.push(e);
    }
    return { ready, input, waiting };
  }, [orderedEntries]);

  const hasLinkedInWaiting = Boolean(linked && grouped.waiting.some((e) => e.key === `item:${linked}`));
  const actionableCount = grouped.ready.length + grouped.input.length;
  const waitingExpanded =
    waitingUserToggle !== null
      ? waitingUserToggle
      : actionableCount === 0 || Boolean(search.trim()) || filter !== "all" || hasLinkedInWaiting;

  // The visible flat order used for default selection and j/k keyboard navigation.
  const visibleEntries = useMemo(() => {
    if (sortBy !== "priority") return orderedEntries;
    return [
      ...grouped.ready,
      ...grouped.input,
      ...(waitingExpanded ? grouped.waiting : []),
    ];
  }, [sortBy, orderedEntries, grouped, waitingExpanded]);

  const loadingDrafts = !error && !data;
  const [selectedKey, setSelectedKey] = useState<string | null>(() => (linked ? `item:${linked}` : null));

  useEffect(() => {
    if (linked) {
      const target = `item:${linked}`;
      if (orderedEntries.some((e) => e.key === target)) {
        setSelectedKey(target);
        return;
      }
      if (loadingDrafts) return;
    }
    if (orderedEntries.length === 0) {
      setSelectedKey(null);
      return;
    }
    if (!selectedKey || !orderedEntries.some((e) => e.key === selectedKey)) {
      setSelectedKey((visibleEntries[0] ?? orderedEntries[0]).key);
    }
  }, [orderedEntries, visibleEntries, linked, selectedKey, loadingDrafts]);

  // Superhuman-style j / k keyboard navigation across visible queue items.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable)
      ) {
        return;
      }
      if (e.key !== "j" && e.key !== "k") return;
      if (visibleEntries.length === 0) return;
      const idx = visibleEntries.findIndex((x) => x.key === selectedKey);
      const nextIdx =
        e.key === "j"
          ? idx < 0
            ? 0
            : Math.min(visibleEntries.length - 1, idx + 1)
          : idx < 0
            ? 0
            : Math.max(0, idx - 1);
      if (visibleEntries[nextIdx]) {
        e.preventDefault();
        setSelectedKey(visibleEntries[nextIdx].key);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [visibleEntries, selectedKey]);

  const active = useMemo(
    () => orderedEntries.find((e) => e.key === selectedKey) ?? visibleEntries[0] ?? orderedEntries[0] ?? null,
    [orderedEntries, visibleEntries, selectedKey],
  );

  const handleJobGiven = useCallback(
    (job: AssistJob) => {
      replaceJob(job);
      setSelectedKey(`job:${job.job_id}`);
      setComposerOpen(false);
      showToast("Job started");
    },
    [replaceJob, showToast],
  );

  const quickDismissPrepared = useCallback(
    (item: PreparedItem) => {
      onOptimistic(item.item_id, "Dismissed");
      void decideAutopilotItem(item.item_id, { decision: "reject" }).catch((e) => {
        onRestore(item.item_id, plainError(e));
      });
    },
    [onOptimistic, onRestore],
  );

  const renderRow = (entry: QueueEntry) => {
    const isSelected = active?.key === entry.key;
    if (entry.kind === "job") {
      const j = entry.job;
      const badge = jobBadgeOf(j);
      const pieces = j.pieces ?? [];
      const checklist = j.checklist ?? [];
      const preview =
        j.summary?.trim() ||
        (j.planned > 0
          ? `${pieces.length} of ${j.planned} ${j.planned === 1 ? "piece" : "pieces"}`
          : checklist.length > 0
            ? `${checklist.length} checklist ${checklist.length === 1 ? "item" : "items"}`
            : "");
      return (
        <li key={entry.key} className="group relative">
          <button
            type="button"
            onClick={() => setSelectedKey(entry.key)}
            aria-current={isSelected ? "true" : undefined}
            data-testid="assist-queue-item"
            className={cn(
              "w-full px-4 py-3 text-left transition-colors cursor-pointer",
              isSelected ? "bg-row-on shadow-[inset_2px_0_0_var(--accent)]" : "hover:bg-hover",
            )}
          >
            <div className="flex items-center justify-between gap-2 text-[12px] text-muted">
              <span className="inline-flex min-w-0 items-center gap-1.5">
                <ClipboardList className={cn("size-3.5 shrink-0", isSelected ? "text-fg" : "text-faint")} aria-hidden />
                <span className="truncate font-medium text-fg/80">Assist job</span>
              </span>
              <span className="num shrink-0 text-[11.5px] text-faint">{formatDateTime(j.requested_at)}</span>
            </div>
            <p className="mt-1 truncate text-[13.5px] font-medium leading-snug text-fg">
              {j.text}
            </p>
            <div className="mt-1.5 flex items-center justify-between gap-2 text-[12px]">
              <span className="min-w-0 flex-1 truncate text-muted">
                {preview || "Background job"}
              </span>
              <StatusIndicator tone={badge.tone} label={badge.label} className="shrink-0 text-[11.5px]" />
            </div>
          </button>
        </li>
      );
    }

    const it = entry.item;
    const badge = preparedBadgeOf(it);
    const headline = preparedHeadlineOf(it);
    const preview = preparedPreviewOf(it, headline);
    const sender = formatSender(it.sender);
    const entityLabel = sender.name || (it.kind === "alert" ? "Alert" : it.channel === "teams" ? "Teams" : it.channel === "mail" ? "Mail" : "Task");

    return (
      <li key={entry.key} className="group relative">
        <button
          type="button"
          onClick={() => setSelectedKey(entry.key)}
          aria-current={isSelected ? "true" : undefined}
          data-testid="assist-queue-item"
          className={cn(
            "w-full px-4 py-3 pr-9 text-left transition-colors cursor-pointer",
            isSelected ? "bg-row-on shadow-[inset_2px_0_0_var(--accent)]" : "hover:bg-hover",
          )}
        >
          <div className="flex items-center justify-between gap-2 text-[12px] text-muted">
            <span className="inline-flex min-w-0 items-center gap-1.5">
              <PreparedIcon item={it} />
              <span className="truncate font-medium text-fg/85" title={sender.email || it.sender || undefined}>
                {entityLabel}
              </span>
              {entry.collapsedCount > 1 && (
                <span className="num shrink-0 text-[11px] text-faint">
                  {entry.collapsedCount} messages
                </span>
              )}
            </span>
            <span className="num shrink-0 text-[11.5px] text-faint">{formatDateTime(it.created_at)}</span>
          </div>
          <p className="mt-1 truncate text-[13.5px] font-medium leading-snug text-fg">
            {headline}
          </p>
          <div className="mt-1.5 flex items-center justify-between gap-2 text-[12px]">
            <span className="min-w-0 flex-1 truncate text-muted" title={preview || undefined}>
              {preview || (it.body ? it.body.replace(/\s+/g, " ").trim() : "Open to review")}
            </span>
            <StatusIndicator tone={badge.tone} label={badge.label} className="shrink-0 text-[11.5px]" />
          </div>
        </button>
        <button
          type="button"
          aria-label={`Dismiss ${headline}`}
          title="Dismiss"
          data-testid="assist-quick-dismiss"
          onClick={(e) => {
            e.stopPropagation();
            quickDismissPrepared(it);
          }}
          className="absolute right-2 top-2.5 inline-flex size-6 items-center justify-center rounded-md text-faint opacity-0 transition-opacity hover:bg-surface-2 hover:text-fg focus-visible:opacity-100 group-hover:opacity-100 cursor-pointer"
        >
          <X className="size-3.5" aria-hidden />
        </button>
      </li>
    );
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-bg" data-testid="assist-workbench">
      {/* Compact Superhuman / Linear Top Control Bar */}
      <div className="shrink-0 border-b border-border bg-bg px-4 py-2 lg:px-5" data-testid="assist-topbar">
        <div className="flex flex-wrap items-center gap-2">
          {otherCount > 0 && (
            <div
              role="group"
              aria-label="Filter queue by owner"
              className="liquid-glass-pill inline-flex shrink-0 items-center rounded-lg p-0.5"
            >
              <button
                type="button"
                aria-pressed={scope === "mine"}
                onClick={() => setScope("mine")}
                className={cn(
                  "h-7 rounded-md px-2.5 text-[12px] font-medium transition-all cursor-pointer",
                  scope === "mine" ? "liquid-glass-active" : "text-muted hover:text-fg",
                )}
              >
                Mine
              </button>
              <button
                type="button"
                aria-pressed={scope === "everyone"}
                onClick={() => setScope("everyone")}
                className={cn(
                  "h-7 rounded-md px-2.5 text-[12px] font-medium transition-all cursor-pointer",
                  scope === "everyone" ? "liquid-glass-active" : "text-muted hover:text-fg",
                )}
              >
                Team
              </button>
            </div>
          )}

          <div
            className="liquid-glass-pill flex items-center gap-0.5 overflow-x-auto rounded-lg p-0.5"
            role="group"
            aria-label="Filter queue by status"
          >
            {(
              [
                ["all", "All", filterCounts.all],
                ["ready", "Ready", filterCounts.ready],
                ["input", "Needs input", filterCounts.input],
                ["jobs", "Jobs", filterCounts.jobs],
              ] as const
            ).map(([key, label, count]) => (
              <button
                key={key}
                type="button"
                aria-pressed={filter === key}
                onClick={() => setFilter(key)}
                className={cn(
                  "inline-flex h-7 shrink-0 items-center gap-1 rounded-md px-2.5 text-[12px] font-medium transition-all cursor-pointer",
                  filter === key ? "liquid-glass-active" : "text-muted hover:text-fg",
                )}
              >
                <span>{label}</span>
                {count > 0 && (
                  <span className={cn("num text-[11px]", filter === key ? "opacity-80" : "text-faint")}>
                    {count}
                  </span>
                )}
              </button>
            ))}
          </div>

          <div className="relative min-w-[160px] flex-1 sm:max-w-[260px]">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-faint" aria-hidden />
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search queue..."
              aria-label="Search queue"
              className="h-8 w-full rounded-md border border-border bg-surface pl-8 pr-7 text-[12.5px] text-fg placeholder:text-faint focus:border-border-strong focus:outline-none"
            />
            {search && (
              <button
                type="button"
                aria-label="Clear search"
                onClick={() => setSearch("")}
                className="absolute right-2 top-1/2 -translate-y-1/2 cursor-pointer text-muted hover:text-fg"
              >
                <X className="size-3.5" aria-hidden />
              </button>
            )}
          </div>

          <div className="ml-auto flex items-center gap-2">
            <label className="shrink-0">
              <span className="sr-only">Sort queue</span>
              <select
                aria-label="Sort queue"
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value as QueueSort)}
                data-testid="assist-queue-sort"
                className="h-8 cursor-pointer rounded-md border border-border bg-surface px-2 text-[12px] font-medium text-muted hover:border-border-strong hover:text-fg focus:border-border-strong focus:outline-none"
              >
                <option value="priority">Priority</option>
                <option value="newest">Newest</option>
                <option value="due">Due soonest</option>
              </select>
            </label>

            <button
              type="button"
              aria-expanded={composerOpen}
              data-testid="assist-new-job-toggle"
              onClick={() => setComposerOpen((v) => !v)}
              className={cn(
                "inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md px-3 text-[12.5px] font-medium transition-all cursor-pointer",
                composerOpen
                  ? "border border-border-strong bg-surface-2 text-fg"
                  : "bg-accent text-accent-fg hover:opacity-90 active:scale-[0.98]",
              )}
            >
              {composerOpen ? <X className="size-3.5" aria-hidden /> : <Plus className="size-3.5" aria-hidden />}
              <span>New Assist job</span>
            </button>
          </div>
        </div>
      </div>

      {/* Collapsible single-line Assist job command bar */}
      <div className="shrink-0" data-testid="assist-jobs">
        {composerOpen && (
          <GiveJob
            compact
            autoFocus
            onGiven={handleJobGiven}
          />
        )}
        {jobsError && !jobsForbidden && (
          <p className="border-b border-border px-4 py-1.5 text-[12.5px] text-muted lg:px-5">{jobsError}</p>
        )}
      </div>

      <FeedNote feed="autopilot" className="shrink-0 border-b border-border px-4 py-1.5 text-[12.5px] text-muted lg:px-5" />
      {data && <StaleNote since={data.stale_since} className="shrink-0 px-4 py-1.5 lg:px-5" />}

      {toast && (
        <div
          role="status"
          data-testid="assist-toast"
          className="flex shrink-0 items-center justify-between gap-2 border-b border-border bg-surface px-4 py-2 text-[13px] font-medium text-fg lg:px-5"
        >
          <span className="inline-flex items-center gap-2">
            <CheckCircle2 className="size-3.5 shrink-0 text-ok" aria-hidden />
            {toast}
          </span>
          <button
            type="button"
            aria-label="Dismiss message"
            onClick={() => setToast(null)}
            className="cursor-pointer rounded-md p-1 text-muted hover:bg-surface-2 hover:text-fg"
          >
            <X className="size-3.5" aria-hidden />
          </button>
        </div>
      )}
      {actionError && (
        <div
          role="alert"
          data-testid="assist-action-error"
          className="flex shrink-0 items-center justify-between gap-2 border-b border-border bg-surface px-4 py-2 text-[13px] text-fg lg:px-5"
        >
          <span className="inline-flex items-center gap-2">
            <CircleAlert className="size-3.5 shrink-0 text-late" aria-hidden />
            {actionError}
          </span>
          <button
            type="button"
            aria-label="Dismiss error"
            onClick={() => setActionError(null)}
            className="cursor-pointer rounded-md p-1 text-muted hover:bg-surface-2 hover:text-fg"
          >
            <X className="size-3.5" aria-hidden />
          </button>
        </div>
      )}
      {data?.status === "off" && (
        <p className="shrink-0 border-b border-border bg-surface/50 px-4 py-2 text-[13px] text-muted lg:px-5" data-testid="assist-off">
          {data.reason ?? "Assist is not switched on for you. Turn it on under Switches."}
        </p>
      )}

      {error && rawEntries.length === 0 ? (
        <div className="p-5 lg:p-8">
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        </div>
      ) : loadingDrafts && rawEntries.length === 0 ? (
        <div className="p-5 lg:p-8">
          <Pending page label="Reading what is prepared for you" />
        </div>
      ) : rawEntries.length === 0 && otherCount === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center px-6 py-16 text-center" data-testid="prepared-empty">
          <Bot className="mx-auto mb-3 size-6 text-muted" aria-hidden />
          <p className="text-[15px] font-medium text-fg">Nothing is prepared for you right now.</p>
          <p className="mt-1.5 text-[13.5px] text-muted">
            Start a new Assist job above, or check your settings under Switches.
          </p>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col lg:flex-row lg:overflow-hidden">
          {/* Left queue */}
          <aside
            className="flex min-h-0 flex-col border-b border-border bg-list lg:w-[380px] lg:shrink-0 lg:border-r lg:border-b-0 xl:w-[420px]"
            aria-label="Assist queue"
            data-testid="assist-queue"
          >
            {rawEntries.length === 0 ? (
              <div className="px-4 py-10 text-center" data-testid="mine-empty">
                <p className="text-[13.5px] font-medium text-fg">Nothing on your own list right now.</p>
                <button
                  type="button"
                  onClick={() => setScope("everyone")}
                  className="mt-2 cursor-pointer text-[13px] font-medium text-accent hover:underline"
                >
                  Show team items ({otherCount})
                </button>
              </div>
            ) : orderedEntries.length === 0 ? (
              <div className="px-4 py-10 text-center" data-testid="filter-empty">
                <p className="text-[13.5px] font-medium text-fg">No matching items.</p>
                <button
                  type="button"
                  onClick={() => {
                    setFilter("all");
                    setSearch("");
                  }}
                  className="mt-2 cursor-pointer text-[13px] font-medium text-accent hover:underline"
                >
                  Clear filters
                </button>
              </div>
            ) : (
              <div className="min-h-0 flex-1 overflow-y-auto">
                {sortBy === "priority" ? (
                  <div>
                    {grouped.ready.length > 0 && (
                      <section aria-label="Ready to act" data-testid="queue-section-ready">
                        <div className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-bg-subtle/95 px-3.5 py-2 backdrop-blur-sm">
                          <span className="text-[12px] font-medium text-muted">Ready to act</span>
                          <span className="num text-[12px] text-muted">{grouped.ready.length}</span>
                        </div>
                        <ul className="divide-y divide-border">{grouped.ready.map(renderRow)}</ul>
                      </section>
                    )}

                    {grouped.input.length > 0 && (
                      <section aria-label="Needs input" data-testid="queue-section-input">
                        <div className="sticky top-0 z-10 flex items-center justify-between border-y border-border bg-bg-subtle/95 px-3.5 py-2 backdrop-blur-sm first:border-t-0">
                          <span className="text-[12px] font-medium text-muted">Needs input</span>
                          <span className="num text-[12px] text-muted">{grouped.input.length}</span>
                        </div>
                        <ul className="divide-y divide-border">{grouped.input.map(renderRow)}</ul>
                      </section>
                    )}

                    {grouped.waiting.length > 0 && (
                      <section aria-label="Waiting" data-testid="queue-section-waiting">
                        <button
                          type="button"
                          aria-expanded={waitingExpanded}
                          onClick={() => setWaitingUserToggle(!waitingExpanded)}
                          className="sticky top-0 z-10 flex w-full items-center justify-between border-y border-border bg-bg-subtle/95 px-3.5 py-2 text-left transition-colors hover:bg-hover backdrop-blur-sm cursor-pointer"
                        >
                          <span className="inline-flex items-center gap-1.5 text-[12px] font-medium text-muted">
                            {waitingExpanded ? (
                              <ChevronDown className="size-3.5" aria-hidden />
                            ) : (
                              <ChevronRight className="size-3.5" aria-hidden />
                            )}
                            <span>Waiting</span>
                          </span>
                          <span className="num text-[12px] text-muted">{grouped.waiting.length}</span>
                        </button>
                        {waitingExpanded && (
                          <ul className="divide-y divide-border">{grouped.waiting.map(renderRow)}</ul>
                        )}
                      </section>
                    )}
                  </div>
                ) : (
                  <ul className="divide-y divide-border">{orderedEntries.map(renderRow)}</ul>
                )}

                {loadingDrafts && (
                  <div className="border-t border-border px-3.5 py-3.5" data-testid="assist-queue-loading" role="status">
                    <p className="text-[12.5px] text-muted">Loading prepared items...</p>
                    <div className="mt-2.5 space-y-2.5" aria-hidden>
                      <div className="skeleton h-3.5 w-3/4" />
                      <div className="skeleton h-3.5 w-2/3" />
                      <div className="skeleton h-3.5 w-1/2" />
                    </div>
                  </div>
                )}

                {(data?.older ?? 0) > 0 && !all && (
                  <div className="border-t border-border px-3.5 py-3">
                    <button
                      type="button"
                      onClick={() => setAll(true)}
                      className="w-full cursor-pointer rounded-lg py-1.5 text-center text-[12.5px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg"
                      data-testid="assist-older"
                    >
                      Show older items
                    </button>
                  </div>
                )}
              </div>
            )}
          </aside>

          {/* Right reading workspace — open canvas, zero nested cards */}
          <section
            className="min-h-0 min-w-0 flex-1 overflow-y-auto bg-bg px-6 py-6 lg:px-10 lg:py-8"
            aria-label="Selected Assist item"
            data-testid="assist-detail"
          >
            <div className="max-w-[820px]">
              {active?.kind === "job" ? (
                <JobCard key={active.job.job_id} job={active.job} onJob={replaceJob} onFeedback={showToast} />
              ) : active?.kind === "prepared" ? (
                <ul aria-label="Prepared for me">
                  {(() => {
                    const i = active.item;
                    const fb = <PreparedFeedback item={i} />;
                    if (i.kind === "job") {
                      return (
                        <PreparedJobCard
                          key={i.item_id}
                          item={i}
                          linked={i.item_id === linked}
                          detail
                          onGone={onGone}
                          onOptimistic={onOptimistic}
                          onRestore={onRestore}
                          onFeedback={showToast}
                          feedback={fb}
                        />
                      );
                    }
                    if (i.kind === "todo") {
                      return (
                        <TodoCard
                          key={i.item_id}
                          item={i}
                          linked={i.item_id === linked}
                          detail
                          onGone={onGone}
                          onChanged={retry}
                          onOptimistic={onOptimistic}
                          onRestore={onRestore}
                          onFeedback={showToast}
                          feedback={fb}
                        />
                      );
                    }
                    if (i.kind === "alert") {
                      return (
                        <AlertCard
                          key={i.item_id}
                          item={i}
                          linked={i.item_id === linked}
                          detail
                          onGone={onGone}
                          onChanged={retry}
                          onOptimistic={onOptimistic}
                          onRestore={onRestore}
                          onFeedback={showToast}
                          feedback={fb}
                        />
                      );
                    }
                    return (
                      <PreparedCard
                        key={i.item_id}
                        item={i}
                        linked={i.item_id === linked}
                        detail
                        onGone={onGone}
                        onChanged={retry}
                        onOptimistic={onOptimistic}
                        onRestore={onRestore}
                        onFeedback={showToast}
                        feedback={fb}
                      />
                    );
                  })()}
                </ul>
              ) : null}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
