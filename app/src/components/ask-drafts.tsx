"use client";

// What an Ask drafted because the question asked for it: an email, edited and sent from its
// preview with your explicit confirm (the approve-to-send desk, /api/actions/*), or an
// action (a payment, a record change, an approval) shown in full, which you keep as a
// follow-up or assign to the colleague who approves it. Nothing here sends on its own.

import { useState } from "react";
import { CardActions } from "./card-actions";
import type { ActionResult, Answer, AskDraft } from "@/lib/types";

const KIND: Record<AskDraft["kind"], string> = {
  email: "Draft email",
  payment: "Draft payment",
  record_change: "Draft record change",
  approval: "Draft approval",
};

function Draft({ d }: { d: AskDraft }) {
  const [done, setDone] = useState<ActionResult | null>(null);
  return (
    <li className="space-y-2.5 rounded-xl bg-bg px-4 py-3.5 shadow-lift" data-testid="ask-draft">
      <p className="text-[12px] leading-4 font-medium text-muted">{KIND[d.kind]}</p>
      <p className="text-[13px] leading-5 font-medium">{d.email_subject ?? d.title}</p>
      {(d.details ?? []).length > 0 ? (
        <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-1 text-[13px] leading-5" data-testid="ask-draft-details">
          {(d.details ?? []).map((x, i) => (
            <div key={i} className="contents">
              <dt className="text-muted">{x.label}</dt>
              <dd className="break-words">{x.value}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="text-[13px] leading-[21px] whitespace-pre-wrap break-words">{d.body}</p>
      )}
      {(d.missing ?? []).map((m, i) => (
        <p key={i} className="text-[12px] leading-4 text-muted">
          {m}
        </p>
      ))}
      <CardActions card={{ id: d.id, subject: { label: d.title }, actions: d.actions }} onDone={setDone} state={done} />
      {done && <p className="text-[12px] leading-4 text-muted">{done.text}</p>}
    </li>
  );
}

export function AskDrafts({ drafts }: { drafts: AskDraft[] }) {
  if (!drafts.length) return null;
  return (
    <section data-testid="ask-drafts">
      <h3 className="mb-2 text-[12px] leading-4 font-medium text-muted">Drafts</h3>
      <ul className="space-y-3">
        {drafts.map((d) => (
          <Draft key={d.id} d={d} />
        ))}
      </ul>
    </section>
  );
}

/** The question's attachments as the answer read them: each by name, and why one was not read. */
export function AttachmentNotes({ items }: { items: NonNullable<Answer["attachments"]> }) {
  if (items.length === 0) return null;
  const read = items.filter((a) => a.read);
  const unread = items.filter((a) => !a.read);
  return (
    <section data-testid="attachment-notes" className="text-[13px] leading-5 text-muted">
      {read.length > 0 && <p>Read from your attachment{read.length > 1 ? "s" : ""}: {read.map((a) => a.name + (a.note ? ` (${a.note.replace(/\.$/, "").toLowerCase()})` : "")).join(", ")}.</p>}
      {unread.map((a) => (
        <p key={a.digest + a.name}>
          Not read: {a.name}. {a.note}
        </p>
      ))}
    </section>
  );
}
