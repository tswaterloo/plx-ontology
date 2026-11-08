"use client";

import Link from "next/link";
import { useState } from "react";
import { CardActions } from "@/components/card-actions";
import { plural } from "@/lib/format";
import type { ActionResult, ReplyLine, ReplyTriage } from "@/lib/types";

// What needs my reply today: your own mail and one-to-one chats where someone asked you
// something you have not answered, heaviest first. A reply is a draft: it goes out only
// through the card's preview and your confirm.
export function ReplyList({ triage }: { triage: ReplyTriage }) {
  const [states, setStates] = useState<Record<string, ActionResult>>({});
  const lines = triage.lines ?? [];
  return (
    <div className="space-y-4" data-testid="reply-triage">
      <h1 className="text-lg font-semibold">My replies</h1>
      <p className="text-[13px] text-muted">
        {plural(triage.total, "message")} waiting on your reply as of {triage.as_of}. Only your own mailbox. Nothing is sent until you confirm it.
      </p>
      {lines.length === 0 && <p className="text-[13px] text-muted">Nothing is waiting on your reply.</p>}
      <ul className="space-y-3">
        {lines.map((line: ReplyLine) => (
          <li key={line.id} className="space-y-1 rounded-lg border border-border p-3 text-[13px]" data-testid="reply-line">
            <div>
              <span className="font-medium">{line.subject || "(no subject)"}</span>
              {line.new_inquiry && <span className="ml-2 rounded bg-hover px-1 text-[11px]">New inquiry</span>}
            </div>
            <div className="text-muted">
              {line.channel === "teams" ? "Teams" : "Mail"} · {plural(line.days, "business day")} waiting
              {line.chases ? ` · asked ${line.chases + 1} times` : ""}
              {line.customer_tier && line.customer_tier !== "none" ? ` · tier ${line.customer_tier} customer` : ""}
              {Object.entries(line.records_named ?? {}).map(([kind, n]) => ` · ${n} ${kind}`).join("")}
            </div>
            {line.object_id && (
              <Link className="font-medium hover:underline" href={`/object/${encodeURIComponent(line.object_id.split(":")[0])}/${encodeURIComponent(line.object_id)}`}>
                Open the message
              </Link>
            )}
            <pre className="whitespace-pre-wrap rounded bg-hover p-2 text-[12px]">{line.draft.body}</pre>
            {line.card_id && (
              <CardActions
                card={{ id: line.card_id, subject: { label: line.subject ?? "Needs your reply" }, actions: line.offers }}
                state={states[line.card_id] ?? null}
                onDone={(r) => setStates((s) => ({ ...s, [line.card_id as string]: r }))}
              />
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
