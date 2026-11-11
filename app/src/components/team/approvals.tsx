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
/** Where an ask was made, as the reader names it. */
const CHANNEL: Record<string, string> = { mail: "Email", email: "Email", teams: "Teams", mission_control: "Mission Control" };

/** The server groups an ask with no named approver under "not recorded". */
const waitingOn = (who: string) => (who === "not recorded" ? "an approver not on record" : who);

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
              <button key={bucket} type="button" role="tab" aria-selected={on} onClick={() => setAging(bucket === "all" ? null : bucket)} className={cn("inline-flex h-7 items-center gap-1.5 rounded-[3px] px-3 text-[12px] cursor-pointer", on ? "bg-[color-mix(in_srgb,var(--fg)_10%,transparent)] font-medium text-fg" : "text-muted hover:bg-hover")}>
                {bucket === "all" ? "All" : bucket.replace(/^./, (c) => c.toUpperCase())}
                <span className="num text-faint">{n}</span>
              </button>
            );
          })}
        </div>
      )}
      {groups.length === 0 && <EmptyState title="No approvals waiting." />}
      {groups.map((g) => (
        <section key={g.waiting_on} className="space-y-2 rounded-lg border border-border p-3" aria-label={`Waiting on ${waitingOn(g.waiting_on)}`}>
          <h3 className="text-[14px] font-medium">
            Waiting on {waitingOn(g.waiting_on)}
            <span className="font-normal text-muted">
              {" "}
              · {plural(g.count, "item")} · oldest {plural(g.oldest_days, "business day")}
            </span>
          </h3>
          <ul className="divide-y divide-border">
            {g.items.map((item) => (
              <li key={item.id} className="space-y-1.5 py-2.5 text-[13px] first:pt-1 last:pb-1" data-testid="approval-item">
                <p className="font-medium">{item.summary || "Approval waiting"}</p>
                <p className="text-[12px] text-muted">
                  {[
                    plural(item.days, "business day"),
                    CHANNEL[item.channel] ?? item.channel.replace(/_/g, " "),
                    item.family ? item.family.replace(/_/g, " ") : null,
                    item.asked_by ? `asked by ${item.asked_by}` : null,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
                {item.object_id && (
                  <Link className="inline-block text-[12px] font-medium hover:underline" href={`/object/${encodeURIComponent(item.object_id.split(":")[0])}/${encodeURIComponent(item.object_id)}`}>
                    Open the message
                  </Link>
                )}
                {item.nudge.body ? (
                  <div className="rounded-lg bg-surface p-2.5">
                    <p className="mb-1 text-[12px] text-muted">Draft nudge, not sent</p>
                    <p className="whitespace-pre-wrap text-[13px] leading-5">{item.nudge.body}</p>
                  </div>
                ) : (
                  <p className="text-[12px] text-muted">No draft nudge.</p>
                )}
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
