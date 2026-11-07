"use client";

import { useId, useState } from "react";
import { sendItemFeedback } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { BriefingItem, ItemFeedbackRequest } from "@/lib/types";

export type ItemVerdict = ItemFeedbackRequest["verdict"];

const WORDS: Record<ItemVerdict, string> = { useful: "Useful", not_useful: "Not useful", wrong: "Wrong" };

/** What an item's feedback carries: the item, what it is about and the verdict. */
export function itemBody(item: BriefingItem, verdict: ItemVerdict, note?: string): ItemFeedbackRequest {
  const body: ItemFeedbackRequest = {
    item_id: item.id,
    verdict,
    item_kind: item.event_type === "watcher_attention" ? "watcher" : "briefing",
    event_type: item.event_type,
    object_type: item.object_type,
    object_id: item.object_id,
    label: (item.subject_label || item.label || "").slice(0, 300),
  };
  if (note?.trim()) body.note = note.trim().slice(0, 2000);
  return body;
}

/**
 * Useful, not useful or wrong, on one briefing or alert item: one click each (wrong asks what,
 * optionally). Not useful and wrong take the item out of your briefing (`onMarked`); the AI
 * watcher and the proactive engine read the same signal.
 */
export function ItemFeedback({
  item,
  onMarked,
  onWrong,
  className,
}: {
  item: BriefingItem;
  onMarked?: (verdict: ItemVerdict) => void;
  /** Wrong opens the reason form where the parent draws it (a row draws it under its line). */
  onWrong?: () => void;
  className?: string;
}) {
  const [state, setState] = useState<"idle" | "why" | "sending" | "useful">("idle");
  const [failed, setFailed] = useState(false);

  async function mark(verdict: ItemVerdict) {
    setFailed(false);
    setState("sending");
    try {
      await sendItemFeedback(itemBody(item, verdict));
      if (verdict === "useful") setState("useful");
      else onMarked?.(verdict);
    } catch {
      setFailed(true);
      setState("idle");
    }
  }

  if (state === "useful") {
    return (
      <span role="status" className={cn("relative z-10 shrink-0 text-muted", className)} data-testid="item-feedback-thanks">
        Marked useful
      </span>
    );
  }
  if (state === "why") {
    return <WrongReason item={item} onMarked={(v) => onMarked?.(v)} onCancel={() => setState("idle")} />;
  }
  const busy = state === "sending";
  return (
    <span className={cn("relative z-10 flex shrink-0 items-baseline gap-2.5", className)} data-testid="item-feedback" aria-label="Was this useful?" role="group">
      {(Object.keys(WORDS) as ItemVerdict[]).map((v) => (
        <button
          key={v}
          type="button"
          disabled={busy}
          onClick={() => (v !== "wrong" ? void mark(v) : onWrong ? onWrong() : setState("why"))}
          className="text-muted hover:text-fg hover:underline disabled:opacity-60 cursor-pointer"
        >
          {WORDS[v]}
        </button>
      ))}
      {failed && (
        <span role="alert" className="text-fg">
          Couldn't save. Try again.
        </span>
      )}
    </span>
  );
}

/** An item marked not useful or wrong: said in one line where the item was. */
export function MarkedLine({ verdict, testId }: { verdict: ItemVerdict; testId?: string }) {
  return (
    <li className="px-3 py-2 text-[12px] leading-4 text-muted" data-testid={testId ?? "item-marked"}>
      {verdict === "wrong" ? "Marked wrong" : "Marked not useful"}. It won't show in your briefing again; the owner reviews it.
    </li>
  );
}

/** The reason form of Wrong, on its own: a row draws it under its line. */
export function WrongReason({ item, onMarked, onCancel }: { item: BriefingItem; onMarked: (verdict: ItemVerdict) => void; onCancel: () => void }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const noteId = useId();
  return (
    <form
      className="relative z-10 mt-1.5 flex w-full items-center gap-1.5"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setFailed(false);
        try {
          await sendItemFeedback(itemBody(item, "wrong", note));
          onMarked("wrong");
        } catch {
          setFailed(true);
          setBusy(false);
        }
      }}
      data-testid="item-feedback-why"
    >
      <label htmlFor={noteId} className="sr-only">
        What is wrong with this item?
      </label>
      <input
        id={noteId}
        autoFocus
        value={note}
        maxLength={2000}
        onChange={(e) => setNote(e.target.value)}
        onKeyDown={(e) => e.key === "Escape" && onCancel()}
        placeholder="What's wrong? (optional)"
        className="h-7 min-w-0 flex-1 rounded-md bg-surface px-2.5 text-[12px] text-fg placeholder:text-faint focus:outline-2 focus:outline-ring"
      />
      <button type="submit" disabled={busy} className="inline-flex h-7 shrink-0 items-center rounded-md bg-accent px-2.5 text-[12px] font-medium text-accent-fg hover:brightness-110 disabled:opacity-60 cursor-pointer">
        Mark wrong
      </button>
      <button type="button" onClick={onCancel} className="inline-flex h-7 shrink-0 items-center rounded-md px-2 text-[12px] text-muted hover:bg-hover hover:text-fg cursor-pointer">
        Cancel
      </button>
      {failed && (
        <span role="alert" className="text-[12px] text-fg">
          Couldn't save. Try again.
        </span>
      )}
    </form>
  );
}
