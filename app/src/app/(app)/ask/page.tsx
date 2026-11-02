"use client";

import { MessageSquarePlus } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef } from "react";
import { AnswerCard } from "@/components/answer-card";
import { AskBox } from "@/components/ask-box";
import { takePending } from "@/lib/pending";
import { useThread } from "@/lib/thread";

import { VERIFIED_EXAMPLES } from "@/lib/examples";

const STARTERS = VERIFIED_EXAMPLES;

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

  const empty = turns.length === 0;
  const composer = (
    <>
      <AskBox
        size="md"
        dropUp={!empty}
        onSubmit={(question, pins) => run(question, pins[0]?.object_id)}
        busy={busy}
        onStop={stop}
        autoFocus={!q}
        placeholder={turns.length ? "Ask a follow-up…" : "Ask anything…"}
      />
      <p className="mt-2 text-center text-meta text-muted">Answers use records and messages you can open. Check the source before you act.</p>
    </>
  );

  return (
    <div className="flex flex-1 flex-col">
      <div className={`mx-auto w-full max-w-[760px] flex-1 px-4 pt-6 sm:px-6 ${empty ? "pb-16" : "pb-44"}`}>
        {empty ? (
          <div className="flex flex-col items-center pt-[14vh] text-center">
            <h1 className="text-title">What do you want to know?</h1>
            <p className="mt-2 text-body text-muted">Ask about any order, supplier, customer or lot. Plain words and typos are fine.</p>
            <div className="mt-6 w-full text-left">{composer}</div>
            <div className="mt-5 flex flex-wrap justify-center gap-2" aria-label="Example questions">
              {STARTERS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => run(s)}
                  className="min-h-9 rounded-full border border-border bg-surface px-3 py-1.5 text-row text-muted hover:border-control hover:text-fg cursor-pointer"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="space-y-10" aria-live="off">
            <div className="flex justify-end">
              <button
                type="button"
                onClick={() => {
                  reset();
                  started.current = null;
                  router.replace("/ask");
                }}
                className="inline-flex min-h-9 items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-row text-muted hover:bg-surface-2 hover:text-fg cursor-pointer"
              >
                <MessageSquarePlus className="size-4" aria-hidden /> New conversation
              </button>
            </div>
            {turns.map((t) => (
              <div key={t.id} className="space-y-5">
                <div className="flex justify-end">
                  <p
                    className="max-w-[80%] rounded-2xl rounded-br-md bg-surface-2 px-4 py-2.5 text-body"
                    data-testid="user-question"
                  >
                    {t.question}
                  </p>
                </div>
                <AnswerCard
                  turn={t}
                  onRetry={() => run(t.question)}
                  onPick={busy ? undefined : (c) => run(`I mean ${c.label}`, c.object_id)}
                  onAsk={busy ? undefined : (q) => run(q)}
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
      {!empty && (
        <div className="sticky bottom-0 z-20 bg-gradient-to-t from-bg via-bg to-transparent px-4 pt-6 pb-4 sm:px-6">
          <div className="mx-auto w-full max-w-[760px]">{composer}</div>
        </div>
      )}
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
