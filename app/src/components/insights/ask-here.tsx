"use client";

import { X } from "lucide-react";
import { useEffect, useRef } from "react";
import { AnswerCard } from "@/components/answer-card";
import { AskBox } from "@/components/ask-box";
import { PinnedHere } from "@/components/insights/pinned";
import { forget, recall, remember } from "@/lib/tab-memory";
import { useThread } from "@/lib/thread";
import type { Answer } from "@/lib/types";

const DRAFT_KEY = "plx.insights.draft";
const LAST_KEY = "plx.insights.last";

interface Last {
  question: string;
  answer: Answer;
}
const isLast = (v: unknown): v is Last =>
  !!v && typeof v === "object" && typeof (v as Last).question === "string" && !!(v as Last).answer && typeof (v as Last).answer === "object" && typeof (v as Last).answer.direct_answer === "string";

/** A question asked on Insights itself: the answer is drawn here, and a chart in it is added to
 * the person's charts with its own "Pin" (the dashboard then shows it at once: `onPinned`).
 * It is the Ask (same engine, same access), so a question about a number reads like one asked
 * on the Ask page. */
export function InsightsAsk({ onPinned }: { onPinned: () => void }) {
  const { turns, run, retry, followUp, stop, reset, busy, seed } = useThread();
  const last = turns[turns.length - 1];
  // Coming back to Insights shows the last answer (and its chart) again, kept for this tab
  const restored = useRef(false);
  useEffect(() => {
    const kept = recall(LAST_KEY, isLast);
    if (kept) seed([{ question: kept.question, answer: kept.answer }]);
    restored.current = true;
  }, [seed]);
  useEffect(() => {
    if (!restored.current) return;
    if (last && last.phase === "done" && last.answer) remember(LAST_KEY, { question: last.question, answer: last.answer });
  }, [last]);
  return (
    <section className="space-y-3" aria-label="Ask about your numbers" data-testid="insights-ask">
      <AskBox
        size="md"
        draftKey={DRAFT_KEY}
        label="Ask about your numbers"
        placeholder="Ask about a number or ask for a chart; add it to your charts from the answer"
        onSubmit={(question, pins) => run(question, pins[0]?.object_id)}
        busy={busy}
        onStop={stop}
      />
      {last && (
        <div className="space-y-2 border-t border-border pt-3" data-testid="insights-ask-answer">
          <div className="flex items-baseline gap-2">
            <h2 className="flex-1 text-[14px] font-semibold break-words">{last.question}</h2>
            <button type="button" onClick={() => {
                forget(LAST_KEY);
                reset();
              }} aria-label="Close this answer" className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-[12px] font-medium hover:bg-hover cursor-pointer">
              <X className="size-3.5" aria-hidden /> Close
            </button>
          </div>
          <PinnedHere.Provider value={onPinned}>
            <AnswerCard turn={last} onRetry={() => retry(last)} onAsk={busy ? undefined : (next) => followUp(next, last)} />
          </PinnedHere.Provider>
        </div>
      )}
    </section>
  );
}
