"use client";

import { Fragment, useRef, useState } from "react";
import { streamWhy, type WhyRank, type WhyStep } from "@/lib/api";
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
  const [parts, setParts] = useState<Record<string, WhyStep | "reading">>({});
  const drill = (dimension: string) => {
    setParts((p) => ({ ...p, [dimension]: "reading" }));
    streamWhy(id, (s) => setParts((p) => ({ ...p, [dimension]: s })), undefined, { drill: dimension }).catch((e) =>
      setParts((p) => ({ ...p, [dimension]: { step: 1, of: 1, says: dimension, answer: "", error: { code: "failed", message: plainError(e), status: 0 } } })));
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
      {steps.map((s) => s.ranking && (
        <table key={`rank-${s.step}`} className="w-full text-[12px]" data-testid={`${testId}-ranking`}>
          <thead><tr className="text-left text-muted"><th>Dimension</th><th>Biggest part</th><th>Its change</th><th>Share of the change</th><th>Parts holding half</th><th /></tr></thead>
          <tbody>
            {s.ranking.map((r: WhyRank, i) => {
              const key = `${r.dimension}`;
              const got = s.ranking!.findIndex((x) => x.dimension === r.dimension) === i ? parts[key] : undefined;
              return (
                <Fragment key={`${key}-${i}`}>
                  <tr>
                    <td>{r.dimension}</td><td>{r.top_part ?? "none readable"}</td><td>{r.top_change ?? ""}</td>
                    <td>{r.top_share_of_change_pct == null ? "" : `${r.top_share_of_change_pct}%`}</td>
                    <td>{r.parts_for_half == null ? "" : `${r.parts_for_half} of ${r.parts}`}</td>
                    <td><button type="button" className={link} onClick={() => drill(key)}>Show every part</button></td>
                  </tr>
                  {got && (
                    <tr data-testid={`${testId}-parts-${key}`}><td colSpan={6} className="text-muted">
                      {got === "reading" ? "Reading" : got.error ? `Could not be read (${got.error.message ?? got.error.code})` : (
                        <table className="w-full"><thead><tr>{(got.columns ?? []).map((c) => <th key={c} className="text-left">{c}</th>)}</tr></thead>
                          <tbody>{(got.rows ?? []).map((row, j) => <tr key={j}>{row.map((c, k) => <td key={k}>{c ?? ""}</td>)}</tr>)}</tbody></table>
                      )}
                    </td></tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
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
