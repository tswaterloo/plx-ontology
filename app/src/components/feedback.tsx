"use client";

import { ThumbsDown, ThumbsUp } from "lucide-react";
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
      <p role="status" className="text-xs text-muted" data-testid="feedback-thanks">
        Thanks — {state === "up-sent" ? "glad it helped." : "we'll use this to improve answers."}
      </p>
    );
  }

  return (
    <div className="space-y-3" data-testid="feedback">
      <div className="flex items-center gap-1">
        <span className="mr-1 text-xs text-muted">Was this right?</span>
        <button
          type="button"
          aria-label="Yes, this answer was right"
          onClick={() => submit(1)}
          disabled={state === "sending"}
          className="rounded-md p-1.5 text-muted transition-colors hover:bg-surface-2 hover:text-fg disabled:opacity-50 cursor-pointer"
        >
          <ThumbsUp className="size-4" aria-hidden />
        </button>
        <button
          type="button"
          aria-label="No, something was wrong"
          aria-expanded={state === "down-open"}
          onClick={() => setState(state === "down-open" ? "idle" : "down-open")}
          disabled={state === "sending"}
          className={cn(
            "rounded-md p-1.5 text-muted transition-colors hover:bg-surface-2 hover:text-fg disabled:opacity-50 cursor-pointer",
            state === "down-open" && "bg-surface-2 text-fg",
          )}
        >
          <ThumbsDown className="size-4" aria-hidden />
        </button>
      </div>
      {state === "down-open" && (
        <form
          className="space-y-3 rounded-xl border border-border bg-bg-subtle p-3 animate-fade-in"
          onSubmit={(e) => {
            e.preventDefault();
            void submit(-1);
          }}
        >
          <fieldset>
            <legend className="mb-2 text-sm font-medium">What was wrong?</legend>
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
                      "rounded-full border px-2.5 py-1 text-xs transition-colors cursor-pointer",
                      on ? "border-accent bg-accent-soft text-accent" : "border-border bg-surface text-fg hover:bg-surface-2",
                    )}
                  >
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
              placeholder="Tell us more (optional): what should it have said?"
              className="w-full resize-none rounded-lg border border-border bg-surface px-3 py-2 text-sm placeholder:text-muted focus:border-accent focus:outline-none"
            />
          </div>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setState("idle")}
              className="rounded-lg px-3 py-1.5 text-sm text-muted hover:bg-surface-2 cursor-pointer"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={reasons.length === 0 && note.trim() === ""}
              className="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-accent-fg disabled:opacity-50 cursor-pointer"
            >
              Send feedback
            </button>
          </div>
        </form>
      )}
      {error && (
        <p role="alert" className="text-xs text-bad">
          {error}
        </p>
      )}
    </div>
  );
}
