"use client";

import Link from "next/link";
import { useState } from "react";
import { CardActions } from "@/components/card-actions";
import { plural } from "@/lib/format";
import type { ActionResult, RdBoard, RdProject } from "@/lib/types";

const KIND: Record<string, string> = {
  sample_request: "Sample request",
  formulation_change: "Formulation change",
  stability_test: "Stability test",
  coa: "Certificate of analysis",
};
const STATUS: Record<string, string> = {
  waiting_on_us: "Waiting on us",
  waiting_on_customer: "Waiting on the customer",
  update_received: "Update received",
  closed: "Closed",
};

// R&D and sample projects per customer, built only from the threads and records you may open.
// A status update is a draft: it goes out only through the card's preview and your confirm.
export function RdBoardView({ board }: { board: RdBoard }) {
  const [states, setStates] = useState<Record<string, ActionResult>>({});
  const projects = board.projects ?? [];
  return (
    <div className="space-y-4" data-testid="rd-board">
      {projects.length === 0 ? (
        <p className="text-[13px] text-muted">No R&amp;D or sample projects in the mail you can open.</p>
      ) : (
        <p className="text-[13px] text-fg">
          {plural(board.total, "project")}, {board.idle} idle (no activity for {board.idle_after_days}+ business days).
        </p>
      )}
      <p className="text-[12px] text-muted">
        Early view, read from mail only: projects are grouped by the other company&apos;s mail domain, so some may be suppliers. No idle alerts are sent. Nothing is sent until you confirm it.
      </p>
      <ul className="space-y-3">
        {projects.map((p: RdProject) => (
          <li key={p.id} className="space-y-1 rounded-lg border border-border p-3 text-[13px]" data-testid="rd-project">
            <div>
              <span className="font-medium">{KIND[p.kind] ?? p.kind}</span>
              {p.idle && <span className="ml-2 rounded bg-hover px-1 text-[11px]" data-testid="rd-idle">Idle</span>}
            </div>
            <div className="text-muted">
              {STATUS[p.status] ?? p.status} · last activity {p.last_on ?? "unknown"} ({plural(p.days, "business day")} ago)
              {p.waiting_on ? ` · waiting on ${p.waiting_on === "us" ? "us" : "the customer"}` : ""}
              {p.owner_is_you ? " · you own it" : p.owner ? " · owner on record" : " · no owner on record"}
            </div>
            {p.customer ? (
              <Link className="font-medium hover:underline" href={`/object/${encodeURIComponent(p.customer.split(":")[0])}/${encodeURIComponent(p.customer)}`}>
                Open the customer
              </Link>
            ) : (
              <span className="text-muted">Party {p.party ?? "not identified"} (customer not linked)</span>
            )}
            <ul className="list-disc pl-5 text-muted">
              {(p.lines ?? []).map((l) => (
                <li key={l.id}>{l.subject || "(no subject)"} · {STATUS[l.state] ?? l.state} · {plural(l.messages_n, "message")}</li>
              ))}
            </ul>
            {p.draft && <pre className="whitespace-pre-wrap rounded bg-hover p-2 text-[12px]">{p.draft.body}</pre>}
            {p.card_id && (
              <CardActions
                card={{ id: p.card_id, subject: { label: p.draft?.subject ?? "Project status" }, actions: p.offers }}
                state={states[p.card_id] ?? null}
                onDone={(r) => setStates((s) => ({ ...s, [p.card_id as string]: r }))}
              />
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
