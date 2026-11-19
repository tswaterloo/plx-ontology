"use client";

import * as Menu from "@radix-ui/react-dropdown-menu";
import { MoreHorizontal } from "lucide-react";
import { useReaderWords } from "@/components/view-as";
import Link from "next/link";
import { useState } from "react";
import { ActionDialog } from "@/components/card-actions";
import { EmptyState } from "@/components/ui/empty-state";
import { cn } from "@/lib/cn";
import { plural } from "@/lib/format";
import type { ActionResult, CardActionOffer, ReplyLine, ReplyTriage } from "@/lib/types";

// What needs my reply today: your own mail and one-to-one chats where someone asked you
// something you have not answered, heaviest first. A reply goes out only through the
// send dialog's preview and your confirm. No holding-reply text is drawn here: a draft
// appears only where the Autopilot prepared a real one (Prepared items).

// the plain words for the main buttons; everything else waits under More
const MAIN: { kind: CardActionOffer["kind"][]; label: string; primary?: boolean }[] = [
  { kind: ["send_email", "reply_in_thread"], label: "Reply", primary: true },
  { kind: ["mark_done"], label: "Done" },
  { kind: ["snooze"], label: "Snooze" },
  { kind: ["dismiss"], label: "Not for me" },
];

function ReplyActions({ line, state, onDone }: { line: ReplyLine; state: ActionResult | null; onDone: (r: ActionResult) => void }) {
  const [open, setOpen] = useState<CardActionOffer | null>(null);
  const offers = line.offers ?? [];
  if (!line.card_id || !offers.length) return null;
  const mains = MAIN.map((m) => ({ ...m, offer: m.kind.map((k) => offers.find((o) => o.kind === k)).find(Boolean) })).filter((m) => m.offer);
  const used = new Set(mains.map((m) => m.offer));
  const more = offers.filter((o) => !used.has(o) && o.kind !== "reply_in_thread");
  const card = { id: line.card_id, subject: { label: line.subject ?? "Needs your reply" }, actions: offers };
  return (
    <div className="flex flex-wrap items-center gap-1" data-testid="card-actions">
      {mains.map((m) => (
        <button
          key={m.label}
          type="button"
          onClick={() => setOpen(m.offer!)}
          className={cn("h-8 rounded-lg px-3 text-[13px] cursor-pointer", m.primary ? "mr-1.5 bg-accent font-medium text-accent-fg shadow-sm hover:brightness-110" : "text-muted hover:bg-hover hover:text-fg")}
          data-testid={`action-${m.offer!.kind}`}
        >
          {m.label}
        </button>
      ))}
      {more.length > 0 && (
        <Menu.Root>
          <Menu.Trigger className="flex h-8 items-center gap-1 rounded-lg px-2 text-[13px] text-muted hover:bg-hover hover:text-fg cursor-pointer" aria-label="More">
            More
            <MoreHorizontal className="size-4" aria-hidden />
          </Menu.Trigger>
          <Menu.Portal>
            <Menu.Content align="end" sideOffset={4} className="z-50 min-w-36 rounded-xl bg-raised p-1.5 shadow-pop">
              {more.map((o) => (
                <Menu.Item key={o.kind} onSelect={() => setOpen(o)} className="flex cursor-pointer items-center rounded-md px-2 py-1.5 text-row outline-none data-[highlighted]:bg-hover" data-testid={`action-${o.kind}`}>
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

function meta(line: ReplyLine) {
  return (
    `${line.channel === "teams" ? "Teams" : "Mail"} · ${plural(line.days, "business day")} waiting` +
    (line.chases ? ` · asked ${line.chases + 1} times` : "") +
    (line.customer_tier && line.customer_tier !== "none" ? ` · tier ${line.customer_tier} customer` : "") +
    Object.entries(line.records_named ?? {}).map(([kind, n]) => ` · ${n} ${kind}`).join("")
  );
}

/** One message waiting on a reply, with its buttons: the card on a phone, the detail pane on a desktop. */
export function ReplyCard({ line, state, onDone, className }: { line: ReplyLine; state: ActionResult | null; onDone: (r: ActionResult) => void; className?: string }) {
  return (
    <div className={cn("space-y-2 text-[13px]", className)} data-testid="reply-line">
      <div>
        <span className="font-medium">{line.subject || "(no subject)"}</span>
        {line.new_inquiry && <span className="ml-2 rounded bg-hover px-1 text-[11px]">New inquiry</span>}
      </div>
      <div className="text-muted">{meta(line)}</div>
      {line.object_id && (
        <Link className="block font-medium hover:underline" href={`/object/${encodeURIComponent(line.object_id.split(":")[0])}/${encodeURIComponent(line.object_id)}`}>
          Open the message
        </Link>
      )}
      <ReplyActions line={line} state={state} onDone={onDone} />
    </div>
  );
}

/**
 * The list. With `onSelect` (a desktop) each message is a row that opens in the detail pane;
 * without it each message is a full card.
 */
export function ReplyList({
  triage,
  selectedId,
  onSelect,
  states: given,
  onDone: giveDone,
}: {
  triage: ReplyTriage;
  selectedId?: string | null;
  onSelect?: (id: string) => void;
  states?: Record<string, ActionResult>;
  onDone?: (r: ActionResult) => void;
}) {
  const [own, setOwn] = useState<Record<string, ActionResult>>({});
  const states = given ?? own;
  const onDone = giveDone ?? ((r: ActionResult) => setOwn((s) => ({ ...s, [r.card_id]: r })));
  const lines = triage.lines ?? [];
  const w = useReaderWords();
  return (
    <div className="space-y-4" data-testid="reply-triage">
      <p className="text-[12px] text-muted">{plural(triage.total, "message")} waiting on {w.your} reply, from {w.your === "your" ? "your own" : w.your} mailbox and chats.</p>
      {lines.length === 0 && <EmptyState title={`Nothing is waiting on ${w.your} reply.`} />}
      <ul className={onSelect ? "-mx-3 space-y-0.5" : "space-y-3"}>
        {lines.map((line: ReplyLine) =>
          onSelect ? (
            <li key={line.id} data-testid="reply-line">
              <button
                type="button"
                onClick={() => onSelect(line.id)}
                aria-current={selectedId === line.id ? "true" : undefined}
                className={cn("block w-full rounded-lg px-3 py-2 text-left text-[13px] cursor-pointer hover:bg-hover", selectedId === line.id && "bg-hover")}
              >
                <span className="block truncate font-medium">{line.subject || "(no subject)"}</span>
                <span className="block truncate text-muted">{meta(line)}</span>
              </button>
            </li>
          ) : (
            <li key={line.id} className="rounded-lg border border-border p-3">
              <ReplyCard line={line} state={line.card_id ? (states[line.card_id] ?? null) : null} onDone={onDone} />
            </li>
          ),
        )}
      </ul>
    </div>
  );
}
