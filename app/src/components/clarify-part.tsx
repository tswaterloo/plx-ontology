"use client";

// The one question an answer asked back (answer.clarify) and the reading an answer assumed
// (answer.assumption). Each option is a quick reply; a reply continues the same
// conversation (AskRequest.clarified), so the question is never typed again.

import { useState } from "react";
import type { Answer, AskRequest } from "@/lib/types";
import { Button } from "./ui/button";

export type ClarifyReply = NonNullable<AskRequest["clarified"]>;

export function ClarifyPart({ a, onReply }: { a: Answer; onReply?: (words: string, reply: ClarifyReply) => void }) {
  const [text, setText] = useState("");
  const c = a.clarify;
  if (!c) return null;
  const busy = !onReply;
  return (
    <section className="space-y-2" data-testid="clarify">
      {(c.options ?? []).length > 0 && (
        <div className="flex flex-wrap gap-2">
          {(c.options ?? []).map((o, i) => (
            <Button key={i} size="sm" variant="outline" disabled={busy} title={o.detail ?? undefined} onClick={() => onReply?.(o.label, { answer_id: a.answer_id, option: i })}>
              {o.label}
              {o.detail ? <span className="ml-1.5 text-muted">{o.detail.split("; ").slice(0, 2).join(", ")}</span> : null}
            </Button>
          ))}
        </div>
      )}
      {c.free_text !== false && (
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
            className="min-w-0 flex-1 rounded-[3px] border border-border bg-bg px-2.5 py-1.5 text-[13px]"
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

export function AssumptionLine({ a, onReply }: { a: Answer; onReply?: (words: string, reply: ClarifyReply) => void }) {
  const s = a.assumption;
  if (!s) return null;
  return (
    <p className="text-[13px] leading-5 text-muted" data-testid="assumption">
      Taken as: {s.text}
      {(s.options ?? []).length > 0 && (
        <>
          {" "}
          Instead:{" "}
          {(s.options ?? []).map((o, i) => (
            <span key={i}>
              {i > 0 && ", "}
              <button type="button" className="text-accent hover:underline disabled:opacity-60" disabled={!onReply} onClick={() => onReply?.(o.label, { answer_id: a.answer_id, option: i })}>
                {o.label}
              </button>
            </span>
          ))}
        </>
      )}
    </p>
  );
}
