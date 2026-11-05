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
      const h = Math.ceil(el.getBoundingClientRect().height) + 32;
      body.style.paddingBottom = `${h}px`;
      // following the stream scrolls the last line to just above the band, not under it
      if (bottom.current) bottom.current.style.scrollMarginBottom = `${h - 24}px`;
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
        placeholder={turns.length ? "Ask a follow-up…" : "Ask anything…"}
      />
      <p className="mt-2 text-center text-meta text-faint">Answers use records and messages you can open. Check the source before you act.</p>
    </>
  );

  return (
    <div className="flex flex-1 flex-col">
      <div ref={thread} className={`mx-auto w-full max-w-[720px] flex-1 px-4 pt-6 sm:px-6 ${empty ? "pb-16" : "pb-48"}`}>
        {empty ? (
          <div className="pt-[10vh]">
            <p className="text-eyebrow uppercase text-muted">Ask</p>
            <h1 className="mt-2 text-display">What do you want to know?</h1>
            <p className="mt-2 max-w-[60ch] text-body text-muted">Ask about any order, supplier, customer or lot, in plain words. Every answer names the records it came from.</p>
            <div className="mt-6 w-full">{composer}</div>
            <ul className="mt-6 divide-y divide-border border-t border-border" aria-label="Example questions">
              {STARTERS.map((s) => (
                <li key={s}>
                  <button
                    type="button"
                    onClick={() => run(s)}
                    className="group flex w-full items-baseline gap-2 py-2.5 text-left text-row text-muted hover:text-fg cursor-pointer"
                  >
                    <span className="text-faint group-hover:text-accent" aria-hidden>→</span>
                    {s}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <div className="space-y-12" aria-live="off">
            <div className="flex justify-end">
              <button
                type="button"
                onClick={() => {
                  reset();
                  started.current = null;
                  router.replace("/ask");
                }}
                className="inline-flex min-h-8 items-center gap-1.5 rounded px-2 py-1 text-meta text-muted hover:bg-surface-2 hover:text-fg cursor-pointer"
              >
                <MessageSquarePlus className="size-4" aria-hidden /> New conversation
              </button>
            </div>
            {turns.map((t) => (
              <div key={t.id} className="space-y-4 border-t border-rule pt-4">
                <p className="text-row font-medium text-muted" data-testid="user-question">
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
        <div ref={dock} className="sticky bottom-0 z-20 border-t border-border bg-bg px-4 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:px-6" data-testid="composer-dock">
          <div className="mx-auto w-full max-w-[672px]">{composer}</div>
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
