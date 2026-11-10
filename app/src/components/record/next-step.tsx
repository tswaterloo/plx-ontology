"use client";

// The next step on a record: the instruction, the drafted message as a lifted mail sheet and the
// card's actions, as Today's detail pane draws them. Each action opens its preview; nothing
// happens before its confirm.

import * as Menu from "@radix-ui/react-dropdown-menu";
import { Check, Copy, MoreHorizontal } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/cn";
import { draftText } from "@/components/proactive-suggestions";
import type { ActionResult, CardActionOffer, ProactiveSuggestion, SuggestionDraft } from "@/lib/types";
import { ActionDialog } from "../card-actions";
import { OpenRing } from "../ui/glyphs";

export function DraftSheet({ draft, who }: { draft: SuggestionDraft; who: string | null }) {
  const [copied, setCopied] = useState(false);
  const to = draft.to.address ? (draft.to.name ? `${draft.to.name} <${draft.to.address}>` : draft.to.address) : null;
  return (
    <div className="overflow-hidden rounded-xl bg-bg shadow-lift" data-testid="suggestion-draft">
      <div className="flex items-center gap-3 border-b border-border px-4 py-2.5 text-[13px]">
        <span className="w-14 shrink-0 text-muted">To</span>
        <span data-testid="draft-to" className="min-w-0 flex-1 truncate">
          {to ?? (
            <span className="inline-flex items-center gap-1.5 text-muted" title={`No address on record for ${who ?? "the supplier"}`}>
              <OpenRing size={12} />
              No address on record
            </span>
          )}
        </span>
        <button
          type="button"
          onClick={() =>
            navigator.clipboard?.writeText(draftText(draft)).then(
              () => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1600);
              },
              () => undefined,
            )
          }
          className="-my-1 inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md px-2 text-[12px] text-muted hover:bg-hover hover:text-fg cursor-pointer"
          data-testid="copy-draft"
          title="Copy the draft"
        >
          {copied ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <div className="flex items-center gap-3 border-b border-border px-4 py-2.5 text-[13px]">
        <span className="w-14 shrink-0 text-muted">Subject</span>
        <span className="min-w-0 truncate font-medium">{draft.subject}</span>
      </div>
      <pre className="max-h-[260px] overflow-y-auto px-4 pt-3.5 pb-4 font-sans text-[13px] leading-[21px] whitespace-pre-wrap text-fg/90">{draft.body}</pre>
    </div>
  );
}

const MORE: CardActionOffer["kind"][] = ["snooze", "dismiss"];

/** The card's actions: send first, then message, follow up and done, the rest under More. */
export function SuggestionActions({ card, state, onDone }: { card: ProactiveSuggestion; state?: ActionResult | null; onDone: (r: ActionResult) => void }) {
  const [open, setOpen] = useState<CardActionOffer | null>(null);
  const offers = card.actions ?? [];
  if (!offers.length) return null;
  const rank = (k: string) => ["send_email", "teams_message", "create_follow_up", "mark_done"].indexOf(k);
  const shown = offers.filter((o) => !MORE.includes(o.kind)).sort((a, b) => ((rank(a.kind) + 9) % 9) - ((rank(b.kind) + 9) % 9));
  const more = offers.filter((o) => MORE.includes(o.kind));
  // a phone has room for the first two (send and message); the rest go under More
  const narrow = shown.slice(2);
  return (
    <div className="mt-3 flex items-center gap-1" data-testid="card-actions">
      {shown.map((o) => (
        <button
          key={o.kind}
          type="button"
          onClick={() => setOpen(o)}
          className={cn(
            "h-8 shrink-0 rounded-lg px-3 text-[13px] cursor-pointer max-sm:min-w-0 max-sm:shrink max-sm:truncate",
            narrow.includes(o) && "max-sm:hidden",
            o.kind === "send_email" ? "mr-1.5 bg-accent font-medium text-accent-fg shadow-sm hover:brightness-110" : "text-muted hover:bg-hover hover:text-fg",
          )}
          data-testid={`action-${o.kind}`}
        >
          {o.kind === "send_email" ? "Review and send" : o.label}
        </button>
      ))}
      {more.length + narrow.length > 0 && (
        <Menu.Root>
          <Menu.Trigger className="ml-auto flex size-8 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg cursor-pointer" aria-label="More actions">
            <MoreHorizontal className="size-4" aria-hidden />
          </Menu.Trigger>
          <Menu.Portal>
            <Menu.Content align="end" sideOffset={4} className="z-50 min-w-36 rounded-xl bg-raised p-1.5 shadow-pop">
              {[...narrow, ...more].map((o) => (
                <Menu.Item key={o.kind} onSelect={() => setOpen(o)} className={cn("flex cursor-pointer items-center rounded-md px-2 py-1.5 text-row outline-none data-[highlighted]:bg-hover", narrow.includes(o) && "sm:hidden")} data-testid={`action-${o.kind}`}>
                  {o.label}
                </Menu.Item>
              ))}
            </Menu.Content>
          </Menu.Portal>
        </Menu.Root>
      )}
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
