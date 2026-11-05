"use client";

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
      <p role="status" className="text-meta text-muted" data-testid="feedback-thanks">
        {state === "up-sent" ? "Marked correct." : "Sent to the Workbench team."}
      </p>
    );
  }

  return (
    <div className="space-y-3" data-testid="feedback">
      <div className="flex items-center justify-end gap-1">
        <button
          type="button"
          aria-label="Yes, this answer was right"
          title="Was this right? Yes"
          onClick={() => submit(1)}
          disabled={state === "sending"}
          className="rounded-sm px-2 py-1 text-meta text-accent hover:bg-surface-2 disabled:opacity-50 cursor-pointer"
        >
          Correct
        </button>
        <button
          type="button"
          aria-label="No, something was wrong"
          aria-expanded={state === "down-open"}
          title="Was this right? No"
          onClick={() => setState(state === "down-open" ? "idle" : "down-open")}
          disabled={state === "sending"}
          className={cn(
            "rounded-sm px-2 py-1 text-meta text-accent hover:bg-surface-2 disabled:opacity-50 cursor-pointer",
            state === "down-open" && "bg-surface-2 text-fg",
          )}
        >
          Wrong
        </button>
      </div>
      {state === "down-open" && (
        <form
          className="space-y-3 rounded-sm border border-border-strong p-3"
          onSubmit={(e) => {
            e.preventDefault();
            void submit(-1);
          }}
        >
          <fieldset>
            <legend className="mb-2 text-row font-medium">What was wrong?</legend>
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
                      "rounded-sm border px-2 py-0.5 text-meta cursor-pointer",
                      on ? "border-fg bg-surface-2 font-medium text-fg" : "border-border-strong text-fg hover:bg-surface-2",
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
              placeholder="What should it have said? (optional)"
              className="w-full resize-none rounded-sm border border-control bg-bg px-3 py-2 text-row placeholder:text-faint focus:border-fg"
            />
          </div>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setState("idle")}
              className="rounded-sm px-3 py-1 text-row text-fg hover:bg-surface-2 cursor-pointer"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={reasons.length === 0 && note.trim() === ""}
              className="rounded-sm bg-accent px-3 py-1 text-row font-medium text-accent-fg disabled:opacity-50 cursor-pointer"
            >
              Send feedback
            </button>
          </div>
        </form>
      )}
      {error && (
        <p role="alert" className="text-meta text-bad">
          {error}
        </p>
      )}
    </div>
  );
}
