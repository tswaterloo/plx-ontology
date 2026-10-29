"use client";

import { MessageSquarePlus } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef } from "react";
import { AnswerCard } from "@/components/answer-card";
import { AskBox } from "@/components/ask-box";
import { takePending } from "@/lib/pending";
import { useThread } from "@/lib/thread";

const STARTERS = [
  "Which purchase orders are late, and why?",
  "why is the soap order from that canadian guy late",
  "landed cost on the latest shea butter lot",
];

function Ask() {
  const params = useSearchParams();
  const router = useRouter();
  const { turns, run, stop, reset, busy } = useThread();
  const started = useRef<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const q = params.get("q");
  const scope = params.get("object");

  useEffect(() => {
    const key = `${q}\u0000${scope}`;
    if (q && started.current !== key) {
      started.current = key;
      if (turns.length > 0) reset();
      void run(q, scope ?? takePending(q)[0]?.object_id);
    }
  }, [q, scope, run, reset, turns.length]);

  // Follow the stream while the reader is near the bottom.
  const last = turns[turns.length - 1];
  useEffect(() => {
    const el = bottom.current;
    if (!el) return;
    const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 240;
    if (nearBottom || last?.phase === "thinking") el.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [last?.streamed, last?.phase, turns.length]);

  return (
    <div className="flex flex-1 flex-col">
      <div className="mx-auto w-full max-w-3xl flex-1 px-4 pt-6 pb-40 sm:px-6">
        {turns.length === 0 ? (
          <div className="flex flex-col items-center pt-[12vh] text-center">
            <h1 className="text-2xl font-semibold tracking-tight">What do you want to know?</h1>
            <p className="mt-2 text-muted">Ask in your own words. Follow-up questions keep the context.</p>
            <div className="mt-6 flex flex-wrap justify-center gap-2">
              {STARTERS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => run(s)}
                  className="rounded-full border border-border bg-surface px-3 py-1.5 text-sm text-muted hover:border-border-strong hover:text-fg cursor-pointer"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="space-y-8" aria-live="off">
            <div className="flex justify-end">
              <button
                type="button"
                onClick={() => {
                  reset();
                  started.current = null;
                  router.replace("/ask");
                }}
                className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-muted hover:bg-surface-2 hover:text-fg cursor-pointer"
              >
                <MessageSquarePlus className="size-3.5" aria-hidden /> New conversation
              </button>
            </div>
            {turns.map((t) => (
              <div key={t.id} className="space-y-5">
                <div className="flex justify-end">
                  <p
                    className="max-w-[85%] rounded-2xl rounded-br-md bg-surface-2 px-4 py-2.5 text-[15px] leading-relaxed"
                    data-testid="user-question"
                  >
                    {t.question}
                  </p>
                </div>
                <AnswerCard
                  turn={t}
                  onRetry={() => run(t.question)}
                  onPick={busy ? undefined : (c) => run(`I mean ${c.label}`, c.object_id)}
                />
              </div>
            ))}
            {last && last.phase === "done" && (
              <p className="sr-only" role="status">
                Answer ready.
              </p>
            )}
          </div>
        )}
        <div ref={bottom} />
      </div>
      <div className="sticky bottom-0 z-20 bg-gradient-to-t from-bg via-bg to-transparent px-4 pt-6 pb-4 sm:px-6">
        <div className="mx-auto w-full max-w-3xl">
          <AskBox
            size="md"
            onSubmit={(question, pins) => run(question, pins[0]?.object_id)}
            busy={busy}
            onStop={stop}
            autoFocus={!q}
            placeholder={turns.length ? "Ask a follow-up…" : "Ask anything…"}
          />
          <p className="mt-2 text-center text-[11px] text-muted">
            Answers come from PLX records and documents you can read. Check the sources before acting.
          </p>
        </div>
      </div>
    </div>
  );
}

export default function AskPage() {
  return (
    <Suspense fallback={null}>
      <Ask />
    </Suspense>
  );
}
