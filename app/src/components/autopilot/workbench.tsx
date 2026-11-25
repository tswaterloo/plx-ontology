"use client";

import { Bot, CheckCircle2, CheckSquare, CircleAlert, ClipboardList, FileText, Mail, MessageSquare, X } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertCard } from "@/components/autopilot/alert";
import { JobCard as PreparedJobCard } from "@/components/autopilot/job";
import { badgeClass, GiveJob, jobBadgeOf, JobCard, useAssistJobs, type JobBadgeInfo } from "@/components/autopilot/jobs";
import { noteParts } from "@/components/autopilot/lines";
import { PreparedCard, senderLabel } from "@/components/autopilot/prepared";
import { PreparedFeedback } from "@/components/autopilot/prepared-feedback";
import { TodoCard } from "@/components/autopilot/todo";
import { FeedNote } from "@/components/feed-note";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { getAutopilotDrafts, type AssistJob, type DraftList, type PreparedItem } from "@/lib/api";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { useStaleRefresh } from "@/lib/stale-refresh";
import { useRead } from "@/lib/use-read";

type QueueEntry =
  | { key: string; kind: "job"; job: AssistJob; sortAt: string }
  | { key: string; kind: "prepared"; item: PreparedItem; sortAt: string };

function preparedBadgeOf(item: PreparedItem): JobBadgeInfo {
  if (item.plan_status === "working") return { label: "Working", tone: "new" };
  if (item.plan_status === "failed") return { label: "Needs context", tone: "risk" };
  if (item.body && item.body.trim() !== "") return { label: "Draft ready", tone: "new" };
  if (item.kind === "job" && (item.jobs ?? []).length > 0) return { label: "Ready to review", tone: "new" };
  if (item.kind === "alert") return { label: "Alert", tone: "risk" };
  return { label: "Waiting", tone: "neutral" };
}

function preparedTitleOf(item: PreparedItem): string {
  const sender = senderLabel(item.sender);
  if (item.subject && sender) return `${sender} · ${item.subject}`;
  return item.subject || item.summary || sender || "Prepared item";
}

function preparedPreviewOf(item: PreparedItem, title: string): string {
  const norm = (s: string) => s.trim().toLowerCase().replace(/\s+/g, " ");
  const t = norm(title);
  const subj = norm(item.subject ?? "");
  const candidates = [
    item.summary ?? "",
    item.message ?? "",
    noteParts(item.note ?? "").text,
  ];
  for (const raw of candidates) {
    const clean = raw.replace(/\s+/g, " ").trim();
    if (!clean) continue;
    const c = norm(clean);
    if (c === t || c === subj) continue;
    return clean;
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

function dedupePrepared(items: PreparedItem[], keepId?: string | null): PreparedItem[] {
  const seen = new Set<string>();
  const out: PreparedItem[] = [];
  for (const item of items) {
    if (keepId && item.item_id === keepId) {
      out.push(item);
      continue;
    }
    const who = senderLabel(item.sender).toLowerCase();
    const subj = (item.subject || "").trim().toLowerCase().replace(/\s+/g, " ");
    const summ = (item.summary || "").trim().toLowerCase().replace(/\s+/g, " ");
    const when = (item.created_at || "").slice(0, 16);
    const sig = `${item.kind}|${who}|${subj}|${summ}|${when}`;
    if ((subj || summ) && seen.has(sig)) continue;
    if (subj || summ) seen.add(sig);
    out.push(item);
  }
  return out;
}

/**
 * Full-bleed Linear-style 2-pane Assist Workbench:
 * - Compact flat top "Give Assist a job" command bar.
 * - Left hairline-divided queue (340-380px on desktop) with inline loading indicator while drafts arrive.
 * - Right clean reading detail workspace separated by a single 1px border-border hairline.
 */
export function AssistWorkbench({ linked }: { linked: string | null }) {
  const [all, setAll] = useState(false);
  const [scope, setScope] = useState<"mine" | "everyone">("mine");
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

  const allPreparedItems = useMemo(
    () => dedupePrepared((data?.items ?? []).filter((i) => !gone.has(i.item_id)), linked),
    [data?.items, gone, linked],
  );

  // If a deep-linked item lives on another person's list or in older items, reveal it automatically.
  useEffect(() => {
    if (!linked || !data) return;
    const match = allPreparedItems.find((i) => i.item_id === linked);
    if (match && isOtherPersonItem(match) && scope === "mine") {
      setScope("everyone");
    } else if (!match && !all && (data.older ?? 0) > 0) {
      setAll(true);
    }
  }, [linked, data, allPreparedItems, scope, all]);

  const otherCount = useMemo(
    () => allPreparedItems.filter(isOtherPersonItem).length,
    [allPreparedItems],
  );

  const mineCount = allPreparedItems.length - otherCount;

  const preparedItems = useMemo(() => {
    if (scope === "everyone" || otherCount === 0) return allPreparedItems;
    return allPreparedItems.filter((i) => !isOtherPersonItem(i));
  }, [allPreparedItems, otherCount, scope]);

  const entries = useMemo<QueueEntry[]>(() => {
    const list: QueueEntry[] = [
      ...(jobs ?? []).map((job): QueueEntry => ({
        key: `job:${job.job_id}`,
        kind: "job",
        job,
        sortAt: job.requested_at ?? "",
      })),
      ...preparedItems.map((item): QueueEntry => ({
        key: `item:${item.item_id}`,
        kind: "prepared",
        item,
        sortAt: item.created_at ?? "",
      })),
    ];
    return list;
  }, [jobs, preparedItems]);

  const loadingDrafts = !error && !data;
  const [selectedKey, setSelectedKey] = useState<string | null>(() => (linked ? `item:${linked}` : null));

  useEffect(() => {
    if (linked) {
      const target = `item:${linked}`;
      if (entries.some((e) => e.key === target)) {
        setSelectedKey(target);
        return;
      }
      if (loadingDrafts) return;
    }
    if (entries.length === 0) {
      setSelectedKey(null);
      return;
    }
    if (!selectedKey || !entries.some((e) => e.key === selectedKey)) {
      setSelectedKey(entries[0].key);
    }
  }, [entries, linked, selectedKey, loadingDrafts]);

  const active = useMemo(
    () => entries.find((e) => e.key === selectedKey) ?? entries[0] ?? null,
    [entries, selectedKey],
  );

  const handleJobGiven = useCallback(
    (job: AssistJob) => {
      replaceJob(job);
      setSelectedKey(`job:${job.job_id}`);
      showToast("Job started");
    },
    [replaceJob, showToast],
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-bg" data-testid="assist-workbench">
      <div className="shrink-0" data-testid="assist-jobs">
        <GiveJob
          onGiven={handleJobGiven}
          switchesLink={
            <Link
              href="/assist?show=switches"
              className="text-[12px] text-muted underline-offset-2 hover:text-fg hover:underline"
            >
              Switches
            </Link>
          }
        />
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
          className="flex shrink-0 items-center justify-between gap-2 border-b border-border border-l-2 border-l-ok/75 bg-surface/60 px-4 py-2 text-[12.5px] font-medium text-fg lg:px-5"
        >
          <span className="inline-flex items-center gap-2">
            <CheckCircle2 className="size-3.5 shrink-0 text-ok" aria-hidden />
            {toast}
          </span>
          <button
            type="button"
            aria-label="Dismiss message"
            onClick={() => setToast(null)}
            className="cursor-pointer rounded-sm p-0.5 text-muted hover:text-fg"
          >
            <X className="size-3.5" aria-hidden />
          </button>
        </div>
      )}
      {actionError && (
        <div
          role="alert"
          data-testid="assist-action-error"
          className="flex shrink-0 items-center justify-between gap-2 border-b border-border border-l-2 border-l-late/75 bg-surface/60 px-4 py-2 text-[12.5px] text-fg lg:px-5"
        >
          <span className="inline-flex items-center gap-2">
            <CircleAlert className="size-3.5 shrink-0 text-late" aria-hidden />
            {actionError}
          </span>
          <button
            type="button"
            aria-label="Dismiss error"
            onClick={() => setActionError(null)}
            className="cursor-pointer rounded-sm p-0.5 text-muted hover:text-fg"
          >
            <X className="size-3.5" aria-hidden />
          </button>
        </div>
      )}
      {data?.status === "off" && (
        <p className="shrink-0 border-b border-border bg-surface/40 px-4 py-2 text-[12.5px] text-muted lg:px-5" data-testid="assist-off">
          {data.reason ?? "Assist is not switched on for you. Turn it on under Switches."}
        </p>
      )}

      {error && entries.length === 0 ? (
        <div className="p-4 lg:p-6">
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        </div>
      ) : loadingDrafts && entries.length === 0 ? (
        <div className="p-4 lg:p-6">
          <Pending page label="Reading what is prepared for you" />
        </div>
      ) : entries.length === 0 && otherCount === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center px-6 py-16 text-center" data-testid="prepared-empty">
          <Bot className="mx-auto mb-2.5 size-6 text-faint" aria-hidden />
          <p className="text-[14px] font-medium text-fg">Nothing is prepared for you right now.</p>
          <p className="mt-1 text-[13px] text-muted">
            Give Assist a job above, or check your settings under Switches.
          </p>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col lg:flex-row lg:overflow-hidden">
          {/* Left queue */}
          <aside
            className="flex min-h-0 flex-col border-b border-border bg-bg-subtle lg:w-[340px] lg:shrink-0 lg:border-r lg:border-b-0 xl:w-[380px]"
            aria-label="Assist queue"
            data-testid="assist-queue"
          >
            <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border px-3.5 py-2">
              {otherCount > 0 ? (
                <div role="group" aria-label="Filter queue by owner" className="inline-flex items-center gap-1">
                  <button
                    type="button"
                    aria-pressed={scope === "mine"}
                    onClick={() => setScope("mine")}
                    className={cn(
                      "inline-flex h-6 items-center gap-1.5 rounded-sm px-2 text-[12px] transition-colors cursor-pointer",
                      scope === "mine" ? "bg-surface-2 font-medium text-fg" : "text-muted hover:bg-hover hover:text-fg",
                    )}
                  >
                    <span>Mine</span>
                    <span className="num text-[11px] text-faint">{(jobs ?? []).length + mineCount}</span>
                  </button>
                  <button
                    type="button"
                    aria-pressed={scope === "everyone"}
                    onClick={() => setScope("everyone")}
                    className={cn(
                      "inline-flex h-6 items-center gap-1.5 rounded-sm px-2 text-[12px] transition-colors cursor-pointer",
                      scope === "everyone" ? "bg-surface-2 font-medium text-fg" : "text-muted hover:bg-hover hover:text-fg",
                    )}
                  >
                    <span>Everyone</span>
                    <span className="num text-[11px] text-faint">{(jobs ?? []).length + allPreparedItems.length}</span>
                  </button>
                </div>
              ) : (
                <h2 className="text-[11.5px] font-medium uppercase tracking-[0.04em] text-faint">
                  Queue ({entries.length})
                </h2>
              )}
              {(data?.older ?? 0) > 0 && !all && (
                <button
                  type="button"
                  onClick={() => setAll(true)}
                  className="cursor-pointer text-[12px] font-medium text-muted hover:text-fg hover:underline"
                  data-testid="assist-older"
                >
                  Show {data?.older} older
                </button>
              )}
            </div>
            {entries.length === 0 ? (
              <div className="px-4 py-8 text-center" data-testid="mine-empty">
                <p className="text-[13px] font-medium text-fg">Nothing on your own list right now.</p>
                <button
                  type="button"
                  onClick={() => setScope("everyone")}
                  className="mt-2 cursor-pointer text-[12.5px] font-medium text-accent hover:underline"
                >
                  Show everyone&apos;s items ({otherCount})
                </button>
              </div>
            ) : (
              <ul className="min-h-0 flex-1 divide-y divide-border overflow-y-auto">
                {entries.map((entry) => {
                  const isSelected = active?.key === entry.key;
                  if (entry.kind === "job") {
                    const j = entry.job;
                    const badge = jobBadgeOf(j);
                    const countLabel =
                      j.planned > 0
                        ? `${j.pieces.length} of ${j.planned} ${j.planned === 1 ? "piece" : "pieces"}`
                        : j.checklist.length > 0
                          ? `${j.checklist.length} checklist ${j.checklist.length === 1 ? "item" : "items"}`
                          : "";
                    return (
                      <li key={entry.key}>
                        <button
                          type="button"
                          onClick={() => setSelectedKey(entry.key)}
                          aria-current={isSelected ? "true" : undefined}
                          data-testid="assist-queue-item"
                          className={cn(
                            "w-full px-3.5 py-2.5 text-left transition-colors cursor-pointer",
                            isSelected
                              ? "bg-row-on shadow-[inset_2px_0_0_var(--accent)]"
                              : "hover:bg-hover",
                          )}
                        >
                          <div className="flex items-start gap-2">
                            <ClipboardList className={cn("mt-0.5 size-3.5 shrink-0", isSelected ? "text-fg" : "text-muted")} aria-hidden />
                            <span className="min-w-0 flex-1 text-[13.5px] font-medium leading-5 text-fg line-clamp-2">
                              {j.text}
                            </span>
                          </div>
                          <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 pl-[22px] text-[11.5px]">
                            <span className={cn("font-medium", badgeClass(badge.tone))}>
                              {badge.label}
                            </span>
                            {countLabel && (
                              <>
                                <span className="text-faint" aria-hidden>·</span>
                                <span className="num text-muted">{countLabel}</span>
                              </>
                            )}
                            <span className="num ml-auto text-faint">{formatDateTime(j.requested_at)}</span>
                          </div>
                        </button>
                      </li>
                    );
                  }

                  const it = entry.item;
                  const badge = preparedBadgeOf(it);
                  const title = preparedTitleOf(it);
                  const preview = preparedPreviewOf(it, title);
                  return (
                    <li key={entry.key}>
                      <button
                        type="button"
                        onClick={() => setSelectedKey(entry.key)}
                        aria-current={isSelected ? "true" : undefined}
                        data-testid="assist-queue-item"
                        className={cn(
                          "w-full px-3.5 py-2.5 text-left transition-colors cursor-pointer",
                          isSelected
                            ? "bg-surface-2/70 shadow-[inset_2px_0_0_var(--accent)]"
                            : "hover:bg-hover",
                        )}
                      >
                        <div className="flex items-start gap-2">
                          <span className="mt-0.5">
                            <PreparedIcon item={it} />
                          </span>
                          <span className="min-w-0 flex-1 text-[13.5px] font-medium leading-5 text-fg line-clamp-2">
                            {title}
                          </span>
                        </div>
                        {preview && (
                          <p className="mt-0.5 truncate pl-[22px] text-[12px] leading-4 text-muted" title={preview}>
                            {preview}
                          </p>
                        )}
                        <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 pl-[22px] text-[11.5px]">
                          <span className={cn("font-medium", badgeClass(badge.tone))}>
                            {badge.label}
                          </span>
                          <span className="num ml-auto text-faint">{formatDateTime(it.created_at)}</span>
                        </div>
                      </button>
                    </li>
                  );
                })}
                {loadingDrafts && (
                  <li className="px-3.5 py-3" data-testid="assist-queue-loading" role="status">
                    <p className="text-[12px] text-muted">Loading prepared items...</p>
                    <div className="mt-2.5 space-y-2.5" aria-hidden>
                      <div className="skeleton h-3.5 w-3/4" />
                      <div className="skeleton h-3.5 w-2/3" />
                      <div className="skeleton h-3.5 w-1/2" />
                    </div>
                  </li>
                )}
              </ul>
            )}
          </aside>

          {/* Right detail workspace */}
          <section className="min-h-0 min-w-0 flex-1 overflow-y-auto bg-bg px-5 py-5 lg:px-8" aria-label="Selected Assist item" data-testid="assist-detail">
            <div className="max-w-[760px]">
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
