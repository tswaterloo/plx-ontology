"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { CardActions } from "@/components/card-actions";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadProblem } from "@/components/ui/notice";
import { getApprovals } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import { plural } from "@/lib/format";
import type { ActionResult, ApprovalsQueue } from "@/lib/types";

// One approvals queue: mail asks no approver answered and Mission Control gates, by who
// they wait on, oldest first. A nudge is a draft: it goes out only through the card's
// preview and your confirm.
export default function ApprovalsPage() {
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
  if (!queue) return <Skeleton className="h-64 w-full" />;
  return (
    <div className="space-y-4" data-testid="approvals-queue">
      <h1 className="text-lg font-semibold">Approvals waiting</h1>
      <p className="text-[13px] text-muted">
        {plural(queue.total, "approval")} waiting as of {queue.as_of}
        {Object.entries(queue.by_aging ?? {}).map(([bucket, n]) => ` · ${n} ${bucket}`).join("")}. Nothing is sent until you confirm it.
      </p>
      {(queue.groups ?? []).length === 0 && <p className="text-[13px] text-muted">No approval you may see is waiting.</p>}
      {(queue.groups ?? []).map((g) => (
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
                <pre className="whitespace-pre-wrap rounded bg-hover p-2 text-[12px]">{item.nudge.body}</pre>
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
