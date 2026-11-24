"use client";

import { useRef, useState } from "react";
import { streamWhy, type WhyStep } from "@/lib/api";
import { plainError } from "@/lib/errors";

const link = "text-[12px] text-accent underline-offset-2 hover:underline cursor-pointer disabled:cursor-default disabled:opacity-50";

/**
 * "Why did this change?" on a measure tile: the steps appear as each is read (the change, which dimension holds it,
 * that dimension's parts), each with the best answer so far. "Answer now" stops reading and keeps that answer: it is
 * true after every step, so stopping early never leaves a guess.
 */
export function WhyDidItChange({ id, testId }: { id: string; testId: string }) {
  const [steps, setSteps] = useState<WhyStep[]>([]);
  const [state, setState] = useState<"idle" | "reading" | "stopped" | "done">("idle");
  const [error, setError] = useState<string | null>(null);
  const ctl = useRef<AbortController | null>(null);
  const start = () => {
    const c = new AbortController();
    ctl.current = c;
    setSteps([]);
    setError(null);
    setState("reading");
    streamWhy(id, (s) => setSteps((all) => [...all, s]), c.signal).then(
      () => !c.signal.aborted && setState("done"),
      (e) => {
        if (c.signal.aborted) return;
        setError(plainError(e));
        setState("done");
      },
    );
  };
  const stop = () => {
    ctl.current?.abort();
    setState("stopped");
  };
  const last = steps[steps.length - 1];
  const failed = steps.find((s) => s.error);
  return (
    <div className="space-y-1 text-[13px]" data-testid={testId}>
      {state === "idle" && <button type="button" className={link} onClick={start}>Why did this change?</button>}
      {state === "reading" && <button type="button" className={link} onClick={stop}>Answer now</button>}
      {state !== "idle" && state !== "reading" && <button type="button" className={link} onClick={start}>Read again</button>}
      {steps.map((s) => (
        <p key={s.step} className="text-muted" data-testid={`${testId}-step-${s.step}`}>
          Step {s.step} of {s.of}: {s.says}{s.error ? ` could not be read (${s.error.message ?? s.error.code})` : ""}
        </p>
      ))}
      {state === "reading" && <p className="text-muted">Reading</p>}
      {last?.answer && (
        <p data-testid={`${testId}-answer`}>
          {state === "reading" ? "So far: " : state === "stopped" ? "Answer so far: " : ""}{last.answer}
        </p>
      )}
      {failed && last?.answer ? <p className="text-muted">It stopped at step {failed.step}; the answer above is what was read before it.</p> : null}
      {error && <p className="text-red-700" role="alert">{error}</p>}
    </div>
  );
}
