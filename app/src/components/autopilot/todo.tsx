"use client";

// Things to do by hand that no prepared change reaches (a system with no write path, a
// question an hourly look raised). The person marks the item done or not needed; nothing
// here writes to any system.

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { ItemActions } from "@/components/autopilot/item-actions";
import { ErrorNotice } from "@/components/ui/notice";
import { decideAutopilotItem, type PreparedItem, type TodoStep } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
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
  const [error, setError] = useState<string | null>(null);
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
        <span className="text-[13px] font-medium">{item.sender || "From the records"}</span>
        {item.subject && <span className="text-[12px] text-muted">{item.subject}</span>}
        <span className="num ml-auto text-[12px] text-muted">{formatDateTime(item.created_at)}</span>
      </div>
      {item.summary && <p className="max-w-[46rem] text-[13px]">{item.summary}</p>}
      {item.message && <p className="max-w-[46rem] whitespace-pre-wrap text-[12px] text-muted">{item.message}</p>}
      <TodoSteps todos={item.todos ?? []} />
      <ItemActions
        item={item}
        onGone={onGone}
        busy={busy}
        primary={
          <Button size="sm" disabled={busy} onClick={() => void decide("accept")}>
            Done
          </Button>
        }
      />
      {error && <ErrorNotice message={error} />}
    </li>
  );
}
