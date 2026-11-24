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

export function TodoCard({ item, linked, onGone }: { item: PreparedItem; linked?: boolean; onGone: (id: string) => void }) {
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
    <li id={`prepared-${item.item_id}`} className={cn("space-y-2 py-4", linked && "border-l-2 border-accent pl-3")} data-testid="prepared-todo">
      <div className="flex flex-wrap items-baseline gap-x-3">
        {item.sender && <span className="text-[13px] font-medium">{item.sender}</span>}
        {item.subject && item.subject.trim() !== (item.summary ?? "").trim() && <span className="text-[12px] text-muted">{item.subject}</span>}
        <span className="num ml-auto text-[12px] text-muted">{formatDateTime(item.created_at)}</span>
      </div>
      {item.summary && <p className="max-w-[46rem] text-[13px]">{countedWords(item.summary)}</p>}
      {item.message && item.message.trim() !== (item.summary ?? "").trim() && <p className="max-w-[46rem] whitespace-pre-wrap text-[12px] text-muted">{item.message}</p>}
      {item.note && <p className="max-w-[46rem] text-[12px] text-muted" data-testid="prepared-note">{item.note}</p>}
      <TodoSteps todos={item.todos ?? []} />
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
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => void draft()}>
                {item.plan_status === "failed" ? "Ask again" : "Draft it"}
              </Button>
            )}
            {asked && <span className="text-[12px] text-muted" data-testid="draft-asked">Drafting. It shows here when it is ready.</span>}
          </>
        }
      />)}
      {card.banner}
    </li>
  );
}
