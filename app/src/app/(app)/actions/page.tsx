"use client";

import { useCallback, useEffect, useState } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { LoadProblem } from "@/components/ui/notice";
import { PageHeader } from "@/components/ui/page-header";
import { SkeletonRows } from "@/components/ui/skeleton";
import { StatusWord } from "@/components/ui/status-badge";
import { actionChip } from "@/lib/actions";
import { getActionsLog } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { formatDateTime, plural } from "@/lib/format";
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
    <div className="mx-auto w-full max-w-[1200px] px-4 pt-6 pb-16 sm:px-8 sm:pt-8">
      <PageHeader
        className="mb-8"
        eyebrow="Actions"
        title="What you did from the Workbench"
        sub="Every action you confirmed on a suggestion: when, to whom, the facts it cited and what happened. The log can't be edited, and the text of a message isn't kept."
      />
      <Card>
        <CardHeader>
          <CardTitle>Your actions</CardTitle>
          {rows && rows.length > 0 && <span className="tabular-nums">{rows.length}</span>}
        </CardHeader>
        <CardBody>
          {error ? (
            <LoadProblem message={error} onRetry={load} />
          ) : !rows ? (
            <SkeletonRows rows={4} label="Reading your actions" />
          ) : rows.length === 0 ? (
            <p className="py-2 text-row text-muted">
              Nothing yet. Actions you confirm from <span className="text-fg">What to do next</span> on Today are listed here, newest first.
            </p>
          ) : (
            <table className="w-full text-row" data-testid="actions-log">
              <thead>
                <tr className="h-8 border-y border-border-strong bg-bg-subtle text-left text-eyebrow uppercase text-muted">
                  <th className="px-2 pl-3 font-semibold">When</th>
                  <th className="px-2 font-semibold">Record</th>
                  <th className="px-2 font-semibold">Action</th>
                  <th className="hidden px-2 font-semibold md:table-cell">What</th>
                  <th className="px-2 font-semibold">Result</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {rows.map((r) => {
                  const chip = actionChip(r, false);
                  return (
                    <tr key={r.seq} className="align-top" data-testid="actions-log-row">
                      <td className="whitespace-nowrap px-2 py-2.5 pl-3 tabular-nums text-meta text-muted">{formatDateTime(r.at)}</td>
                      <td className="px-2 py-2.5 font-medium">{r.card_label || "A suggestion"}</td>
                      <td className="px-2 py-2.5">
                        {KIND[r.kind] ?? r.kind}
                        <span className="block text-meta text-muted">{r.actor}</span>
                      </td>
                      <td className="hidden px-2 py-2.5 text-meta md:table-cell">
                        <What r={r} />
                        {(r.citations ?? []).length > 0 && (
                          <span className="block text-faint" title={`Fingerprint ${r.payload_hash.slice(0, 12)}`}>
                            {plural((r.citations ?? []).length, "cited fact")}
                          </span>
                        )}
                      </td>
                      <td className="px-2 py-2.5">{chip && <StatusWord shown={chip} title={chip.title ?? r.text} />}</td>
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
