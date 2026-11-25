"use client";

// Things to do by hand that no prepared change reaches (a system with no write path, a
// question an hourly look raised). The person marks the item done or not needed; nothing
// here writes to any system.

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { ItemActions } from "@/components/autopilot/item-actions";
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

export function TodoCard({ item, linked, onGone, feedback }: { item: PreparedItem; linked?: boolean; onGone: (id: string) => void; feedback?: React.ReactNode }) {
  const [busy, setBusy] = useState(false);
  const card = useProblemSlot();
  const setError = card.set;
  const [asked, setAsked] = useState(false);
  // an Assist matter with no draft yet: "Draft it" asks for one (the list shows it when ready)
  const canDraft = item.plan_status === "deferred" || item.plan_status === "failed";
  const draft = async () => {
    setBusy(true);
    setError(null);
    try {
      await requestDraft(item.item_id);
      setAsked(true);
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const decide = async (decision: "accept") => {
    setBusy(true);
    setError(null);
    try {
      await decideAutopilotItem(item.item_id, { decision });
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
        "max-w-[50rem] space-y-3 rounded-lg border border-border bg-surface/60 p-4 transition-colors",
        linked && "border-accent/70 ring-1 ring-accent/30",
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
            <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
              {item.sender && <span className="text-[14px] font-semibold text-fg">{item.sender}</span>}
              {subHeader && (
                <span className="inline-flex max-w-full items-center truncate rounded-md border border-border/70 bg-bg/80 px-2 py-0.5 text-[12px] text-muted">
                  {subHeader}
                </span>
              )}
              {n.labels.map((l) => (
                <p key={l} className="inline-flex items-center rounded-md bg-surface-2 px-2 py-0.5 text-[11px] leading-4 text-muted" data-testid="prepared-label">
                  {l}
                </p>
              ))}
              <span className="num ml-auto text-[12px] text-muted">{formatDateTime(item.created_at)}</span>
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
      <TodoSteps todos={item.todos ?? []} />
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/70 pt-3">
        <div className="min-w-0">
          {card.provide(<ItemActions
            item={item}
            onGone={onGone}
            busy={busy}
            primary={
              <>
                <Button size="sm" disabled={busy} onClick={() => void decide("accept")}>
                  Done
                </Button>
                {canDraft && !asked && (
                  <Button size="sm" variant="outline" disabled={busy} onClick={() => void draft()}>
                    {item.plan_status === "failed" ? "Ask again" : "Draft it"}
                  </Button>
                )}
                {asked && <span className="text-[12px] text-muted" data-testid="draft-asked">Drafting. It shows here when it is ready.</span>}
              </>
            }
          />)}
        </div>
        {feedback && <div className="shrink-0" data-testid="prepared-feedback-row">{feedback}</div>}
      </div>
      {card.banner}
    </li>
  );
}
