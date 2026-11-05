"use client";

import { useEffect, useRef, useState } from "react";
import { confirmAction, previewAction } from "@/lib/api";
import { confirmBlocked, confirmLabel, EDITABLE, edits, newIdempotencyKey, outcomeUnknown, sameEdits, splitAddresses } from "@/lib/actions";
import { plainError } from "@/lib/errors";
import type { ActionKind, ActionPayload, ActionPreview, ActionResult, CardActionOffer, ProactiveSuggestion } from "@/lib/types";
import { Button } from "./ui/button";
import { Sheet } from "./ui/sheet";

const field = "w-full rounded-md border border-border bg-surface px-2.5 py-1.5 text-row focus:border-control focus:outline-none";

function Label({ children, htmlFor }: { children: React.ReactNode; htmlFor: string }) {
  return (
    <label htmlFor={htmlFor} className="mb-1 block text-meta font-medium text-muted">
      {children}
    </label>
  );
}

/** The fields one action takes, bound to the form. */
function Fields({ kind, form, set, preview }: { kind: ActionKind; form: ActionPayload; set: (p: ActionPayload) => void; preview: ActionPreview }) {
  const text = (key: keyof ActionPayload) => (typeof form[key] === "string" ? (form[key] as string) : "");
  const list = (key: "to" | "cc") => (form[key] ?? []).join(", ");
  const has = (key: keyof ActionPayload) => EDITABLE[kind].includes(key);
  return (
    <div className="space-y-3">
      {preview.outbound && (
        <p className="text-meta">
          <span className="text-muted">From </span>
          <span data-testid="action-from">{preview.sender ?? "your mailbox"}</span>
        </p>
      )}
      {has("to") && (
        <div>
          <Label htmlFor="action-to">To</Label>
          <input id="action-to" className={field} value={list("to")} onChange={(e) => set({ ...form, to: splitAddresses(e.target.value) })} data-testid="action-to" />
        </div>
      )}
      {has("cc") && (
        <div>
          <Label htmlFor="action-cc">Cc</Label>
          <input id="action-cc" className={field} value={list("cc")} onChange={(e) => set({ ...form, cc: splitAddresses(e.target.value) })} />
        </div>
      )}
      {has("subject") && (
        <div>
          <Label htmlFor="action-subject">Subject</Label>
          <input id="action-subject" className={field} value={text("subject")} onChange={(e) => set({ ...form, subject: e.target.value })} />
        </div>
      )}
      {preview.payload.in_reply_to && <p className="text-meta text-muted">In reply to the cited message; the thread keeps its own subject.</p>}
      {(preview.payload.thread_recipients ?? []).length > 0 && (
        <p className="text-meta" data-testid="action-thread-recipients">
          <span className="text-muted">The reply also reaches the message's Reply-To or sender: </span>
          {(preview.payload.thread_recipients ?? []).join(", ")}
        </p>
      )}
      {has("chat_id") && (
        <div>
          <Label htmlFor="action-chat">Teams chat</Label>
          <select id="action-chat" className={field} value={text("chat_id")} onChange={(e) => set({ ...form, chat_id: e.target.value })}>
            {(preview.teams_chats ?? []).length === 0 && <option value="">No chat is set up for actions</option>}
            {(preview.teams_chats ?? []).map((c) => (
              <option key={c.chat_id} value={c.chat_id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>
      )}
      {has("body") && (
        <div>
          <Label htmlFor="action-body">Message</Label>
          <textarea id="action-body" rows={12} className={`${field} font-sans`} value={text("body")} onChange={(e) => set({ ...form, body: e.target.value })} data-testid="action-body" />
        </div>
      )}
      {has("due") && (
        <div>
          <Label htmlFor="action-due">Due</Label>
          <input id="action-due" type="date" className={field} value={text("due")} onChange={(e) => set({ ...form, due: e.target.value })} />
        </div>
      )}
      {has("until") && (
        <div>
          <Label htmlFor="action-until">Snooze until</Label>
          <input id="action-until" type="date" className={field} value={text("until")} onChange={(e) => set({ ...form, until: e.target.value })} />
        </div>
      )}
      {has("reason") && (
        <div>
          <Label htmlFor="action-reason">Why dismiss it</Label>
          <input id="action-reason" className={field} value={text("reason")} onChange={(e) => set({ ...form, reason: e.target.value })} data-testid="action-reason" />
        </div>
      )}
      {has("note") && (
        <div>
          <Label htmlFor="action-note">Note</Label>
          <input id="action-note" className={field} value={text("note")} onChange={(e) => set({ ...form, note: e.target.value })} />
        </div>
      )}
    </div>
  );
}

/** What the preview found: cited facts kept or dropped, blocking problems, what a live send lacks. */
function Checks({ preview }: { preview: ActionPreview }) {
  const [request, setRequest] = useState(false);
  const cites = preview.citations ?? [];
  return (
    <div className="space-y-3 text-meta">
      {(preview.problems ?? []).length > 0 && (
        <ul className="space-y-1 rounded-md border border-late-line bg-late-soft p-2.5 text-late" data-testid="action-problems">
          {(preview.problems ?? []).map((p, n) => (
            <li key={n}>{p}</li>
          ))}
        </ul>
      )}
      {preview.outbound && (preview.missing ?? []).length > 0 && (
        <div className="rounded-md border border-suspect-line bg-suspect-soft p-2.5 text-suspect" data-testid="action-missing">
          <p className="font-medium">Sending is off for you. Confirming logs a dry run and sends nothing. Missing:</p>
          <ul className="list-disc pl-5">
            {(preview.missing ?? []).map((m, n) => (
              <li key={n}>{m}</li>
            ))}
          </ul>
        </div>
      )}
      {cites.length > 0 && (
        <div>
          <p className="font-medium">Figures used</p>
          <ul className="mt-1 space-y-0.5">
            {cites.map((c, n) => (
              <li key={n} className={c.present ? "" : "text-muted line-through"} title={c.handle}>
                {c.fact.replace(/_/g, " ")}: {c.value}
                {!c.present && <span className="ml-1 no-underline">(no longer in the text)</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {preview.request && (
        <div>
          <button type="button" onClick={() => setRequest(!request)} aria-expanded={request} className="inline-flex items-center gap-1 font-medium text-accent hover:underline cursor-pointer">
            <span aria-hidden>{request ? "▾" : "▸"}</span>
            Exact request
          </button>
          {request && (
            <pre className="mt-1 max-h-64 overflow-auto rounded-md border border-border bg-surface-2 p-2 text-label" data-testid="action-request">
              {`${preview.request.method} ${preview.request.url}\n${JSON.stringify(preview.request.body, null, 2)}`}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

/** Preview, edit and confirm one action. Nothing happens until the confirm button; the key
 * drawn when the dialog opens makes a repeat click return the first result. */
export function ActionDialog({
  card,
  offer,
  onClose,
  onDone,
  state,
}: {
  card: ProactiveSuggestion;
  offer: CardActionOffer;
  onClose: () => void;
  onDone: (r: ActionResult) => void;
  state?: ActionResult | null;
}) {
  const mustCheckSent = outcomeUnknown(state);
  const [sentChecked, setSentChecked] = useState(false);
  const [preview, setPreview] = useState<ActionPreview | null>(null);
  const [form, setForm] = useState<ActionPayload>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [unknownOk, setUnknownOk] = useState(false);
  const key = useRef(newIdempotencyKey());
  const sending = useRef(false);

  const review = (payload?: ActionPayload) => {
    setError(null);
    return previewAction({ card_id: card.id, kind: offer.kind, payload: payload ? edits(offer.kind, payload) : undefined }).then(
      (p) => {
        setPreview(p);
        setForm(p.payload);
      },
      (e) => setError(plainError(e)),
    );
  };
  useEffect(() => {
    void review();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card.id, offer.kind]);

  const edited = !!preview && !sameEdits(offer.kind, form, preview.payload);
  const blocked = confirmBlocked(preview, { edited, unknownConfirmed: unknownOk, busy, mustCheckSent, sentChecked });
  const go = () => {
    if (!preview || blocked || sending.current) return;
    sending.current = true;
    setBusy(true);
    confirmAction({
      card_id: card.id,
      kind: offer.kind,
      payload: edits(offer.kind, preview.payload),
      confirm: true,
      idempotency_key: key.current,
      payload_hash: preview.payload_hash,
      confirm_unknown_domains: unknownOk,
      checked_sent_items: mustCheckSent && sentChecked,
    }).then(
      (r) => onDone(r),
      (e) => {
        sending.current = false;
        setBusy(false);
        setError(plainError(e));
        void review(form);
      },
    );
  };

  return (
    <Sheet open onOpenChange={(o) => !o && onClose()} title={`${offer.label}: ${card.subject.label}`}>
      <div className="flex-1 space-y-4 overflow-y-auto px-5 py-4" data-testid="action-dialog">
        {error && <p className="rounded-md border border-late-line bg-late-soft p-2.5 text-meta text-late">{error}</p>}
        {!preview ? (
          !error && <p className="text-row text-muted">Loading preview…</p>
        ) : (
          <>
            <Fields kind={offer.kind} form={form} set={setForm} preview={preview} />
            {edited && (
              <Button variant="outline" size="sm" onClick={() => void review(form)} data-testid="action-review">
                Review changes
              </Button>
            )}
            <Checks preview={preview} />
            {preview.outbound && mustCheckSent && (
              <label className="flex items-start gap-2 rounded-md border border-risk-line bg-risk-soft p-2.5 text-meta text-risk">
                <input type="checkbox" checked={sentChecked} onChange={(e) => setSentChecked(e.target.checked)} className="mt-0.5" data-testid="action-sent-checked" />
                <span>An earlier send of this card has an unknown outcome. I checked my Sent Items and it is not there.</span>
              </label>
            )}
            {(preview.unknown_domains ?? []).length > 0 && (
              <label className="flex items-start gap-2 rounded-md border border-risk-line bg-risk-soft p-2.5 text-meta text-risk">
                <input type="checkbox" checked={unknownOk} onChange={(e) => setUnknownOk(e.target.checked)} className="mt-0.5" data-testid="action-unknown-ok" />
                <span>
                  Outside the known vendor, customer and company domains: {(preview.unknown_domains ?? []).join(", ")}. I checked these recipients.
                </span>
              </label>
            )}
          </>
        )}
      </div>
      <div className="flex items-center justify-between gap-3 border-t border-border px-5 py-3">
        <span className="text-meta text-muted">{blocked && preview ? blocked : preview?.outbound ? "Sent as you, from your mailbox." : ""}</span>
        <div className="flex gap-2">
          <Button variant="ghost" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button size="sm" onClick={go} disabled={!!blocked} data-testid="action-confirm">
            {preview ? confirmLabel(preview) : "Confirm"}
          </Button>
        </div>
      </div>
    </Sheet>
  );
}

/** The card's action buttons; one opens its preview dialog. */
export function CardActions({ card, onDone, state }: { card: ProactiveSuggestion; onDone: (r: ActionResult) => void; state?: ActionResult | null }) {
  const [open, setOpen] = useState<CardActionOffer | null>(null);
  const offers = card.actions ?? [];
  if (!offers.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-1 gap-y-1.5" data-testid="card-actions">
      {offers.map((o) => (
        <Button
          key={o.kind}
          variant={o.kind === "send_email" ? "outline" : "ghost"}
          size="sm"
          className={o.kind === "send_email" ? "h-7 px-2.5 text-meta" : "h-7 px-2 text-meta font-normal text-muted hover:text-fg"}
          onClick={() => setOpen(o)}
          data-testid={`action-${o.kind}`}
        >
          {o.label}
        </Button>
      ))}
      {open && (
        <ActionDialog
          card={card}
          offer={open}
          state={state}
          onClose={() => setOpen(null)}
          onDone={(r) => {
            setOpen(null);
            onDone(r);
          }}
        />
      )}
    </div>
  );
}
