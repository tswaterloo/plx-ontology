"use client";

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
  // The follow-up composer is a solid band stuck to the bottom of the window; the thread
  // keeps that band's height (plus a gap) free below its last line, however tall the band
  // grows (picked items, a long draft), so no answer ever sits under it. Set through the
  // CSSOM, not a style attribute, so the CSP's style-src holds.
  const thread = useRef<HTMLDivElement>(null);
  const dock = useRef<HTMLDivElement>(null);
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
  useEffect(() => {
    const el = dock.current;
    const body = thread.current;
    if (!el || !body) return;
    const fit = () => {
      const h = Math.ceil(el.getBoundingClientRect().height) + 16;
      body.style.paddingBottom = `${h}px`;
      // following the stream scrolls the last line to just above the band, not under it
      if (bottom.current) bottom.current.style.scrollMarginBottom = `${h - 8}px`;
    };
    fit();
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(fit);
    ro?.observe(el);
    return () => {
      ro?.disconnect();
      body.style.paddingBottom = "";
    };
  }, [empty]);
  const composer = (
    <>
      <AskBox
        size="md"
        dropUp={!empty}
        onSubmit={(question, pins) => run(question, pins[0]?.object_id)}
        busy={busy}
        onStop={stop}
        autoFocus={!q}
        placeholder={turns.length ? "Follow-up question" : "Question about a PO, SO, supplier or lot"}
      />
    </>
  );

  return (
    <div className="flex flex-1 flex-col">
      <div ref={thread} className={`mx-auto w-full max-w-[1200px] flex-1 px-4 pt-6 sm:px-8 sm:pt-8 ${empty ? "pb-16" : "pb-48"}`}>
        {empty ? (
          <div className="max-w-[760px]">
            <h1 className="border-b border-border pb-3 text-display">Ask</h1>
            <p className="mt-3 text-row text-muted">Answers cite the BC, Portal, mail and file records they come from.</p>
            <div className="mt-3 w-full">{composer}</div>
            <p className="mt-4 text-meta text-muted">For example:</p>
            <ul className="mt-0.5" aria-label="Example questions">
              {STARTERS.map((s) => (
                <li key={s}>
                  <button
                    type="button"
                    onClick={() => run(s)}
                    className="py-0.5 text-left text-row text-accent hover:underline cursor-pointer"
                  >
                    {s}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <div className="max-w-[760px] space-y-8" aria-live="off">
            <div className="flex justify-end">
              <button
                type="button"
                onClick={() => {
                  reset();
                  started.current = null;
                  router.replace("/ask");
                }}
                className="rounded-sm border border-control px-2.5 py-1 text-meta text-fg hover:bg-surface-2 cursor-pointer"
              >
                New question
              </button>
            </div>
            {turns.map((t) => (
              <div key={t.id} className="space-y-3 border-t border-border pt-4">
                <p className="text-title" data-testid="user-question">
                  {t.question}
                </p>
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
        <div ref={dock} className="sticky bottom-0 z-20 border-t border-border bg-bg pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]" data-testid="composer-dock">
          <div className="mx-auto w-full max-w-[1200px] px-4 sm:px-8"><div className="max-w-[760px]">{composer}</div></div>
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
