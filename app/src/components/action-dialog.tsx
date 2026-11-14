"use client";

import { useEffect, useRef, useState } from "react";
import { confirmAction, previewAction } from "@/lib/api";
import { confirmBlocked, confirmLabel, EDITABLE, edits, hasPreset, newIdempotencyKey, outcomeUnknown, sameEdits, splitAddresses, withPreset, type Preset } from "@/lib/actions";
import { plainError } from "@/lib/errors";
import type { ActionKind, ActionPayload, ActionPreview, ActionResult, CardActionOffer, ProactiveSuggestion } from "@/lib/types";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/cn";
import { Button } from "./ui/button";
import { StatusGlyph, SystemCode } from "./ui/glyphs";
import { Sheet } from "./ui/sheet";

const bare = "h-10 w-full min-w-0 bg-transparent text-[13px] text-fg placeholder:text-faint focus:outline-none dark:[color-scheme:dark]";

/** One line of the compose sheet: its label on the left, the field filling the rest, as a mail app sets To and Subject. */
function Line({ id, label, children }: { id?: string; label: string; children: React.ReactNode }) {
  return (
    <div className="flex min-h-10 items-center gap-3 border-b border-border px-4 focus-within:bg-[color-mix(in_srgb,var(--accent)_4%,transparent)]">
      {id ? (
        <label htmlFor={id} className="w-[76px] shrink-0 text-[13px] text-muted">
          {label}
        </label>
      ) : (
        <span className="w-[76px] shrink-0 text-[13px] text-muted">{label}</span>
      )}
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

/** The fields one action takes, bound to the form, as one sheet. */
function Fields({ kind, form, set, preview }: { kind: ActionKind; form: ActionPayload; set: (p: ActionPayload) => void; preview: ActionPreview }) {
  const text = (key: keyof ActionPayload) => (typeof form[key] === "string" ? (form[key] as string) : "");
  const list = (key: "to" | "cc") => (form[key] ?? []).join(", ");
  const has = (key: keyof ActionPayload) => EDITABLE[kind].includes(key);
  return (
    <div className="space-y-2.5">
      <div className="overflow-hidden rounded-xl bg-bg shadow-lift [&>*:last-child]:border-b-0">
        {preview.outbound && (
          <Line label="From">
            <span className="block truncate py-2.5 text-[13px]" data-testid="action-from">
              {preview.sender ?? "your mailbox"}
            </span>
          </Line>
        )}
        {has("to") && (
          <Line id="action-to" label="To">
            <input id="action-to" className={bare} value={list("to")} onChange={(e) => set({ ...form, to: splitAddresses(e.target.value) })} data-testid="action-to" />
          </Line>
        )}
        {has("cc") && (
          <Line id="action-cc" label="Cc">
            <input id="action-cc" className={bare} value={list("cc")} onChange={(e) => set({ ...form, cc: splitAddresses(e.target.value) })} />
          </Line>
        )}
        {has("subject") && (
          <Line id="action-subject" label="Subject">
            <input id="action-subject" className={`${bare} font-medium`} value={text("subject")} onChange={(e) => set({ ...form, subject: e.target.value })} />
          </Line>
        )}
        {has("chat_id") && (
          <Line id="action-chat" label="Teams chat">
            <select id="action-chat" className={`${bare} cursor-pointer`} value={text("chat_id")} onChange={(e) => set({ ...form, chat_id: e.target.value })}>
              {(preview.teams_chats ?? []).length === 0 && <option value="">No chat is set up for actions</option>}
              {(preview.teams_chats ?? []).map((c) => (
                <option key={c.chat_id} value={c.chat_id}>
                  {c.name}
                </option>
              ))}
            </select>
          </Line>
        )}
        {has("due") && (
          <Line id="action-due" label="Due">
            <input id="action-due" type="date" className={bare} value={text("due")} onChange={(e) => set({ ...form, due: e.target.value })} />
          </Line>
        )}
        {has("until") && (
          <Line id="action-until" label="Snooze until">
            <input id="action-until" type="date" className={bare} value={text("until")} onChange={(e) => set({ ...form, until: e.target.value })} />
          </Line>
        )}
        {has("reason") && (
          <Line id="action-reason" label="Why dismiss it">
            <input id="action-reason" className={bare} value={text("reason")} onChange={(e) => set({ ...form, reason: e.target.value })} data-testid="action-reason" />
          </Line>
        )}
        {has("assignee") && (
          <Line id="action-assignee" label="Assign to">
            <input id="action-assignee" type="email" placeholder="colleague@company" className={bare} value={text("assignee")} onChange={(e) => set({ ...form, assignee: e.target.value })} data-testid="action-assignee" />
          </Line>
        )}
        {has("note") && (
          <Line id="action-note" label="Note">
            <input id="action-note" className={bare} value={text("note")} onChange={(e) => set({ ...form, note: e.target.value })} />
          </Line>
        )}
        {has("body") && (
          <div className="border-b border-border">
            <label htmlFor="action-body" className="sr-only">
              Message
            </label>
            <textarea
              id="action-body"
              rows={8}
              className="block min-h-[168px] w-full resize-y bg-transparent px-4 pt-3.5 pb-4 font-sans text-[13px] leading-[21px] text-fg focus:outline-none"
              value={text("body")}
              onChange={(e) => set({ ...form, body: e.target.value })}
              data-testid="action-body"
            />
          </div>
        )}
      </div>
      {preview.payload.in_reply_to && <p className="px-1 text-[12px] leading-[18px] text-muted">In reply to the cited message; the thread keeps its own subject.</p>}
      {(preview.payload.thread_recipients ?? []).length > 0 && (
        <p className="px-1 text-[12px] leading-[18px]" data-testid="action-thread-recipients">
          <span className="text-muted">The reply also reaches the message's Reply-To or sender: </span>
          {(preview.payload.thread_recipients ?? []).join(", ")}
        </p>
      )}
    </div>
  );
}

function Heading({ children }: { children: React.ReactNode }) {
  return <h3 className="mb-2 text-[12px] leading-4 font-medium text-muted">{children}</h3>;
}

/** What the preview found: cited facts kept or dropped, blocking problems, what a live send lacks. */
function Checks({ preview }: { preview: ActionPreview }) {
  const [request, setRequest] = useState(false);
  const cites = preview.citations ?? [];
  return (
    <div className="space-y-6 text-[13px] leading-5">
      {(preview.problems ?? []).length > 0 && (
        <ul className="space-y-1.5" data-testid="action-problems">
          {(preview.problems ?? []).map((p, n) => (
            <li key={n} className="flex gap-2.5">
              <StatusGlyph status="late" size={14} className="mt-[3px]" />
              {p}
            </li>
          ))}
        </ul>
      )}
      {preview.outbound && (preview.missing ?? []).length > 0 && (
        <div className="flex gap-2.5" data-testid="action-missing">
          <StatusGlyph status="suspect" size={14} className="mt-[3px]" />
          <div>
            <p className="font-medium">Sending is off for you. Confirming logs a dry run and sends nothing. Missing:</p>
            <ul className="mt-0.5 text-muted">
              {(preview.missing ?? []).map((m, n) => (
                <li key={n}>{m}</li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {cites.length > 0 && (
        <section>
          <Heading>Figures used</Heading>
          <dl className="space-y-px">
            {cites.map((c, n) => (
              <div key={n} className="grid grid-cols-[minmax(0,140px)_minmax(0,1fr)] items-baseline gap-3 py-[3px]" title={c.handle}>
                <dt className="truncate text-[12px] text-muted first-letter:uppercase">{c.fact.replace(/_/g, " ")}</dt>
                <dd className={c.present ? "min-w-0 break-words tabular-nums" : "min-w-0 break-words text-muted"}>
                  <span className={c.present ? "" : "line-through"}>{c.value}</span>
                  {!c.present && <span className="ml-1.5 text-[12px]">(no longer in the text)</span>}
                </dd>
              </div>
            ))}
          </dl>
        </section>
      )}
      {preview.request && (
        <section>
          <button
            type="button"
            onClick={() => setRequest(!request)}
            aria-expanded={request}
            className="-ml-1 inline-flex h-7 items-center gap-1 rounded-md px-1 text-[12px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
          >
            <ChevronRight className={cn("size-3.5 transition-transform", request && "rotate-90")} aria-hidden />
            Exact request
          </button>
          {request && (
            <pre className="mt-1.5 max-h-64 overflow-auto rounded-lg bg-[color-mix(in_srgb,var(--fg)_5%,transparent)] p-3 font-mono text-[11px] leading-4" data-testid="action-request">
              {`${preview.request.method} ${preview.request.url}\n${JSON.stringify(preview.request.body, null, 2)}`}
            </pre>
          )}
        </section>
      )}
    </div>
  );
}

/** A tick the person must give before the confirm: on a lifted sheet, its glyph, the words in ink. */
function Attest({ children, ...props }: React.InputHTMLAttributes<HTMLInputElement> & { children: React.ReactNode }) {
  return (
    <label className="flex cursor-pointer items-start gap-3 rounded-xl bg-bg px-3.5 py-3 text-[13px] leading-5 shadow-lift">
      <input type="checkbox" className="mt-[3px] size-3.5 shrink-0 cursor-pointer accent-[var(--accent)]" {...props} />
      <span>{children}</span>
    </label>
  );
}

/** Preview, edit and confirm one action (loaded when an action is first opened; see card-actions). Nothing happens until the confirm button; the key
 * drawn when the dialog opens makes a repeat click return the first result. */
export default function ActionDialog({
  card,
  offer,
  onClose,
  onDone,
  state,
  preset,
}: {
  card: { id: string; subject: { label: string } };
  offer: CardActionOffer;
  onClose: () => void;
  onDone: (r: ActionResult) => void;
  state?: ActionResult | null;
  preset?: Preset;
}) {
  const mustCheckSent = outcomeUnknown(state);
  const [sentChecked, setSentChecked] = useState(false);
  const [preview, setPreview] = useState<ActionPreview | null>(null);
  const [form, setForm] = useState<ActionPayload>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [unknownOk, setUnknownOk] = useState(false);
  // the person's picks are being put into the preview: nothing is confirmed before it shows
  const [presetPending, setPresetPending] = useState(false);
  // why the last confirm failed: kept apart from preview errors, so the review that follows
  // a failed confirm cannot wipe it
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const ackedDomains = useRef("");
  const key = useRef(newIdempotencyKey());
  const sending = useRef(false);

  const review = (payload?: ActionPayload): Promise<ActionPreview | null> => {
    setError(null);
    return previewAction({ card_id: card.id, kind: offer.kind, payload: payload ? edits(offer.kind, payload) : undefined }).then(
      (p) => {
        // an acknowledgement belongs to the recipients (and so the domains) it was given for
        const people = [...(p.payload.to ?? []), ...(p.payload.cc ?? [])].map((a) => a.trim().toLowerCase()).sort().join(",");
        const domains = `${[...(p.unknown_domains ?? [])].sort().join(",")}|${people}`;
        if (domains !== ackedDomains.current) setUnknownOk(false);
        ackedDomains.current = domains;
        setPreview(p);
        setForm(p.payload);
        return p;
      },
      (e) => {
        setError(plainError(e));
        return null;
      },
    );
  };
  useEffect(() => {
    // what the person picked on the draft (who it is for, who else, a question for the
    // recipient) goes into the preview the desk checks, never past it
    const picked = !!preset && hasPreset(preset);
    setPresetPending(picked);
    void review().then((p) => {
      if (p && preset && picked) void review(withPreset(p.payload, preset)).finally(() => setPresetPending(false));
      else setPresetPending(false);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card.id, offer.kind]);

  const edited = !!preview && !sameEdits(offer.kind, form, preview.payload);
  const blocked = confirmBlocked(presetPending ? null : preview, { edited, unknownConfirmed: unknownOk, busy, mustCheckSent, sentChecked });
  const go = () => {
    if (!preview || blocked || sending.current) return;
    sending.current = true;
    setBusy(true);
    setConfirmError(null);
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
        setConfirmError(plainError(e));
        void review(form);
      },
    );
  };

  const channel = offer.kind === "teams_message" ? "Teams" : preview?.outbound || /send_email|reply_in_thread|forward/.test(offer.kind) ? "Email" : null;
  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={`${offer.label}: ${card.subject.label}`}
      heading={
        <>
          {offer.label}
          <span className="px-1.5 font-normal text-faint">·</span>
          <span className="font-normal text-muted">{card.subject.label}</span>
        </>
      }
      icon={channel ? <SystemCode system={channel} size={20} /> : undefined}
      bodyClassName="bg-[color-mix(in_srgb,var(--surface)_70%,var(--bg))] pt-4"
      footer={
        <div className="flex flex-wrap items-center justify-end gap-x-3 gap-y-2">
          <span className="mr-auto min-w-0 text-[12px] leading-4 text-muted">{blocked && preview ? blocked : preview?.outbound ? "Sent as you, from your mailbox." : ""}</span>
          <div className="flex gap-1.5">
            <Button variant="ghost" size="sm" onClick={onClose}>
              Cancel
            </Button>
            <Button size="sm" className="min-w-20" onClick={go} disabled={!!blocked} data-testid="action-confirm">
              {preview ? confirmLabel(preview) : "Confirm"}
            </Button>
          </div>
        </div>
      }
    >
      <div className="space-y-6" data-testid="action-dialog">
        {confirmError && (
          <p className="flex gap-2.5 text-[13px] leading-5" role="alert" data-testid="action-confirm-error">
            <StatusGlyph status="late" size={14} className="mt-[3px]" />
            <span>{confirmError} Review the draft below before you try again.</span>
          </p>
        )}
        {error && (
          <p className="flex gap-2.5 text-[13px] leading-5">
            <StatusGlyph status="late" size={14} className="mt-[3px]" />
            {error}
          </p>
        )}
        {!preview ? (
          !error && (
            <div className="space-y-2.5" aria-busy="true">
              <p className="sr-only">Loading preview</p>
              <div className="h-[300px] rounded-xl bg-bg shadow-lift" />
            </div>
          )
        ) : (
          <>
            <div className="space-y-2.5">
              <Fields kind={offer.kind} form={form} set={setForm} preview={preview} />
              {edited && (
                <Button variant="secondary" size="sm" onClick={() => void review(form)} data-testid="action-review">
                  Review changes
                </Button>
              )}
            </div>
            {preview.outbound && mustCheckSent && (
              <Attest checked={sentChecked} onChange={(e) => setSentChecked(e.target.checked)} data-testid="action-sent-checked">
                An earlier send of this card has an unknown outcome. I checked my Sent Items and it is not there.
              </Attest>
            )}
            {(preview.unknown_domains ?? []).length > 0 && (
              <Attest checked={unknownOk} onChange={(e) => setUnknownOk(e.target.checked)} data-testid="action-unknown-ok">
                Outside the known vendor, customer and company domains: {(preview.unknown_domains ?? []).join(", ")}. I checked these recipients.
              </Attest>
            )}
            <Checks preview={preview} />
          </>
        )}
      </div>
    </Sheet>
  );
}
