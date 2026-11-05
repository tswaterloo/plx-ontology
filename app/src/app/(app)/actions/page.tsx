"use client";

import { useCallback, useEffect, useState } from "react";
import { Card, CardBody } from "@/components/ui/card";
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
          To {(r.to ?? []).join(", ") || "nobody"}
          {(r.cc ?? []).length > 0 && `; cc ${(r.cc ?? []).join(", ")}`}
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
        className="mb-4"
        title="Action log"
        sub="Emails, Teams messages and tasks you confirmed, newest first. Entries can't be edited; message bodies aren't stored."
      />
      <Card className="border-t-0">
        <CardBody>
          {error ? (
            <LoadProblem message={error} onRetry={load} />
          ) : !rows ? (
            <SkeletonRows rows={4} label="Loading the log" />
          ) : rows.length === 0 ? (
            <p className="py-2 text-row text-muted">No actions yet. Confirm one from Follow-ups on Today.</p>
          ) : (
            <table className="w-full text-row" data-testid="actions-log">
              <thead>
                <tr className="h-8 border-y border-border-strong bg-bg-subtle text-left text-meta text-muted">
                  <th className="px-2 pl-3 font-medium">When</th>
                  <th className="px-2 font-medium">Record</th>
                  <th className="px-2 font-medium">Action</th>
                  <th className="hidden px-2 font-medium md:table-cell">Details</th>
                  <th className="px-2 font-medium">Result</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {rows.map((r) => {
                  const chip = actionChip(r, false);
                  return (
                    <tr key={r.seq} className="align-top" data-testid="actions-log-row">
                      <td className="whitespace-nowrap px-2 py-2.5 pl-3 tabular-nums text-meta text-muted">{formatDateTime(r.at)}</td>
                      <td className="px-2 py-2.5">{r.card_label || "Follow-up"}</td>
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
