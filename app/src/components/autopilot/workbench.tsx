"use client";

import {
  ArrowLeft,
  ArrowUp,
  Bot,
  Check,
  CheckCircle2,
  CheckSquare,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  ClipboardList,
  Clock3,
  Copy,
  FileText,
  Mail,
  MessageSquare,
  Pencil,
  Search,
  Send,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { canonicalObjectHref, extractQcFacts, objectIdFromHref } from "@/components/autopilot/alert";
import { DraftUse, draftUseOf } from "@/components/autopilot/draft-use";
import { MoreMenu, TASK_NOTE, taskText, tomorrowMorning, type MenuEntry, type OptimisticCallbacks } from "@/components/autopilot/item-actions";
import { JobCard as PreparedJobCard } from "@/components/autopilot/job";
import { jobBadgeOf, JobCard, StatusIndicator, useAssistJobs, type JobBadgeInfo } from "@/components/autopilot/jobs";
import { cleanHeadline, formatSender, noteParts, saidOnce, threadTopicKey } from "@/components/autopilot/lines";
import { formatElapsed, mailtoOf } from "@/components/autopilot/prepared";
import { PreparedFeedback } from "@/components/autopilot/prepared-feedback";
import { TodoSteps } from "@/components/autopilot/todo";
import { FeedNote } from "@/components/feed-note";
import { Button } from "@/components/ui/button";
import { StyledSelect } from "@/components/ui/controls";
import { LoadProblem, StaleNote, useProblem, useProblemSlot } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import {
  alwaysDoThis,
  decideAutopilotItem,
  getAutopilotColleagues,
  getAutopilotDrafts,
  handAutopilotItem,
  replyFromLine,
  requestAssistJob,
  requestDraft,
  saveMemory,
  setFollowing,
  setNeverSender,
  takeAutopilotItem,
  type AssistJob,
  type DraftList,
  type PreparedItem,
} from "@/lib/api";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { countedWords, formatDateTime } from "@/lib/format";
import { useStaleRefresh } from "@/lib/stale-refresh";
import { useRead } from "@/lib/use-read";

type QueueBucket = "ready" | "input" | "waiting";
type QueueFilter = "all" | "ready" | "input" | "jobs" | "mc";
type QueueSort = "priority" | "newest" | "due";

const QUICK_JOB_SUGGESTIONS = [
  "Chase late POs with no confirmed delivery date",
  "Summarize open QC holds and affected customer orders",
  "Check shipped orders that are not yet invoiced",
  "Draft follow-ups for quiet supplier threads",
];

function isAutomatedMcApproval(item: PreparedItem): boolean {
  return item.kind === "alert" && item.alert_kind === "mc_approval_pending";
}

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

function channelLabelOf(item: PreparedItem): string {
  if (item.kind === "alert") return item.alert_kind === "qc_hold" ? "QC hold" : "System alert";
  if (item.channel === "teams") return "Teams";
  if (item.channel === "mail") return "Email";
  if (item.kind === "todo") return "Action item";
  if (item.kind === "job") return "Proposed change";
  return "Assist";
}

function PreparedIcon({ item, active }: { item: PreparedItem; active?: boolean }) {
  const cls = cn("size-3.5 shrink-0", active ? "text-fg" : "text-muted");
  if (item.kind === "alert") return <CircleAlert className={cn("size-3.5 shrink-0", active ? "text-late" : "text-late/80")} aria-hidden />;
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
  const ts = Date.parse(item.created_at ?? "");
  const base = Number.isNaN(ts) ? Date.now() : ts;
  if (item.kind === "alert") return base - 7 * 86_400_000;
  if (item.kind === "your_call" || /^You said:/i.test((item.summary ?? "").trim())) return base - 3 * 86_400_000;
  return base;
}

function startedAtMs(requestedAt?: string | null): number {
  if (requestedAt) {
    const parsed = Date.parse(requestedAt);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return Date.now();
}

const sameWords = (a: string, b: string) =>
  a.replace(/\s+/g, " ").trim().toLowerCase() === b.replace(/\s+/g, " ").trim().toLowerCase();

/** Sleek single-row h-11 inline command strip with focus-only quick-try popover. */
function InlineCommandStrip({ onGiven }: { onGiven: (job: AssistJob) => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [popoverOpen, setPopoverOpen] = useState(false);
  const [, setError, problem] = useProblem();
  const boxRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!popoverOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) {
        setPopoverOpen(false);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [popoverOpen]);

  const submit = async () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    setError(null);
    setPopoverOpen(false);
    try {
      const job = await requestAssistJob(text.trim());
      onGiven(job);
      setText("");
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      ref={boxRef}
      className="relative shrink-0 border-b border-border bg-bg px-4 lg:px-5"
      data-testid="give-job"
    >
      <div className="flex h-11 items-center gap-2.5" data-testid="assist-command-bar">
        <Sparkles className="size-3.5 shrink-0 text-faint" aria-hidden />
        <label htmlFor="assist-command-input" className="sr-only">
          Give Assist a job
        </label>
        <input
          ref={inputRef}
          id="assist-command-input"
          type="text"
          value={text}
          disabled={busy}
          onFocus={() => {
            if (!text.trim()) setPopoverOpen(true);
          }}
          onChange={(e) => {
            setText(e.target.value);
            if (e.target.value.trim()) setPopoverOpen(false);
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              setPopoverOpen(false);
              inputRef.current?.blur();
              return;
            }
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void submit();
            }
          }}
          placeholder="Give Assist a job, e.g. chase late POs or summarize QC holds..."
          className="h-8 min-w-0 flex-1 bg-transparent text-[13.5px] text-fg placeholder:text-faint focus:outline-none"
        />
        <span className="hidden text-[11.5px] text-faint md:inline">Nothing sends without confirmation</span>
        <button
          type="button"
          disabled={busy || !text.trim()}
          onClick={() => void submit()}
          data-testid="assist-command-run"
          className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md bg-accent px-3 text-[12.5px] font-medium text-accent-fg transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40 cursor-pointer"
        >
          <span>{busy ? "Running..." : "Run"}</span>
          {!busy && <ArrowUp className="size-3" aria-hidden />}
        </button>
      </div>

      {popoverOpen && !text.trim() && (
        <div
          role="region"
          aria-label="Quick-try prompts"
          data-testid="give-job-examples"
          className="absolute left-4 right-4 top-full z-30 mt-1 max-w-2xl rounded-xl border border-border-strong bg-raised p-1.5 shadow-pop lg:left-5"
        >
          <p className="px-2.5 py-1 text-[11.5px] font-medium text-faint">Quick try</p>
          <ul className="space-y-0.5">
            {QUICK_JOB_SUGGESTIONS.map((example) => (
              <li key={example}>
                <button
                  type="button"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => {
                    setText(example);
                    setPopoverOpen(false);
                    inputRef.current?.focus();
                  }}
                  className="flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 text-left text-[13px] text-fg transition-colors hover:bg-hover cursor-pointer"
                >
                  <span className="truncate">{example}</span>
                  <ArrowUp className="size-3 shrink-0 text-faint" aria-hidden />
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {problem && <div className="pb-2">{problem}</div>}
    </div>
  );
}

function FollowRecordButton({ objectId, label }: { objectId: string; label: string }) {
  const [followed, setFollowed] = useState(false);
  const [busy, setBusy] = useState(false);
  const toggle = async () => {
    setBusy(true);
    try {
      await setFollowing(objectId, !followed);
      setFollowed((v) => !v);
    } catch {
      // Quiet fallback; user can retry
    } finally {
      setBusy(false);
    }
  };
  return (
    <button
      type="button"
      disabled={busy}
      onClick={() => void toggle()}
      aria-label={`${followed ? "Unfollow" : "Follow"} ${label}`}
      className="h-7 rounded-md px-2 text-[12px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg disabled:opacity-40 cursor-pointer"
    >
      {followed ? "Following" : "Follow"}
    </button>
  );
}

/** Right Action Canvas for PreparedItem (reply, your_call, todo, alert):
 *  - Crisp top header with title, metadata strip, and Snooze / Dismiss / More controls.
 *  - Structured brief (2-col QcFactGrid for QC holds, clean summary + checklist for todos).
 *  - Elevated Interactive Draft Composer Surface with hairline To:/Subject: rows and clear bottom bar. */
function InboxPreparedCanvas({
  item,
  onGone,
  onChanged,
  onOptimistic,
  onRestore,
  onFeedback,
  feedback,
}: {
  item: PreparedItem;
  onGone: (id: string) => void;
  onChanged: () => void;
  feedback?: React.ReactNode;
} & OptimisticCallbacks) {
  const [text, setText] = useState(item.body);
  const [editing, setEditing] = useState(false);
  const [line, setLine] = useState("");
  const [pref, setPref] = useState("");
  const [panel, setPanel] = useState<"rewrite" | "pref" | "pass" | null>(null);
  const [colleagues, setColleagues] = useState<{ key: string; name: string }[]>([]);
  const [passTo, setPassTo] = useState("");
  const [token, setToken] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const initiallyWorking = item.plan_status === "working";
  const [waitingSince, setWaitingSince] = useState<number | null>(() => (initiallyWorking ? startedAtMs(item.requested_at) : null));
  const [sawWorking, setSawWorking] = useState<boolean>(initiallyWorking);
  const [baseline, setBaseline] = useState<{ body: string; planAt: string | null } | null>(null);
  const [elapsedSec, setElapsedSec] = useState<number>(() =>
    initiallyWorking ? Math.max(0, Math.floor((Date.now() - startedAtMs(item.requested_at)) / 1000)) : 0,
  );
  const card = useProblemSlot();
  const setError = card.set;
  const waiting = waitingSince !== null;

  useEffect(() => {
    if (panel !== "pass" || colleagues.length > 0) return;
    getAutopilotColleagues()
      .then((res) => {
        const list = res.people ?? [];
        setColleagues(list);
        if (list[0]) setPassTo(list[0].key);
      })
      .catch(() => undefined);
  }, [panel, colleagues.length]);

  useEffect(() => {
    if (waitingSince === null) return;
    setElapsedSec(Math.max(0, Math.floor((Date.now() - waitingSince) / 1000)));
    const tick = setInterval(() => setElapsedSec(Math.max(0, Math.floor((Date.now() - waitingSince) / 1000))), 1000);
    const poll = setInterval(onChanged, 4000);
    return () => {
      clearInterval(tick);
      clearInterval(poll);
    };
  }, [waitingSince, onChanged]);

  useEffect(() => {
    if (item.plan_status === "working") {
      setSawWorking(true);
      if (waitingSince === null) setWaitingSince(startedAtMs(item.requested_at));
      return;
    }
    const baseBody = baseline?.body ?? text;
    if (item.body && item.body !== baseBody) {
      setText(item.body);
      setWaitingSince(null);
      setSawWorking(false);
      setBaseline(null);
      setNote(null);
      return;
    }
    const planMoved = baseline !== null && Boolean(item.plan_at) && item.plan_at !== baseline.planAt;
    if (waitingSince !== null && (sawWorking || planMoved)) {
      setWaitingSince(null);
      setSawWorking(false);
      setBaseline(null);
      if (item.plan_status === "failed" || item.plan_reason) {
        const why = item.plan_reason ? ` (${item.plan_reason.split(":", 1)[0]})` : "";
        setNote(`Could not write the reply${why}. Try again.`);
      } else {
        setNote("Finished without a new draft. Try a shorter prompt.");
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item.body, item.plan_status, item.plan_at, item.plan_reason, item.requested_at]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };

  const optimisticDecide = (body: Parameters<typeof decideAutopilotItem>[1], toastMsg: string) => {
    if (onOptimistic) {
      onOptimistic(item.item_id, toastMsg);
      void decideAutopilotItem(item.item_id, body).catch((e) => {
        onRestore?.(item.item_id, plainError(e));
      });
      return;
    }
    void run(async () => {
      await decideAutopilotItem(item.item_id, body);
      onGone(item.item_id);
    });
  };

  const snooze = () => optimisticDecide({ decision: "snooze", until: tomorrowMorning().toISOString() }, "Snoozed until tomorrow 8 AM");
  const dismiss = () => optimisticDecide({ decision: "reject" }, "Dismissed");
  const markDone = () => optimisticDecide({ decision: "accept" }, "Marked done");

  const makeTask = () => {
    const t = taskText(item);
    if (onOptimistic) {
      onOptimistic(item.item_id, "Added to your tasks");
      void saveMemory({ kind: "commitment", text: t })
        .then(() => decideAutopilotItem(item.item_id, { decision: "reject", note: TASK_NOTE }))
        .catch((e) => {
          onRestore?.(item.item_id, plainError(e));
        });
      return;
    }
    void run(async () => {
      await saveMemory({ kind: "commitment", text: t });
      await decideAutopilotItem(item.item_id, { decision: "reject", note: TASK_NOTE });
      onGone(item.item_id);
    });
  };

  const approveAndSend = () =>
    run(async () => {
      const out = await decideAutopilotItem(item.item_id, {
        decision: "accept",
        ...(text !== item.body ? { text } : {}),
        ...(token ? { confirm_token: token } : {}),
      });
      if (out.confirm_token) {
        setToken(out.confirm_token);
        return;
      }
      if (out.decision?.sent) {
        if (onOptimistic) onOptimistic(item.item_id, "Sent");
        else onGone(item.item_id);
      } else if (out.state === "waiting") {
        setNote("Not sent: the send failed. Confirm again to retry.");
      } else {
        setNote("Confirmed. Sending is not switched on for this mailbox yet.");
        onFeedback?.("Confirmed");
      }
    });

  const copyReply = () =>
    run(async () => {
      await navigator.clipboard?.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
      onFeedback?.("Copied draft to clipboard");
    });

  const writeFromLine = () =>
    run(async () => {
      const out = await replyFromLine(item.item_id, line);
      const written = out.decision?.text ?? "";
      setPanel(null);
      setLine("");
      if (written) {
        setText(written);
        setEditing(true);
        setWaitingSince(null);
        setSawWorking(false);
        setBaseline(null);
      } else {
        setBaseline({ body: item.body, planAt: item.plan_at ?? null });
        setSawWorking(item.plan_status === "working");
        setWaitingSince(Date.now());
        setElapsedSec(0);
        setNote(null);
      }
      onChanged();
    });

  const askForDraft = () =>
    run(async () => {
      setBaseline({ body: item.body, planAt: item.plan_at ?? null });
      setSawWorking(item.plan_status === "working");
      setWaitingSince(Date.now());
      setElapsedSec(0);
      await requestDraft(item.item_id);
      onChanged();
    });

  const savePref = () =>
    run(async () => {
      await saveMemory({ kind: "preference", text: pref.trim() });
      setPref("");
      setPanel(null);
      setNote("Saved to Memory. Future drafts will follow it.");
      onFeedback?.("Saved as your preference");
    });

  const takePooled = () =>
    run(async () => {
      await takeAutopilotItem(item.item_id);
      onFeedback?.("Added to your list");
      onChanged();
    });

  const passToColleague = () =>
    run(async () => {
      if (!passTo) return;
      await handAutopilotItem(item.item_id, passTo);
      setPanel(null);
      onFeedback?.("Passed to colleague");
      onChanged();
    });

  const haveReply = text.trim() !== "";
  const senderInfo = formatSender(item.sender);
  const parsedNote = noteParts(item.note ?? "");
  const badge = preparedBadgeOf(item);
  const headline = preparedHeadlineOf(item);
  const sourceName = channelLabelOf(item);
  const isAlert = item.kind === "alert";
  const isTodo = item.kind === "todo";
  const isReplyLike = !isAlert && (!isTodo || haveReply);
  const versions = (item.options ?? []).filter((o) => o.reply.trim() && o.reply !== text);

  const [dedupedSummary, dedupedMessage, dedupedAsk, dedupedNoteText] = saidOnce([
    countedWords(item.summary ?? ""),
    item.message ?? "",
    !haveReply ? item.ask ?? "" : "",
    parsedNote.text,
  ]);
  const qc = isAlert ? extractQcFacts(item, dedupedSummary, dedupedMessage) : { structured: [], remainingFacts: item.facts ?? [] };
  const showSubject = Boolean(item.subject) && !sameWords(item.subject, dedupedMessage);

  const moreEntries: MenuEntry[] = [];
  if (haveReply && item.redo) moreEntries.push({ label: "Rewrite from a line", run: () => setPanel("rewrite") });
  if (haveReply && item.channel === "mail" && item.to.length > 0) {
    moreEntries.push({ label: "Open in Outlook", run: () => void (window.location.href = mailtoOf(item, text)) });
  }
  if (isAlert && !item.handed_from && !item.pooled) {
    moreEntries.push({ label: "Pass to a colleague", run: () => setPanel("pass") });
  }
  if (!isAlert) {
    moreEntries.push({ label: "Save a preference for my drafts", run: () => setPanel("pref") });
  }
  moreEntries.push({ label: "Make it a task", run: makeTask });
  moreEntries.push({ label: "Not needed", run: dismiss, danger: true });
  if (item.sender && !isAlert) {
    moreEntries.push({
      label: "Never draft from this sender",
      run: () =>
        run(async () => {
          await setNeverSender(item.sender, true);
          onFeedback?.("Muted future drafts from this sender");
        }),
    });
  }
  if (item.always_ok && item.category) {
    moreEntries.push({
      label: `Always do this: ${item.category_label || "this kind of item"}`,
      run: () =>
        run(async () => {
          await alwaysDoThis(item.item_id, "category");
          onFeedback?.("Turned on automatic handling");
        }),
    });
  }

  return (
    <article
      id={`prepared-${item.item_id}`}
      className="w-full space-y-6"
      data-testid={isAlert ? "prepared-alert" : "prepared-item"}
    >
      {card.provide(
        <>
          {/* Crisp Top Header: Title, Metadata Strip, Right-aligned Snooze & Dismiss */}
          <header className="space-y-3 border-b border-border pb-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <h2 className="min-w-0 flex-1 text-[18px] font-semibold leading-[1.35] tracking-[-0.02em] text-fg">
                {countedWords(headline)}
              </h2>
              <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                {isTodo && !haveReply && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={markDone}
                    className="inline-flex h-8 items-center gap-1.5 rounded-md bg-accent px-3 text-[12.5px] font-medium text-accent-fg transition-opacity hover:opacity-90 disabled:opacity-50 cursor-pointer"
                  >
                    <Check className="size-3.5" aria-hidden />
                    <span>Done</span>
                  </button>
                )}
                {isAlert && item.pooled ? (
                  <Button size="sm" variant="secondary" disabled={busy} onClick={() => void takePooled()}>
                    Take this
                  </Button>
                ) : (
                  <>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={snooze}
                      className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 text-[12.5px] font-medium text-muted transition-colors hover:border-border-strong hover:text-fg disabled:opacity-50 cursor-pointer"
                    >
                      <Clock3 className="size-3.5" aria-hidden />
                      <span>Snooze</span>
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={dismiss}
                      className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 text-[12.5px] font-medium text-muted transition-colors hover:border-border-strong hover:text-late disabled:opacity-50 cursor-pointer"
                    >
                      <X className="size-3.5" aria-hidden />
                      <span>Dismiss</span>
                    </button>
                    <MoreMenu entries={moreEntries} disabled={busy} align="right" />
                  </>
                )}
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[12.5px] text-muted">
              <StatusIndicator tone={badge.tone} label={badge.label} />
              <span className="text-border-strong" aria-hidden>
                ·
              </span>
              {senderInfo.name && (
                <>
                  <span className="font-medium text-fg" title={senderInfo.email || item.sender || undefined}>
                    {senderInfo.name}
                  </span>
                  <span className="text-border-strong" aria-hidden>
                    ·
                  </span>
                </>
              )}
              <span>{sourceName}</span>
              <span className="text-border-strong" aria-hidden>
                ·
              </span>
              <span className="num text-faint">{formatDateTime(item.created_at)}</span>
              {parsedNote.labels.map((l) => (
                <span key={l} className="text-[12px] text-faint" data-testid="prepared-label">
                  · {l}
                </span>
              ))}
            </div>
          </header>

          {/* Structured Brief: 2-col QcFactGrid for QC holds, clean summary + checklist for todos/items */}
          {(dedupedSummary || dedupedMessage || qc.structured.length > 0 || qc.remainingFacts.length > 0 || (item.todos ?? []).length > 0 || dedupedNoteText) && (
            <section className="space-y-4">
              {dedupedSummary && !sameWords(dedupedSummary, headline) && (
                <p
                  className="max-w-[68ch] text-[14px] leading-[1.65] text-fg"
                  data-testid={item.alert_kind === "mc_approval_pending" ? "alert-line" : undefined}
                >
                  {dedupedSummary}
                </p>
              )}

              {qc.structured.length > 0 && (
                <dl
                  className="grid grid-cols-1 gap-x-6 gap-y-3 rounded-xl border border-border bg-surface/60 dark:bg-[#14161b] p-4 sm:grid-cols-2"
                  data-testid="qc-fact-grid"
                >
                  {qc.structured.map((f) => (
                    <div key={f.label} className="min-w-0">
                      <dt className="text-[11.5px] font-medium text-faint">{f.label}</dt>
                      <dd className="mt-0.5 break-words text-[13.5px] font-medium leading-5 text-fg tabular-nums">
                        {f.value}
                      </dd>
                    </div>
                  ))}
                </dl>
              )}

              {dedupedMessage && !sameWords(dedupedMessage, headline) && (
                <div className="rounded-lg border border-border/80 bg-surface/40 px-4 py-3">
                  <p className="max-w-[68ch] whitespace-pre-wrap text-[13.5px] leading-[1.65] text-fg/90">
                    {countedWords(dedupedMessage)}
                  </p>
                </div>
              )}

              {(item.todos ?? []).length > 0 && (
                <div className="rounded-xl border border-border bg-surface/60 dark:bg-[#14161b] p-4">
                  <TodoSteps todos={item.todos ?? []} />
                </div>
              )}

              {qc.remainingFacts.length > 0 && (
                <ul className="list-disc space-y-1.5 pl-5 text-[13.5px] leading-[1.6] text-fg/90">
                  {qc.remainingFacts.map((f, n) => (
                    <li key={n}>{countedWords(f)}</li>
                  ))}
                </ul>
              )}

              {dedupedNoteText && (
                <p className="text-[13px] text-muted" data-testid="prepared-note">
                  {countedWords(dedupedNoteText)}
                </p>
              )}

              {(item.links ?? []).length > 0 && (
                <div className="flex flex-wrap items-center gap-2 pt-1">
                  {(item.links ?? []).map((l) => {
                    const href = canonicalObjectHref(l.href);
                    const followId = objectIdFromHref(href);
                    return (
                      <span key={l.href} className="inline-flex items-center gap-1">
                        <a
                          href={href}
                          {...(href.startsWith("/") ? {} : { target: "_blank", rel: "noopener noreferrer" })}
                          className="inline-flex h-7 items-center rounded-md border border-border-strong bg-surface px-2.5 text-[12px] font-medium text-fg transition-colors hover:bg-surface-2"
                        >
                          {l.label}
                        </a>
                        {followId && <FollowRecordButton objectId={followId} label={l.label} />}
                      </span>
                    );
                  })}
                </div>
              )}
            </section>
          )}

          {/* Interactive Draft Composer Surface (elevated #14161b Superhuman/Linear draft card) */}
          {haveReply && (
            <section
              className="overflow-hidden rounded-xl border border-border/80 bg-surface dark:bg-[#14161b]"
              aria-label="Draft reply composer"
              data-testid="inbox-draft-composer"
            >
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/80 px-4 py-2.5">
                <div className="flex items-center gap-2 text-[12.5px] font-medium text-fg">
                  {item.channel === "teams" ? (
                    <MessageSquare className="size-3.5 text-muted" aria-hidden />
                  ) : (
                    <Mail className="size-3.5 text-muted" aria-hidden />
                  )}
                  <span>{item.channel === "teams" ? "Teams reply draft" : "Email reply draft"}</span>
                </div>
                {versions.length > 0 && (
                  <div className="flex flex-wrap items-center gap-1" aria-label="Other versions">
                    {versions.map((o) => (
                      <button
                        key={o.reply}
                        type="button"
                        disabled={busy}
                        onClick={() => setText(o.reply)}
                        className="rounded-md px-2 py-1 text-[11.5px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg cursor-pointer"
                      >
                        {o.label || "Alternate draft"}
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {(item.to.length > 0 || item.cc.length > 0 || showSubject || senderInfo.email) && (
                <div className="divide-y divide-border/70 border-b border-border/80 text-[13px]">
                  <div className="flex items-baseline gap-3 px-4 py-2">
                    <span className="w-14 shrink-0 text-[12px] font-medium text-faint">To</span>
                    <span className="min-w-0 flex-1 break-words text-fg">
                      {item.to.length > 0 ? item.to.join(", ") : senderInfo.email || senderInfo.name || "Recipient on thread"}
                    </span>
                  </div>
                  {item.cc.length > 0 && (
                    <div className="flex items-baseline gap-3 px-4 py-2">
                      <span className="w-14 shrink-0 text-[12px] font-medium text-faint">Cc</span>
                      <span className="min-w-0 flex-1 break-words text-muted">{item.cc.join(", ")}</span>
                    </div>
                  )}
                  {showSubject && (
                    <div className="flex items-baseline gap-3 px-4 py-2">
                      <span className="w-14 shrink-0 text-[12px] font-medium text-faint">Subject</span>
                      <span className="min-w-0 flex-1 font-medium text-fg">{item.subject}</span>
                    </div>
                  )}
                </div>
              )}

              <div className="px-4 py-4">
                {editing ? (
                  <textarea
                    aria-label="Reply draft body"
                    value={text}
                    onChange={(e) => setText(e.target.value)}
                    rows={8}
                    className="w-full resize-y rounded-lg border border-border-strong bg-bg p-3 text-[14px] leading-[1.65] text-fg focus:border-accent focus:outline-none"
                  />
                ) : (
                  <p className="max-w-[68ch] whitespace-pre-wrap text-[14px] leading-[1.65] text-fg">{text}</p>
                )}
              </div>

              <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/80 bg-bg-subtle/60 dark:bg-[#111318] px-4 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void approveAndSend()}
                    data-testid="inbox-draft-approve"
                    className="inline-flex h-8 items-center gap-1.5 rounded-md bg-accent px-3.5 text-[13px] font-medium text-accent-fg transition-opacity hover:opacity-90 disabled:opacity-50 cursor-pointer"
                  >
                    <Send className="size-3.5" aria-hidden />
                    <span>{busy ? "Sending..." : token ? "Confirm to send" : "Approve"}</span>
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => setEditing((v) => !v)}
                    className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 text-[12.5px] font-medium text-fg transition-colors hover:border-border-strong hover:bg-surface-2 disabled:opacity-50 cursor-pointer"
                  >
                    <Pencil className="size-3.5 text-muted" aria-hidden />
                    <span>{editing ? "Done editing" : "Edit"}</span>
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void copyReply()}
                    className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 text-[12.5px] font-medium text-muted transition-colors hover:border-border-strong hover:text-fg disabled:opacity-50 cursor-pointer"
                  >
                    {copied ? <Check className="size-3.5 text-ok" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
                    <span>{copied ? "Copied" : "Copy"}</span>
                  </button>
                </div>

                <button
                  type="button"
                  disabled={busy}
                  onClick={dismiss}
                  className="inline-flex h-8 items-center gap-1.5 rounded-md px-2.5 text-[12.5px] font-medium text-muted transition-colors hover:bg-hover hover:text-late disabled:opacity-50 cursor-pointer"
                >
                  <Trash2 className="size-3.5" aria-hidden />
                  <span>Not needed</span>
                </button>
              </div>
            </section>
          )}

          {/* No draft yet: guided reply prompt for your_call / reply / todo */}
          {!haveReply && isReplyLike && (
            <section className="space-y-3.5 rounded-xl border border-border/80 bg-surface dark:bg-[#14161b] p-4">
              {dedupedAsk && (
                <p className="text-[14px] font-medium leading-[1.55] text-fg">{countedWords(dedupedAsk)}</p>
              )}
              {(item.options ?? []).length > 0 && (
                <div className="flex flex-wrap gap-2" aria-label="Suggested reply angles">
                  {(item.options ?? []).map((o) => (
                    <Button
                      key={o.reply}
                      size="sm"
                      variant="outline"
                      className="rounded-md"
                      disabled={busy}
                      onClick={() => setText(o.reply)}
                    >
                      {o.label || "Use this reply"}
                    </Button>
                  ))}
                </div>
              )}
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                <label className="sr-only" htmlFor={`reply-line-${item.item_id}`}>
                  What do you want to say?
                </label>
                <input
                  id={`reply-line-${item.item_id}`}
                  value={line}
                  maxLength={1000}
                  onChange={(e) => setLine(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && line.trim() && !busy && !waiting) {
                      e.preventDefault();
                      void writeFromLine();
                    }
                  }}
                  placeholder="Tell Assist what to say in a short line..."
                  className="h-9 min-w-0 flex-1 rounded-md border border-border-strong bg-bg px-3 text-[13.5px] text-fg placeholder:text-faint focus:border-accent focus:outline-none"
                />
                <button
                  type="button"
                  disabled={busy || waiting || !line.trim()}
                  onClick={() => void writeFromLine()}
                  className="inline-flex h-9 shrink-0 items-center justify-center rounded-md bg-accent px-3.5 text-[13px] font-medium text-accent-fg transition-opacity hover:opacity-90 disabled:opacity-40 cursor-pointer"
                >
                  {waiting ? `Writing (${formatElapsed(elapsedSec)})...` : "Draft reply"}
                </button>
              </div>
            </section>
          )}

          {/* Optional draft trigger on todo items without a draft */}
          {!haveReply && isTodo && item.channel !== "system" && (
            <div>
              <button
                type="button"
                disabled={busy || waiting}
                onClick={() => void askForDraft()}
                className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border-strong bg-surface px-3 text-[12.5px] font-medium text-fg transition-colors hover:bg-surface-2 disabled:opacity-50 cursor-pointer"
              >
                <Sparkles className="size-3.5 text-muted" aria-hidden />
                <span>{waiting ? `Drafting reply (${formatElapsed(elapsedSec)})...` : "Ask Assist to draft a reply"}</span>
              </button>
            </div>
          )}

          {/* Inline secondary drawers (rewrite, preference, pass to colleague) */}
          {panel === "rewrite" && (
            <div className="space-y-2.5 rounded-xl border border-border bg-surface p-4">
              <label htmlFor={`rewrite-${item.item_id}`} className="block text-[12.5px] font-medium text-muted">
                How should Assist rewrite this reply?
              </label>
              <div className="flex flex-wrap items-center gap-2">
                <input
                  id={`rewrite-${item.item_id}`}
                  value={line}
                  onChange={(e) => setLine(e.target.value)}
                  placeholder="Shorter, firmer date, include tracking..."
                  className="h-8 min-w-[220px] flex-1 rounded-md border border-border-strong bg-bg px-3 text-[13px] text-fg focus:border-accent focus:outline-none"
                />
                <Button size="sm" disabled={busy || !line.trim()} onClick={() => void writeFromLine()}>
                  Rewrite
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setPanel(null)}>
                  Cancel
                </Button>
              </div>
            </div>
          )}

          {panel === "pref" && (
            <div className="space-y-2.5 rounded-xl border border-border bg-surface p-4">
              <label htmlFor={`pref-${item.item_id}`} className="block text-[12.5px] font-medium text-muted">
                Save a standing preference for future drafts
              </label>
              <div className="flex flex-wrap items-center gap-2">
                <input
                  id={`pref-${item.item_id}`}
                  value={pref}
                  onChange={(e) => setPref(e.target.value)}
                  placeholder="e.g. Keep supplier chases under 3 lines"
                  className="h-8 min-w-[220px] flex-1 rounded-md border border-border-strong bg-bg px-3 text-[13px] text-fg focus:border-accent focus:outline-none"
                />
                <Button size="sm" disabled={busy || !pref.trim()} onClick={() => void savePref()}>
                  Save preference
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setPanel(null)}>
                  Cancel
                </Button>
              </div>
            </div>
          )}

          {panel === "pass" && (
            <div className="space-y-2.5 rounded-xl border border-border bg-surface p-4">
              <p className="text-[12.5px] font-medium text-muted">Pass this alert to a colleague</p>
              <div className="flex flex-wrap items-center gap-2">
                <StyledSelect
                  ariaLabel="Colleague"
                  value={passTo}
                  onChange={setPassTo}
                  options={colleagues.map((c) => ({ value: c.key, label: c.name }))}
                />
                <Button size="sm" disabled={busy || !passTo} onClick={() => void passToColleague()}>
                  Pass
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setPanel(null)}>
                  Cancel
                </Button>
              </div>
            </div>
          )}

          {note && (
            <p className="text-[13px] text-muted" role="status">
              {note}
            </p>
          )}

          {haveReply && <DraftUse use={draftUseOf(item)} />}
          {feedback}
        </>,
      )}
    </article>
  );
}

/**
 * Superhuman / Linear-grade Assist Inbox:
 * - Single sleek h-11 inline command bar with focus-only quick-try popover.
 * - Fixed-height 38% / 62% split workspace with distinct surface contrast (#121418 rail vs #0d0e11 canvas).
 * - Scannable left triage rail with quiet 6px dots, zero left-border stripes, and j/k navigation.
 * - Spacious editorial right action canvas with structured brief + elevated #14161b draft composer.
 */
export function AssistWorkbench({ linked }: { linked: string | null }) {
  const [all, setAll] = useState(false);
  const [scope, setScope] = useState<"mine" | "everyone">("mine");
  const [filter, setFilter] = useState<QueueFilter>("all");
  const [sortBy, setSortBy] = useState<QueueSort>("priority");
  const [search, setSearch] = useState("");
  const [waitingUserToggle, setWaitingUserToggle] = useState<boolean | null>(null);
  const [mobileDetailOpen, setMobileDetailOpen] = useState<boolean>(() => Boolean(linked));

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

  useEffect(() => {
    if (!linked || !data) return;
    const match = allPreparedDeduped.find((d) => d.item.item_id === linked);
    if (match && isAutomatedMcApproval(match.item) && filter !== "mc") {
      setFilter("mc");
      setMobileDetailOpen(true);
    } else if (match && isOtherPersonItem(match.item) && scope === "mine") {
      setScope("everyone");
    } else if (!match && !all && (data.older ?? 0) > 0) {
      setAll(true);
    }
  }, [linked, data, allPreparedDeduped, scope, all, filter]);

  const otherCount = useMemo(
    () => allPreparedDeduped.filter((d) => !isAutomatedMcApproval(d.item) && isOtherPersonItem(d.item)).length,
    [allPreparedDeduped],
  );

  const mcPrepared = useMemo(
    () => allPreparedDeduped.filter((d) => isAutomatedMcApproval(d.item)),
    [allPreparedDeduped],
  );

  const scopedPrepared = useMemo(() => {
    const human = allPreparedDeduped.filter((d) => !isAutomatedMcApproval(d.item));
    if (scope === "everyone" || otherCount === 0) return human;
    return human.filter((d) => !isOtherPersonItem(d.item));
  }, [allPreparedDeduped, otherCount, scope]);

  const mcEntries = useMemo<QueueEntry[]>(
    () =>
      mcPrepared.map(({ item, collapsedCount }): QueueEntry => {
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
    [mcPrepared],
  );

  const rawEntries = useMemo<QueueEntry[]>(() => {
    return [
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
    return { all: rawEntries.length, ready, input, jobs: jobCount, mc: mcEntries.length };
  }, [rawEntries, mcEntries.length]);

  const filteredEntries = useMemo(() => {
    const q = search.trim().toLowerCase();
    const source = filter === "mc" ? mcEntries : rawEntries;
    return source.filter((e) => {
      if (filter === "ready" && e.bucket !== "ready") return false;
      if (filter === "input" && e.bucket !== "input") return false;
      if (filter === "jobs" && e.kind !== "job") return false;
      if (q && !e.searchText.includes(q)) return false;
      return true;
    });
  }, [rawEntries, mcEntries, filter, search]);

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

  const visibleEntries = useMemo(() => {
    if (sortBy !== "priority") return orderedEntries;
    return [...grouped.ready, ...grouped.input, ...(waitingExpanded ? grouped.waiting : [])];
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
      setMobileDetailOpen(true);
      showToast("Job started");
    },
    [replaceJob, showToast],
  );

  const selectEntry = useCallback((key: string) => {
    setSelectedKey(key);
    setMobileDetailOpen(true);
  }, []);

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
            onClick={() => selectEntry(entry.key)}
            aria-current={isSelected ? "true" : undefined}
            data-testid="assist-queue-item"
            className={cn(
              "w-full px-3.5 py-3 text-left transition-colors cursor-pointer",
              isSelected ? "bg-row-on dark:bg-[#1b1e25]" : "hover:bg-hover",
            )}
          >
            <div className="flex items-center justify-between gap-2 text-[12px] text-muted">
              <span className="inline-flex min-w-0 items-center gap-1.5">
                <ClipboardList className={cn("size-3.5 shrink-0", isSelected ? "text-fg" : "text-muted")} aria-hidden />
                <span className="truncate font-medium text-fg/80">Assist job</span>
              </span>
              <span className="num shrink-0 text-[11.5px] text-faint">{formatDateTime(j.requested_at)}</span>
            </div>
            <p className="mt-1 truncate text-[13.5px] font-semibold leading-snug text-fg">{j.text}</p>
            <div className="mt-1 flex items-center justify-between gap-2 text-[12px]">
              <span className="min-w-0 flex-1 truncate text-muted">{preview || "Background job"}</span>
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
    const entityLabel =
      sender.name || (it.kind === "alert" ? "Alert" : it.channel === "teams" ? "Teams" : it.channel === "mail" ? "Mail" : "Task");

    return (
      <li key={entry.key} className="group relative">
        <button
          type="button"
          onClick={() => selectEntry(entry.key)}
          aria-current={isSelected ? "true" : undefined}
          data-testid="assist-queue-item"
          className={cn(
            "w-full px-3.5 py-3 pr-9 text-left transition-colors cursor-pointer",
            isSelected ? "bg-row-on dark:bg-[#1b1e25]" : "hover:bg-hover",
          )}
        >
          <div className="flex items-center justify-between gap-2 text-[12px] text-muted">
            <span className="inline-flex min-w-0 items-center gap-1.5">
              <PreparedIcon item={it} active={isSelected} />
              <span className="truncate font-medium text-fg/85" title={sender.email || it.sender || undefined}>
                {entityLabel}
              </span>
              {entry.collapsedCount > 1 && (
                <span className="num shrink-0 text-[11px] text-faint">{entry.collapsedCount} messages</span>
              )}
            </span>
            <span className="num shrink-0 text-[11.5px] text-faint">{formatDateTime(it.created_at)}</span>
          </div>
          <p className="mt-1 truncate text-[13.5px] font-semibold leading-snug text-fg">{headline}</p>
          <div className="mt-1 flex items-center justify-between gap-2 text-[12px]">
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
      {/* Sleek single-row h-11 inline command strip */}
      <div className="shrink-0" data-testid="assist-jobs">
        <InlineCommandStrip onGiven={handleJobGiven} />
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
      ) : rawEntries.length === 0 && otherCount === 0 && mcEntries.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center px-6 py-16 text-center" data-testid="prepared-empty">
          <Bot className="mx-auto mb-3 size-6 text-muted" aria-hidden />
          <p className="text-[15px] font-medium text-fg">Nothing is prepared for you right now.</p>
          <p className="mt-1.5 text-[13.5px] text-muted">
            Type a job in the command bar above, or check your settings under Memory &amp; Rules.
          </p>
        </div>
      ) : (
        /* Fixed-Height 38% / 62% Master-Detail Split Workspace */
        <div className="grid h-[calc(100dvh-148px)] min-h-[560px] flex-1 grid-cols-1 overflow-hidden lg:grid-cols-[minmax(340px,38%)_1fr]">
          {/* Left Recessed Triage Rail (#121418 in dark) */}
          <aside
            className={cn(
              "min-h-0 flex-col border-b border-border bg-list dark:bg-[#121418] lg:flex lg:border-r lg:border-b-0 overflow-hidden",
              mobileDetailOpen && active ? "hidden lg:flex" : "flex",
            )}
            aria-label="Assist queue"
            data-testid="assist-queue"
          >
            {/* Tight Triage Rail Controls */}
            <div className="shrink-0 space-y-2 border-b border-border px-3 py-2.5" data-testid="assist-topbar">
              <div className="flex items-center gap-2">
                <div className="relative min-w-0 flex-1">
                  <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-faint" aria-hidden />
                  <input
                    type="search"
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder="Search queue..."
                    aria-label="Search queue"
                    className="h-8 w-full rounded-md border border-border bg-bg dark:bg-[#0d0e11] pl-8 pr-7 text-[12.5px] text-fg placeholder:text-faint focus:border-border-strong focus:outline-none"
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
                        "h-7 rounded-md px-2 text-[12px] font-medium transition-colors cursor-pointer",
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
                        "h-7 rounded-md px-2 text-[12px] font-medium transition-colors cursor-pointer",
                        scope === "everyone" ? "liquid-glass-active" : "text-muted hover:text-fg",
                      )}
                    >
                      Team
                    </button>
                  </div>
                )}

                <label className="shrink-0">
                  <span className="sr-only">Sort queue</span>
                  <select
                    aria-label="Sort queue"
                    value={sortBy}
                    onChange={(e) => setSortBy(e.target.value as QueueSort)}
                    data-testid="assist-queue-sort"
                    className="h-8 cursor-pointer rounded-md border border-border bg-bg dark:bg-[#0d0e11] px-2 text-[12px] font-medium text-muted hover:border-border-strong hover:text-fg focus:border-border-strong focus:outline-none"
                  >
                    <option value="priority">Priority</option>
                    <option value="newest">Newest</option>
                    <option value="due">Due soonest</option>
                  </select>
                </label>
              </div>

              <div
                className="flex items-center gap-1 overflow-x-auto"
                role="group"
                aria-label="Filter queue by status"
              >
                {(
                  [
                    ["all", "All", filterCounts.all],
                    ["ready", "Ready", filterCounts.ready],
                    ["input", "Needs input", filterCounts.input],
                    ["jobs", "Jobs", filterCounts.jobs],
                    ...(filterCounts.mc > 0 ? ([["mc", "MC approvals", filterCounts.mc]] as const) : []),
                  ] as const
                ).map(([key, label, count]) => (
                  <button
                    key={key}
                    type="button"
                    aria-pressed={filter === key}
                    onClick={() => setFilter(key)}
                    className={cn(
                      "inline-flex h-7 shrink-0 items-center gap-1 rounded-md px-2 text-[12px] font-medium transition-colors cursor-pointer",
                      filter === key
                        ? "bg-row-on dark:bg-[#1b1e25] text-fg"
                        : "text-muted hover:bg-hover hover:text-fg",
                    )}
                  >
                    <span>{label}</span>
                    {count > 0 && (
                      <span className={cn("num text-[11px]", filter === key ? "text-fg/80" : "text-faint")}>
                        {count}
                      </span>
                    )}
                  </button>
                ))}
              </div>
            </div>

            {/* Scrollable Triage List */}
            {rawEntries.length === 0 && filter !== "mc" ? (
              <div className="px-4 py-10 text-center" data-testid="mine-empty">
                <p className="text-[13.5px] font-medium text-fg">Nothing on your own list right now.</p>
                {otherCount > 0 ? (
                  <button
                    type="button"
                    onClick={() => setScope("everyone")}
                    className="mt-2 cursor-pointer text-[13px] font-medium text-accent hover:underline"
                  >
                    Show team items ({otherCount})
                  </button>
                ) : mcEntries.length > 0 ? (
                  <button
                    type="button"
                    onClick={() => setFilter("mc")}
                    className="mt-2 cursor-pointer text-[13px] font-medium text-accent hover:underline"
                  >
                    View MC approvals ({mcEntries.length})
                  </button>
                ) : null}
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
                        <div className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-list/95 dark:bg-[#121418]/95 px-3.5 py-1.5 backdrop-blur-sm">
                          <span className="text-[11.5px] font-medium text-muted">Ready to act</span>
                          <span className="num text-[11.5px] text-faint">{grouped.ready.length}</span>
                        </div>
                        <ul className="divide-y divide-border/70">{grouped.ready.map(renderRow)}</ul>
                      </section>
                    )}

                    {grouped.input.length > 0 && (
                      <section aria-label="Needs input" data-testid="queue-section-input">
                        <div className="sticky top-0 z-10 flex items-center justify-between border-y border-border bg-list/95 dark:bg-[#121418]/95 px-3.5 py-1.5 backdrop-blur-sm first:border-t-0">
                          <span className="text-[11.5px] font-medium text-muted">Needs input</span>
                          <span className="num text-[11.5px] text-faint">{grouped.input.length}</span>
                        </div>
                        <ul className="divide-y divide-border/70">{grouped.input.map(renderRow)}</ul>
                      </section>
                    )}

                    {grouped.waiting.length > 0 && (
                      <section aria-label="Waiting" data-testid="queue-section-waiting">
                        <button
                          type="button"
                          aria-expanded={waitingExpanded}
                          onClick={() => setWaitingUserToggle(!waitingExpanded)}
                          className="sticky top-0 z-10 flex w-full items-center justify-between border-y border-border bg-list/95 dark:bg-[#121418]/95 px-3.5 py-1.5 text-left transition-colors hover:bg-hover backdrop-blur-sm cursor-pointer"
                        >
                          <span className="inline-flex items-center gap-1.5 text-[11.5px] font-medium text-muted">
                            {waitingExpanded ? (
                              <ChevronDown className="size-3.5" aria-hidden />
                            ) : (
                              <ChevronRight className="size-3.5" aria-hidden />
                            )}
                            <span>Waiting</span>
                          </span>
                          <span className="num text-[11.5px] text-faint">{grouped.waiting.length}</span>
                        </button>
                        {waitingExpanded && (
                          <ul className="divide-y divide-border/70">{grouped.waiting.map(renderRow)}</ul>
                        )}
                      </section>
                    )}
                  </div>
                ) : (
                  <ul className="divide-y divide-border/70">{orderedEntries.map(renderRow)}</ul>
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
                  <div className="border-t border-border px-3.5 py-2.5">
                    <button
                      type="button"
                      onClick={() => setAll(true)}
                      className="w-full cursor-pointer rounded-md py-1.5 text-center text-[12.5px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg"
                      data-testid="assist-older"
                    >
                      Show older items
                    </button>
                  </div>
                )}
              </div>
            )}
          </aside>

          {/* Right Spacious Editorial Action Canvas (#0d0e11 in dark) */}
          <section
            className={cn(
              "min-h-0 min-w-0 overflow-y-auto bg-bg dark:bg-[#0d0e11] px-5 py-5 lg:block lg:px-7 lg:py-6",
              mobileDetailOpen && active ? "block" : "hidden lg:block",
            )}
            aria-label="Selected Assist item"
            data-testid="assist-detail"
          >
            <div className="mb-4 border-b border-border pb-3 lg:hidden">
              <button
                type="button"
                onClick={() => setMobileDetailOpen(false)}
                data-testid="assist-mobile-back"
                className="inline-flex items-center gap-1.5 text-[13px] font-medium text-muted transition-colors hover:text-fg cursor-pointer"
              >
                <ArrowLeft className="size-3.5" aria-hidden />
                <span>Back to queue</span>
              </button>
            </div>

            <div className="max-w-3xl">
              {active?.kind === "job" ? (
                <JobCard key={active.job.job_id} job={active.job} onJob={replaceJob} onFeedback={showToast} />
              ) : active?.kind === "prepared" ? (
                active.item.kind === "job" ? (
                  <ul aria-label="Prepared for me">
                    <PreparedJobCard
                      key={active.item.item_id}
                      item={active.item}
                      linked={active.item.item_id === linked}
                      detail
                      onGone={onGone}
                      onOptimistic={onOptimistic}
                      onRestore={onRestore}
                      onFeedback={showToast}
                      feedback={<PreparedFeedback item={active.item} />}
                    />
                  </ul>
                ) : (
                  <InboxPreparedCanvas
                    key={active.item.item_id}
                    item={active.item}
                    onGone={onGone}
                    onChanged={retry}
                    onOptimistic={onOptimistic}
                    onRestore={onRestore}
                    onFeedback={showToast}
                    feedback={<PreparedFeedback item={active.item} />}
                  />
                )
              ) : (
                <div className="py-16 text-center text-[13.5px] text-muted">
                  Select an item from the queue to inspect or approve it.
                </div>
              )}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
