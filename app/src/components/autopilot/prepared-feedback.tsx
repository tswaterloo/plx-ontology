"use client";

import { useId, useState } from "react";
import { sendItemFeedback } from "@/lib/api";
import type { PreparedItem } from "@/lib/api";
import { FEEDBACK_MAX } from "@/components/feedback";

/** Right or wrong on one prepared Assist item, with an optional note. Sent to the owner. */
export function PreparedFeedback({ item }: { item: PreparedItem }) {
  const [state, setState] = useState<"idle" | "why" | "sending" | "right" | "wrong">("idle");
  const [note, setNote] = useState("");
  const [failed, setFailed] = useState(false);
  const noteId = useId();

  async function send(verdict: "useful" | "wrong") {
    setFailed(false);
    setState("sending");
    try {
      await sendItemFeedback({
        item_id: item.item_id,
        item_kind: "suggestion",
        event_type: `prepared_${item.kind}`,
        verdict,
        // the item id names it exactly; the label is only for reading, so a title past the label bound is left off, not cut
        ...(item.subject && item.subject.length <= 300 ? { label: item.subject } : {}),
        ...(note.trim() ? { note: note.trim() } : {}),
      });
      setState(verdict === "useful" ? "right" : "wrong");
    } catch {
      setFailed(true);
      setState(verdict === "useful" ? "idle" : "why");
    }
  }

  if (state === "right" || state === "wrong") {
    return (
      <p role="status" className="text-[12px] text-muted" data-testid="prepared-feedback-thanks">
        {state === "right" ? "Marked right." : "Marked wrong."} The owner sees it.
      </p>
    );
  }
  return (
    <div className="text-[12px] text-muted" data-testid="prepared-feedback" role="group" aria-label="Was this prepared item right?">
      {state === "why" || (state === "sending" && note) ? (
        <form
          className="flex items-center gap-1.5"
          onSubmit={(e) => {
            e.preventDefault();
            void send("wrong");
          }}
        >
          <label htmlFor={noteId} className="sr-only">What is wrong with this item?</label>
          <input
            id={noteId}
            autoFocus
            value={note}
            maxLength={FEEDBACK_MAX}
            onChange={(e) => setNote(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && setState("idle")}
            placeholder="What's wrong? (optional)"
            className="h-7 min-w-0 flex-1 rounded-md bg-surface px-2.5 text-[12px] text-fg placeholder:text-faint focus:outline-2 focus:outline-ring"
          />
          <button type="submit" disabled={state === "sending"} className="inline-flex h-7 items-center rounded-md bg-accent px-2.5 font-medium text-accent-fg hover:brightness-110 disabled:opacity-60 cursor-pointer">
            Mark wrong
          </button>
        </form>
      ) : (
        <span className="inline-flex items-center gap-2">
          <span className="text-[11.5px] text-muted">Was this right?</span>
          <button
            type="button"
            disabled={state === "sending"}
            onClick={() => void send("useful")}
            className="inline-flex h-6 items-center rounded-md border border-border bg-bg px-2 text-[11.5px] font-medium text-muted transition-colors hover:border-border-strong hover:bg-hover hover:text-fg disabled:opacity-60 cursor-pointer"
          >
            Right
          </button>
          <button
            type="button"
            disabled={state === "sending"}
            onClick={() => setState("why")}
            className="inline-flex h-6 items-center rounded-md border border-border bg-bg px-2 text-[11.5px] font-medium text-muted transition-colors hover:border-border-strong hover:bg-hover hover:text-fg disabled:opacity-60 cursor-pointer"
          >
            Wrong
          </button>
        </span>
      )}
      {failed && <span role="alert" className="ml-2 text-fg">Couldn&apos;t save that; try again</span>}
    </div>
  );
}
