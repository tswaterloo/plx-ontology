"use client";

// The one question an answer asked back (answer.clarify). Each option is a quick reply;
// a reply continues the same conversation (AskRequest.clarified), so the question is
// never typed again.

import { useState } from "react";
import type { Answer, AskRequest } from "@/lib/types";
import { Button } from "./ui/button";

export type ClarifyReply = NonNullable<AskRequest["clarified"]>;

export function ClarifyPart({
  a,
  onReply,
  hideInput = false,
}: {
  a: Answer;
  onReply?: (words: string, reply: ClarifyReply) => void;
  /** Hide the inline free-text box when the page's unified composer accepts the reply. */
  hideInput?: boolean;
}) {
  const [text, setText] = useState("");
  const c = a.clarify;
  if (!c) return null;
  const busy = !onReply;
  return (
    <section className="space-y-2.5" data-testid="clarify">
      {(c.options ?? []).length > 0 && (
        <div className="flex flex-wrap gap-2">
          {(c.options ?? []).map((o, i) => (
            <button
              key={i}
              type="button"
              disabled={busy}
              title={o.detail ?? undefined}
              onClick={() => onReply?.(o.label, { answer_id: a.answer_id, option: i })}
              className="inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-border-strong/80 bg-bg px-3 py-1.5 text-left text-[13px] font-medium text-fg shadow-sm transition-colors hover:border-accent/60 hover:bg-accent-soft/50 hover:text-accent disabled:cursor-default disabled:opacity-50 cursor-pointer"
            >
              <span>{o.label}</span>
              {o.detail ? <span className="text-[12px] font-normal text-muted">{o.detail.split("; ").slice(0, 2).join(", ")}</span> : null}
            </button>
          ))}
        </div>
      )}
      {!hideInput && c.free_text !== false && (
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (text.trim()) onReply?.(text.trim(), { answer_id: a.answer_id, text: text.trim() });
          }}
        >
          <label className="sr-only" htmlFor={`clarify-${a.answer_id}`}>
            Your answer
          </label>
          <input
            id={`clarify-${a.answer_id}`}
            className="min-w-0 flex-1 rounded-lg border border-border-strong bg-bg px-3 py-1.5 text-[13px] outline-none focus:border-accent"
            value={text}
            maxLength={400}
            onChange={(e) => setText(e.target.value)}
            placeholder="Or say which"
          />
          <Button size="sm" type="submit" disabled={busy || !text.trim()}>
            Answer
          </Button>
        </form>
      )}
    </section>
  );
}
