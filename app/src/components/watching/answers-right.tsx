"use client";

import { useEffect, useState } from "react";
import { MeasureCandidates } from "@/components/watching/measure-candidates";
import { getAllFeedback, getFeedback, getGaps, getObserved, setFeedbackStatus } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { plainError } from "@/lib/errors";
import type { AskDay, AskObserved, FeedbackEntry, FeedbackStatus, GapRow } from "@/lib/types";

const link = "text-[12px] text-accent underline-offset-2 hover:underline cursor-pointer disabled:cursor-default disabled:opacity-50";

/** Pure: the thumbs on answers, from their entries: up, down and still to be looked at. */
export function thumbs(entries: Pick<FeedbackEntry, "rating" | "status">[]): { up: number; down: number; waiting: number } {
  let up = 0, down = 0, waiting = 0;
  for (const e of entries) {
    if (e.rating === 1) up++;
    else if (e.rating === -1) {
      down++;
      if (e.status === "new") waiting++;
    }
  }
  return { up, down, waiting };
}

const seconds = (ms: number | null) => (ms === null ? "—" : `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`);

/** Pure: one day of the Ask in a line: how many were answered and failed, how long they took, how many reads they made. */
export function dayWords(d: AskDay): string {
  const parts = [`${d.answered} answered`];
  if (d.failed) parts.push(`${d.failed} failed`);
  if (d.unavailable) parts.push(`${d.unavailable} without the assistant`);
  if (d.median_ms !== null) parts.push(`usually ${seconds(d.median_ms)}, slowest ${seconds(d.slowest_ms)}`);
  if (d.median_steps !== null) parts.push(`${d.median_steps} steps`);
  if (d.cost_usd) parts.push(`$${d.cost_usd} as the runs report it, for ${d.cost_answers} of ${d.answered} answers`);
  else if (d.answered) parts.push("cost not kept for this day");
  return parts.join(", ");
}

/** "Asked 12 times, no definition": the names Ask and Genie were asked for that nothing defines, most asked first. */
export function gapWords(g: Pick<GapRow, "kind" | "name" | "asked" | "defined">): string {
  const times = g.asked === 1 ? "once" : `${g.asked} times`;
  return `${g.name} (${g.kind}): asked ${times}, ${g.defined ? "defined since" : "no definition"}`;
}

/**
 * Admin, Data health, under "Are the answers right?": the thumbs people gave answers, the thumbs-down answers waiting for
 * a person (accept the fix, won't fix, or mark fixed: it feeds the nightly digest the improvement agents read), and
 * what was asked for that nothing defines. Only the owner and admins are given any of it; others see nothing here.
 */
export function AnswersRight() {
  const [entries, setEntries] = useState<FeedbackEntry[] | null>(null);
  const [gaps, setGaps] = useState<{ on: boolean; gaps: GapRow[] } | null>(null);
  const [seen, setSeen] = useState<AskObserved | null>(null);
  const [allDays, setAllDays] = useState(false);
  const [admin, setAdmin] = useState<boolean | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    const ctl = new AbortController();
    (async () => {
      try {
        const first = await getFeedback({ kind: "answer", limit: 1 }, ctl.signal);
        if (!first.all_readers) return setAdmin(false);
        setAdmin(true);
        const [all, g, o] = await Promise.all([getAllFeedback({ kind: "answer" }, ctl.signal), getGaps(ctl.signal), getObserved(ctl.signal)]);
        setEntries(all.entries);
        setGaps(g);
        setSeen(o);
      } catch (e) {
        if (!ctl.signal.aborted) setProblem(plainError(e));
      }
    })();
    return () => ctl.abort();
  }, []);
  const triage = async (e: FeedbackEntry, status: FeedbackStatus) => {
    try {
      const got = await setFeedbackStatus(e.id, status);
      setEntries((all) => (all ?? []).map((x) => (x.id === e.id ? { ...x, status: got.status } : x)));
    } catch (err) {
      setProblem(plainError(err));
    }
  };
  if (admin === false) return null;
  const t = thumbs(entries ?? []);
  const waiting = (entries ?? []).filter((e) => e.rating === -1 && e.status === "new");
  const open = (gaps?.gaps ?? []).filter((g) => !g.defined);
  return (
    <section className="space-y-3" aria-label="What people said about answers" data-testid="answers-right">
      <h3 className="text-[13px] font-semibold">What people said about answers</h3>
      {problem && <p className="text-[13px]" role="alert">{problem}</p>}
      {entries === null && !problem && <p className="text-[13px] text-muted">Reading</p>}
      {entries && (
        <p className="text-[13px]" data-testid="answers-thumbs">
          {t.up + t.down === 0 ? "No answer has been rated yet." : `${t.up} answers rated right, ${t.down} rated wrong${t.waiting ? `, ${t.waiting} of those waiting for a person` : ""}.`}
        </p>
      )}
      {seen && !seen.available && <p className="text-[13px] text-muted" data-testid="observed-off">How long answers took cannot be read now.</p>}
      {seen?.available && (
        <div className="space-y-1" data-testid="answers-observed">
          <h4 className="text-[13px] font-semibold">How the Ask has been doing</h4>
          <ul className="text-[13px]">
            {seen.days.filter((_, i) => allDays || i < 7).map((d) => <li key={d.day}><span className="num">{d.day}</span>: {dayWords(d)}</li>)}
          </ul>
          {seen.days.length > 7 && <button type="button" className={link} onClick={() => setAllDays((a) => !a)}>{allDays ? "Show the last 7 days" : `Show all ${seen.days.length} days`}</button>}
          {seen.failures.length > 0 && (
            <details className="text-[13px]">
              <summary className="cursor-pointer text-[12px] text-muted">{seen.failures.length} questions ended in an error</summary>
              <ul className="mt-1 divide-y divide-border">{seen.failures.map((f, i) => <li key={i} className="py-1">{formatDateTime(f.at)}{f.question ? `: ${f.question}` : ""} {f.error ? <span className="text-muted">({f.error})</span> : null}</li>)}</ul>
            </details>
          )}
        </div>
      )}
      {waiting.length > 0 && (
        <ul className="divide-y divide-border border-y border-border" data-testid="answers-waiting">
          {waiting.map((e) => (
            <li key={e.id} className="space-y-1 py-2 text-[13px]">
              <p className="font-medium">{e.question || "A question"}{e.review_requested ? <span className="ml-2 text-[12px] font-normal text-accent">review asked for</span> : null}</p>
              {e.note && <p className="text-muted">What was wrong: {e.note}</p>}
              {e.expected && <p className="text-muted">{e.review_requested ? "Proposed fix" : "The right answer is"}: {e.expected}</p>}
              <p className="flex flex-wrap items-center gap-3 text-[12px] text-muted">
                {formatDateTime(e.at)}{e.reader_name ? ` · ${e.reader_name}` : ""}
                <button type="button" className={link} onClick={() => triage(e, "accepted")}>{e.review_requested ? "Approve the fix" : "Accept the fix"}</button>
                <button type="button" className={link} onClick={() => triage(e, "wont_fix")}>Won&apos;t fix</button>
                <button type="button" className={link} onClick={() => triage(e, "fixed")}>Already fixed</button>
              </p>
            </li>
          ))}
        </ul>
      )}
      {gaps && !gaps.on && <p className="text-[13px] text-muted" data-testid="gaps-off">The list of what was asked for but not defined is not switched on on this server, so none is counted.</p>}
      {gaps?.on && (
        <div id="answers-gaps" className="space-y-1" data-testid="answers-gaps">
          <h4 className="text-[13px] font-semibold">Asked for, not defined</h4>
          {open.length === 0 ? <p className="text-[13px] text-muted">Nothing is waiting for a definition.</p> : (
            <ul className="text-[13px]">{open.map((g) => <li key={`${g.kind}:${g.name}`}>{gapWords(g)}</li>)}</ul>
          )}
        </div>
      )}
      {admin === true && <MeasureCandidates />}
    </section>
  );
}
