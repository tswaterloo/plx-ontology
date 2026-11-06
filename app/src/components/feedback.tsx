"use client";

import { Check, ThumbsDown, ThumbsUp } from "lucide-react";
import { useId, useState } from "react";
import { sendFeedback } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";

const REASONS = ["Wrong number", "Wrong item or order", "Missing a source", "Out of date", "Didn't answer the question"];

type State = "idle" | "up-sent" | "down-open" | "down-sent" | "sending";

/** The API's note field: chosen reasons first, then the person's words. */
export function feedbackNote(reasons: string[], note: string): string | undefined {
  const parts = [reasons.length ? `[${reasons.join("; ")}]` : "", note.trim()].filter(Boolean);
  return parts.length ? parts.join(" ").slice(0, 2000) : undefined;
}

export function Feedback({ answerId }: { answerId: string }) {
  const [state, setState] = useState<State>("idle");
  const [reasons, setReasons] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const noteId = useId();

  async function submit(rating: 1 | -1) {
    setError(null);
    const prev = state;
    setState("sending");
    try {
      await sendFeedback(
        rating === -1 ? { answer_id: answerId, rating, note: feedbackNote(reasons, note) } : { answer_id: answerId, rating },
      );
      setState(rating === 1 ? "up-sent" : "down-sent");
    } catch (e) {
      setError(plainError(e));
      setState(prev === "sending" ? "idle" : prev);
    }
  }

  if (state === "up-sent" || state === "down-sent") {
    return (
      <p role="status" className="inline-flex h-8 items-center gap-1.5 px-2.5 text-[13px] text-muted" data-testid="feedback-thanks">
        <Check className="size-[15px]" strokeWidth={2} aria-hidden />
        {state === "up-sent" ? "Marked correct" : "Sent to the Workbench team"}
      </p>
    );
  }

  const quiet = "inline-flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-[13px] text-muted hover:bg-hover hover:text-fg disabled:opacity-50 cursor-pointer [&_svg]:size-[15px]";
  return (
    <div className="contents" data-testid="feedback">
      <button type="button" aria-label="Yes, this answer was right" title="Was this right? Yes" onClick={() => submit(1)} disabled={state === "sending"} className={quiet}>
        <ThumbsUp strokeWidth={2} aria-hidden />
        <span className="max-sm:hidden">Correct</span>
      </button>
      <button
        type="button"
        aria-label="No, something was wrong"
        aria-expanded={state === "down-open"}
        title="Was this right? No"
        onClick={() => setState(state === "down-open" ? "idle" : "down-open")}
        disabled={state === "sending"}
        className={cn(quiet, state === "down-open" && "bg-hover text-fg")}
      >
        <ThumbsDown strokeWidth={2} aria-hidden />
        <span className="max-sm:hidden">Wrong</span>
      </button>
      {state === "down-open" && (
        <form
          className="order-last mt-1 w-full space-y-4 rounded-xl bg-bg p-4 shadow-lift"
          onSubmit={(e) => {
            e.preventDefault();
            void submit(-1);
          }}
        >
          <fieldset>
            <legend className="mb-2.5 text-[13px] font-medium">What was wrong?</legend>
            <div className="flex flex-wrap gap-1.5">
              {REASONS.map((r) => {
                const on = reasons.includes(r);
                return (
                  <button
                    key={r}
                    type="button"
                    aria-pressed={on}
                    onClick={() => setReasons(on ? reasons.filter((x) => x !== r) : [...reasons, r])}
                    className={cn(
                      "inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[12px] cursor-pointer",
                      on ? "bg-accent-soft font-medium text-fg" : "bg-surface-2 text-fg hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)]",
                    )}
                  >
                    {on && <Check className="size-3" strokeWidth={2.4} aria-hidden />}
                    {r}
                  </button>
                );
              })}
            </div>
          </fieldset>
          <div>
            <label htmlFor={noteId} className="sr-only">
              Tell us more
            </label>
            <textarea
              id={noteId}
              value={note}
              onChange={(e) => setNote(e.target.value)}
              rows={2}
              maxLength={2000}
              placeholder="What should it have said? (optional)"
              className="w-full resize-none rounded-lg bg-surface px-3 py-2 text-[13px] leading-5 placeholder:text-faint focus:outline-2 focus:outline-ring"
            />
          </div>
          <div className="flex items-center justify-end gap-1">
            <button type="button" onClick={() => setState("idle")} className="inline-flex h-8 items-center rounded-lg px-3 text-[13px] text-muted hover:bg-hover hover:text-fg cursor-pointer">
              Cancel
            </button>
            <button
              type="submit"
              disabled={reasons.length === 0 && note.trim() === ""}
              className="inline-flex h-8 items-center rounded-lg bg-accent px-3 text-[13px] font-medium text-accent-fg hover:brightness-110 disabled:opacity-50 cursor-pointer disabled:cursor-default"
            >
              Send feedback
            </button>
          </div>
        </form>
      )}
      {error && (
        <p role="alert" className="order-last w-full text-[12px] text-fg">
          {error}
        </p>
      )}
    </div>
  );
}
