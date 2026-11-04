"use client";

import { useCallback, useEffect, useState } from "react";
import { Card, CardBody } from "@/components/ui/card";
import { LoadProblem } from "@/components/ui/notice";
import { PageHeader } from "@/components/ui/page-header";
import { SkeletonRows } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { actionChip } from "@/lib/actions";
import { getActionsLog } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import type { ActionLogRow } from "@/lib/types";

const KIND: Record<string, string> = {
  send_email: "Email",
  reply_in_thread: "Reply",
  forward: "Forward",
  teams_message: "Teams",
  create_follow_up: "Follow-up",
  mark_done: "Done",
  snooze: "Snooze",
  dismiss: "Dismiss",
};

function What({ r }: { r: ActionLogRow }) {
  if (r.to?.length || r.subject) {
    return (
      <>
        {r.subject && <span className="block">{r.subject}</span>}
        <span className="block text-muted">
          To {(r.to ?? []).join(", ") || "—"}
          {(r.cc ?? []).length > 0 && ` · Cc ${(r.cc ?? []).join(", ")}`}
        </span>
      </>
    );
  }
  return <span className="text-muted">{r.text}</span>;
}

/** Your actions journal: every action you confirmed, newest first (append-only on the server). */
export default function ActionsPage() {
  const [rows, setRows] = useState<ActionLogRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    getActionsLog(c.signal)
      .then((r) => setRows(r.rows ?? []))
      .catch((e) => !c.signal.aborted && setError(plainError(e)));
    return () => c.abort();
  }, []);
  useEffect(() => load(), [load]);
  return (
    <div className="space-y-6">
      <PageHeader eyebrow="Actions" title="What you did from the Workbench" sub="Every action you confirmed on a suggestion: who, when, to whom, the cited facts it carried and what happened. Kept append-only; message text is not stored, only its fingerprint." />
      <Card>
        <CardBody>
          {error ? (
            <LoadProblem message={error} onRetry={load} />
          ) : !rows ? (
            <SkeletonRows rows={4} label="Reading your actions" />
          ) : rows.length === 0 ? (
            <p className="text-row text-muted">No actions yet. Use the buttons on a card under What to do next on Today.</p>
          ) : (
            <table className="w-full text-row" data-testid="actions-log">
              <thead>
                <tr className="text-left text-meta text-muted">
                  <th className="pb-2 font-medium">When</th>
                  <th className="pb-2 font-medium">Record</th>
                  <th className="pb-2 font-medium">Action</th>
                  <th className="hidden pb-2 font-medium md:table-cell">What</th>
                  <th className="pb-2 font-medium">Result</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {rows.map((r) => {
                  const chip = actionChip(r, false);
                  return (
                    <tr key={r.seq} className="align-top" data-testid="actions-log-row">
                      <td className="whitespace-nowrap py-2 pr-3 tabular-nums text-meta">{formatDateTime(r.at)}</td>
                      <td className="py-2 pr-3">{r.card_label || r.card_id}</td>
                      <td className="py-2 pr-3">
                        {KIND[r.kind] ?? r.kind}
                        <span className="block text-meta text-muted">{r.actor}</span>
                      </td>
                      <td className="hidden py-2 pr-3 text-meta md:table-cell">
                        <What r={r} />
                        {(r.citations ?? []).length > 0 && <span className="block text-muted">{(r.citations ?? []).length} cited facts · {r.payload_hash.slice(0, 12)}</span>}
                      </td>
                      <td className="py-2">{chip && <StatusBadge shown={chip} title={chip.title ?? r.text} />}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
