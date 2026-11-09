"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { CardActions } from "@/components/card-actions";
import { EmptyState } from "@/components/ui/empty-state";
import { Pending } from "@/components/ui/skeleton";
import { cn } from "@/lib/cn";
import { plural } from "@/lib/format";
import { LoadProblem } from "@/components/ui/notice";
import { getApprovals } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import type { ActionResult, ApprovalsQueue } from "@/lib/types";

// Team, Approvals: mail asks no approver answered and Mission Control gates, by who they
// wait on, oldest first. A nudge is a draft: it goes out only through the card's preview
// and your confirm.
export function TeamApprovals() {
  const [aging, setAging] = useState<string | null>(null);
  const [queue, setQueue] = useState<ApprovalsQueue | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const [states, setStates] = useState<Record<string, ActionResult>>({});
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    setQueue(null);
    getApprovals(ctl.signal).then(setQueue, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [round]);
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!queue) return <Pending label="Reading the approvals" />;
  const groups = (queue.groups ?? [])
    .map((g) => ({ ...g, items: aging ? g.items.filter((i) => i.aging === aging) : g.items }))
    .filter((g) => g.items.length > 0);
  return (
    <div className="space-y-4" data-testid="approvals-queue">
      {Object.keys(queue.by_aging ?? {}).length > 0 && (
        <div className="flex flex-wrap gap-1.5" role="tablist" aria-label="Waiting for">
          {[["all", queue.total] as const, ...Object.entries(queue.by_aging ?? {})].map(([bucket, n]) => {
            const on = (bucket === "all" ? null : bucket) === aging;
            return (
              <button key={bucket} type="button" role="tab" aria-selected={on} onClick={() => setAging(bucket === "all" ? null : bucket)} className={cn("inline-flex h-7 items-center gap-1.5 rounded-full px-3 text-[12px] cursor-pointer", on ? "bg-[color-mix(in_srgb,var(--fg)_10%,transparent)] font-medium text-fg" : "text-muted hover:bg-hover")}>
                {bucket === "all" ? "All" : bucket}
                <span className="tabular-nums text-faint">{n}</span>
              </button>
            );
          })}
        </div>
      )}
      {groups.length === 0 && <EmptyState title="No approvals waiting." />}
      {groups.map((g) => (
        <section key={g.waiting_on} className="space-y-2 rounded-lg border border-border p-3">
          <h2 className="text-[14px] font-medium">
            Waiting on {g.waiting_on} · {plural(g.count, "item")} · oldest {g.oldest_days} business days
          </h2>
          <ul className="space-y-2">
            {g.items.map((item) => (
              <li key={item.id} className="space-y-1 text-[13px]">
                <div>
                  <span className="font-medium">{item.aging}</span> · {item.channel}
                  {item.family ? ` · ${item.family.replace(/_/g, " ")}` : ""}
                  {item.asked_by ? ` · asked by ${item.asked_by}` : ""}
                </div>
                <div className="text-muted">{item.summary}</div>
                {item.object_id && (
                  <Link className="font-medium hover:underline" href={`/object/${encodeURIComponent(item.object_id.split(":")[0])}/${encodeURIComponent(item.object_id)}`}>
                    Open the message
                  </Link>
                )}
                {item.nudge.body ? <pre className="whitespace-pre-wrap rounded bg-hover p-2 text-[12px]">{item.nudge.body}</pre> : <p className="text-muted">No draft.</p>}
                {item.card_id && (
                  <CardActions
                    card={{ id: item.card_id, subject: { label: item.summary ?? "Approval waiting" }, actions: item.offers }}
                    state={states[item.card_id] ?? null}
                    onDone={(r) => setStates((s) => ({ ...s, [item.card_id as string]: r }))}
                  />
                )}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
