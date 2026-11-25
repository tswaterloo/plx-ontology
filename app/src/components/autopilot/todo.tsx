"use client";

// Things to do by hand that no prepared change reaches (a system with no write path, a
// question an hourly look raised). The person marks the item done or not needed; nothing
// here writes to any system.

import { Fragment, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { ItemActions, type OptimisticCallbacks } from "@/components/autopilot/item-actions";
import { useProblemSlot } from "@/components/ui/notice";
import { decideAutopilotItem, requestDraft, type PreparedItem, type TodoStep } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { countedWords, formatDateTime } from "@/lib/format";
import { noteParts, saidOnce } from "@/components/autopilot/lines";
import { cn } from "@/lib/cn";

const SYSTEMS: Record<string, string> = {
  bc: "Business Central",
  portal: "Portal",
  mc: "Mission Control",
  planner: "Planner",
  calendar: "Calendar",
  sharepoint: "SharePoint",
  sage: "Sage",
  filemaker: "FileMaker",
  mail: "Mail",
  teams: "Teams",
};

/** The steps as a list: system and record first when the step names them. */
export function TodoSteps({ todos }: { todos: TodoStep[] }) {
  if (todos.length === 0) return null;
  return (
    <div className="max-w-[46rem] space-y-1" data-testid="todo-steps">
      <p className="text-[12px] text-muted">To do by hand</p>
      <ul className="list-disc space-y-0.5 pl-4 text-[13px]">
        {todos.map((t, n) => {
          const where = [SYSTEMS[t.system.toLowerCase()] ?? t.system, t.record].filter((x) => x.trim() !== "").join(", ");
          return (
            <li key={n}>
              {where && <span className="font-medium">{where}: </span>}
              {t.what}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function elapsedLabel(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return rem ? `${m}m ${rem}s` : `${m}m`;
}

function startedAtMs(requestedAt?: string | null): number {
  if (requestedAt) {
    const parsed = Date.parse(requestedAt);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return Date.now();
}

export function TodoCard({
  item,
  linked,
  onGone,
  onChanged,
  onOptimistic,
  onRestore,
  onFeedback,
  feedback,
}: {
  item: PreparedItem;
  linked?: boolean;
  onGone: (id: string) => void;
  onChanged?: () => void;
  feedback?: React.ReactNode;
} & OptimisticCallbacks) {
  const [busy, setBusy] = useState(false);
  const card = useProblemSlot();
  const setError = card.set;
  const initiallyWorking = item.plan_status === "working";
  const [waitingSince, setWaitingSince] = useState<number | null>(() => (initiallyWorking ? startedAtMs(item.requested_at) : null));
  const [sawWorking, setSawWorking] = useState<boolean>(initiallyWorking);
  const [baselinePlanAt, setBaselinePlanAt] = useState<string | null>(null);
  const [elapsedSec, setElapsedSec] = useState<number>(() => (initiallyWorking ? Math.max(0, Math.floor((Date.now() - startedAtMs(item.requested_at)) / 1000)) : 0));
  const [draftNote, setDraftNote] = useState<string | null>(null);
  const asked = waitingSince !== null;

  useEffect(() => {
    if (waitingSince === null) return;
    setElapsedSec(Math.max(0, Math.floor((Date.now() - waitingSince) / 1000)));
    const tick = setInterval(() => setElapsedSec(Math.max(0, Math.floor((Date.now() - waitingSince) / 1000))), 1000);
    const poll = onChanged ? setInterval(onChanged, 4000) : null;
    return () => {
      clearInterval(tick);
      if (poll) clearInterval(poll);
    };
  }, [waitingSince, onChanged]);

  useEffect(() => {
    if (item.plan_status === "working") {
      setSawWorking(true);
      if (waitingSince === null) setWaitingSince(startedAtMs(item.requested_at));
      return;
    }
    const planMoved = baselinePlanAt !== null && Boolean(item.plan_at) && item.plan_at !== baselinePlanAt;
    if (waitingSince !== null && (sawWorking || planMoved)) {
      setWaitingSince(null);
      setSawWorking(false);
      setBaselinePlanAt(null);
      if (item.plan_status === "failed") {
        setDraftNote("Could not prepare a draft for this item. Ask again, or handle it directly.");
      } else {
        setDraftNote(null);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item.plan_status, item.plan_at, item.requested_at]);

  // An Assist matter with no draft yet: offer a direct draft action ("Draft a follow-up" for
  // waiting items, "Draft reply" for open mail/Teams items, "Draft it" for deferred, "Ask again" for failed).
  const isWaiting = /^No answer yet\b/i.test((item.summary ?? "").trim()) || /^No answer yet\b/i.test((item.subject ?? "").trim());
  const isPromise = /^You said:/i.test((item.summary ?? "").trim());
  const isMessageChannel = item.channel === "mail" || item.channel === "teams";
  const canFollowUp = isWaiting && item.plan_status !== "working" && item.plan_status !== "ready" && item.plan_status !== "drafted";
  const canDraftReply = isMessageChannel && !isWaiting && !isPromise && (item.plan_status === "planned" || item.plan_status === "none");
  const canDraft = item.plan_status === "deferred" || item.plan_status === "failed" || canFollowUp || canDraftReply;
  const draft = async () => {
    setBusy(true);
    setError(null);
    setDraftNote(null);
    try {
      await requestDraft(item.item_id);
      setBaselinePlanAt(item.plan_at ?? null);
      setSawWorking(item.plan_status === "working");
      setWaitingSince(Date.now());
      setElapsedSec(0);
      onFeedback?.(canFollowUp ? "Follow-up draft requested" : "Draft requested");
      onChanged?.();
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const decide = async (decision: "accept") => {
    if (onOptimistic) {
      setError(null);
      onOptimistic(item.item_id, "Marked done");
      try {
        await decideAutopilotItem(item.item_id, { decision });
      } catch (e) {
        const msg = plainError(e);
        setError(msg);
        onRestore?.(item.item_id, msg);
      }
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await decideAutopilotItem(item.item_id, { decision });
      onFeedback?.("Marked done");
      onGone(item.item_id);
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <li
      id={`prepared-${item.item_id}`}
      className={cn(
        "w-full space-y-3 py-1 transition-colors",
        linked && "border-l-2 border-l-accent pl-3",
      )}
      data-testid="prepared-todo"
    >
      {(() => {
        const n = noteParts(item.note ?? "");
        const [subjectLine, summary, noteText, message] = saidOnce([item.subject ?? "", item.summary ?? "", n.text, item.message ?? ""]);
        const headline = summary || subjectLine;
        const subHeader = summary && subjectLine ? subjectLine : "";
        return (
          <>
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              {item.sender && <span className="text-[14px] font-semibold text-fg">{item.sender}</span>}
              {subHeader && (
                <>
                  <span className="text-faint" aria-hidden>·</span>
                  <span className="max-w-full truncate text-[13px] text-muted">
                    {subHeader}
                  </span>
                </>
              )}
              {n.labels.map((l) => (
                <Fragment key={l}>
                  <span className="text-faint" aria-hidden>·</span>
                  <p className="text-[11.5px] leading-4 text-faint" data-testid="prepared-label">
                    {l}
                  </p>
                </Fragment>
              ))}
              <span className="num ml-auto text-[12px] text-faint">{formatDateTime(item.created_at)}</span>
            </div>
            {headline && <p className="text-[14px] font-medium leading-relaxed text-fg">{countedWords(headline)}</p>}
            {noteText && <p className="text-[13px] leading-relaxed text-muted" data-testid="prepared-note">{noteText}</p>}
            {message && (
              <p className="whitespace-pre-wrap border-l-2 border-border-strong pl-3 text-[13px] leading-relaxed text-muted">
                {message}
              </p>
            )}
          </>
        );
      })()}
      {(item.facts ?? []).length > 0 && (
        <ul className="list-disc space-y-0.5 pl-4 text-[13px] leading-relaxed text-muted" data-testid="todo-facts">
          {(item.facts ?? []).map((f, i) => (
            <li key={`${i}-${f}`}>{countedWords(f)}</li>
          ))}
        </ul>
      )}
      <TodoSteps todos={item.todos ?? []} />
      {(item.links ?? []).length > 0 && (
        <div className="flex flex-wrap gap-3 text-[13px]">
          {(item.links ?? []).map((l) => (
            <a key={l.href} href={l.href} {...(l.href.startsWith("/") ? {} : { target: "_blank", rel: "noopener noreferrer" })} className="text-accent underline-offset-2 hover:underline">
              {l.label}
            </a>
          ))}
        </div>
      )}
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/70 pt-3">
        <div className="min-w-0">
          {card.provide(<ItemActions
            item={item}
            onGone={onGone}
            onChanged={onChanged}
            onOptimistic={onOptimistic}
            onRestore={onRestore}
            onFeedback={onFeedback}
            busy={busy}
            primary={
              <>
                <Button size="sm" disabled={busy} onClick={() => void decide("accept")}>
                  {busy ? "Saving..." : "Done"}
                </Button>
                {canDraft && !asked && (
                  <Button size="sm" variant="outline" disabled={busy} onClick={() => void draft()}>
                    {busy
                      ? "Requesting..."
                      : canFollowUp && item.plan_status !== "failed"
                        ? "Draft a follow-up"
                        : item.plan_status === "failed"
                          ? "Ask again"
                          : canDraftReply
                            ? "Draft reply"
                            : "Draft it"}
                  </Button>
                )}
                {asked && (
                  <span className="text-[12px] text-muted" role="status" data-testid="draft-asked">
                    Drafting ({elapsedLabel(elapsedSec)}). It shows here when it is ready.
                  </span>
                )}
              </>
            }
          />)}
        </div>
        {feedback && <div className="shrink-0" data-testid="prepared-feedback-row">{feedback}</div>}
      </div>
      {draftNote && <p className="text-[12px] text-muted">{draftNote}</p>}
      {card.banner}
    </li>
  );
}
