"use client";

// What an Ask drafted because the question asked for it: an email, edited and sent from its
// preview with your explicit confirm (the approve-to-send desk, /api/actions/*), or an
// action (a payment, a record change, an approval) shown in full, which you keep as a
// follow-up or assign to the colleague who approves it. Nothing here sends on its own.

import { useState } from "react";
import { CardActions } from "./card-actions";
import { SaveAsTemplate, UseTemplate } from "./templates";
import { Button } from "./ui/button";
import type { Preset } from "@/lib/actions";
import type { ActionResult, Answer, AskDraft } from "@/lib/types";

type Picks = { one: Record<number, string>; any: Record<string, boolean>; ask: Record<string, boolean> };

/** The draft's open questions as one tap each: who a recipient is (pick one), who else
 * goes on it (any), and a record the draft could not state (ask the recipient in the
 * message instead). The picks go into the send preview, where the desk checks them. */
function Questions({ d, picks, setPicks }: { d: AskDraft; picks: Picks; setPicks: (p: Picks) => void }) {
  const rq = d.recipient_questions ?? [];
  const fq = d.record_questions ?? [];
  if (!rq.length && !fq.length) return null;
  const key = (field: string, a: string) => `${field}:${a}`;
  return (
    <div className="space-y-2" data-testid="draft-questions">
      {rq.map((q, i) => (
        <div key={i} className="space-y-1">
          <p className="text-[12px] leading-4 text-muted">{q.label}</p>
          <div className="flex flex-wrap gap-1.5">
            {(q.candidates ?? []).map((a) => {
              const on = q.pick === "one" ? picks.one[i] === a : !!picks.any[key(q.field, a)];
              return (
                <Button
                  key={a}
                  size="sm"
                  variant={on ? "secondary" : "outline"}
                  className="h-7 rounded-md px-2 text-[12px] font-normal"
                  aria-pressed={on}
                  data-testid="draft-pick"
                  onClick={() =>
                    q.pick === "one"
                      ? setPicks({ ...picks, one: { ...picks.one, [i]: on ? "" : a } })
                      : setPicks({ ...picks, any: { ...picks.any, [key(q.field, a)]: !on } })
                  }
                >
                  {a}
                </Button>
              );
            })}
          </div>
        </div>
      ))}
      {fq.map((q) => {
        const on = !!picks.ask[q.record];
        return (
          <div key={q.record} className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <p className="min-w-0 text-[12px] leading-4 text-muted">{q.note}</p>
            <Button
              size="sm"
              variant={on ? "secondary" : "outline"}
              className="h-7 rounded-md px-2 text-[12px] font-normal"
              aria-pressed={on}
              data-testid="draft-ask-recipient"
              onClick={() => setPicks({ ...picks, ask: { ...picks.ask, [q.record]: !on } })}
            >
              {on ? "Asked in the email" : "Ask them in the email"}
            </Button>
          </div>
        );
      })}
    </div>
  );
}

/** The picks as the send preview's preset. */
export function presetOf(d: AskDraft, picks: Picks): Preset {
  const rq = d.recipient_questions ?? [];
  const to: string[] = [];
  const cc: string[] = [];
  rq.forEach((q, i) => {
    if (q.pick === "one" && picks.one[i]) (q.field === "cc" ? cc : to).push(picks.one[i]);
  });
  for (const [k, on] of Object.entries(picks.any)) {
    if (!on) continue;
    const [field, ...rest] = k.split(":");
    (field === "cc" ? cc : to).push(rest.join(":"));
  }
  const append = (d.record_questions ?? []).filter((q) => picks.ask[q.record]).map((q) => q.ask_line);
  return { to, cc, append };
}

const KIND: Record<AskDraft["kind"], string> = {
  email: "Draft email",
  payment: "Draft payment",
  record_change: "Draft record change",
  approval: "Draft approval",
};

// the template types an email draft may start from
const EMAIL_TEMPLATES = ["customer_email", "supplier_email", "order_confirmation", "po_note", "phrasing"] as const;

function Draft({ d, record }: { d: AskDraft; record?: string | null }) {
  const [done, setDone] = useState<ActionResult | null>(null);
  const [picks, setPicks] = useState<Picks>({ one: {}, any: {}, ask: {} });
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
      {(d.to || (d.also_to ?? []).length > 0 || (d.cc ?? []).length > 0) && (
        <p className="text-[12px] leading-4 text-muted" data-testid="draft-recipients">
          To {[d.to, ...(d.also_to ?? [])].filter(Boolean).join(", ")}
          {(d.cc ?? []).length > 0 && `; Cc ${(d.cc ?? []).join(", ")}`}
        </p>
      )}
      <Questions d={d} picks={picks} setPicks={setPicks} />
      {d.kind === "email" && (
        <div className="space-y-1.5" data-testid="draft-templates">
          <UseTemplate docTypes={[...EMAIL_TEMPLATES]} record={record} />
          <SaveAsTemplate text={d.body} record={record} docType="customer_email" />
        </div>
      )}
      <CardActions card={{ id: d.id, subject: { label: d.title }, actions: d.actions }} onDone={setDone} state={done} preset={presetOf(d, picks)} />
      {done && <p className="text-[12px] leading-4 text-muted">{done.text}</p>}
    </li>
  );
}

export function AskDrafts({ drafts, record }: { drafts: AskDraft[]; record?: string | null }) {
  if (!drafts.length) return null;
  return (
    <section data-testid="ask-drafts">
      <h3 className="mb-2 text-[12px] leading-4 font-medium text-muted">Drafts</h3>
      <ul className="space-y-3">
        {drafts.map((d) => (
          <Draft key={d.id} d={d} record={record} />
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
