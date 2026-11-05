"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { cn } from "@/lib/cn";
import type { NoticeKind } from "@/lib/errors";
import { internalType, objectHref, plural, proseMoney, readable, recordKind, recordLabel, safeHref, typeLabel } from "@/lib/format";
import { fieldPairs, parseMarkdown, type Block } from "@/lib/markdown";
import { lastPick, rememberPick } from "@/lib/picks";
import { toggleSaved, useSaved } from "@/lib/saved";
import type { Answer, Fact, ObjectRef, Source, Understanding } from "@/lib/types";
import { CheckedList, ConflictCard, conflictField, Disclosure, FactList, FreshnessLabel, HiddenNotice, missingText, NotFoundSection, unconfirmed } from "./answer-parts";
import { ProfitabilityView } from "./cost-breakdown";
import { EvidenceTrailCard, trailNextAction, trailStepCount } from "./evidence-trail";
import { Feedback } from "./feedback";
import { ProposalCards } from "./proposal-cards";
import { Inlines, RichText } from "./rich-text";
import { CiteNumbers, SourceChips, SourceListRow } from "./source-chip";
import { ErrorNotice } from "./ui/notice";

/** Server prose as a reader reads it: codes and dates in words, amounts with their currency. */
/** Answer prose as the app writes it: money as money, every PO/SO number spaced one way
 * ("PO100095" -> "PO 100095"), and the late-order sentence said once, short. */
export const prose = (s: string) =>
  proseMoney(readable(s))
    .replace(/\b(PO|SO)(?=\d{3,}\b)/g, "$1 ")
    .replace(/(\d+ days? late)\. An open purchase order needed by (.+?) has no supplier confirmation/g, "$1, needed by $2; no supplier confirmation")
    // a run-on where the server joined two sentences without a stop ("…on its header Still to confirm: …")
    .replace(/([a-z0-9)])\s+(Still to confirm:)/g, "$1. $2")
    .replace(/\b(Longest late|Oldest):\s+(?=\d{5,}\b)/g, "$1: PO ")
    // the server's first-person stock lines, said as the software states a fact
    .replace(/\bI stopped researching at the time limit; the parts not yet checked are listed as open\./g, "Stopped at the time limit. Parts not checked are listed under Not found.")
    .replace(/\bI stopped researching at the time limit\b/g, "Stopped at the time limit")
    .replace(/\bI couldn't find records you can see that answer this\./g, "No records you can see answer this.")
    .replace(/\bI found related records but could not confirm an answer from what you can see\./g, "Related records found, but none you can see confirms an answer.")
    .replace(/\bI can't find that purchase order: /g, "")
    .replace(/\bI found no message (from the vendor )?naming /g, (_m, v: string | undefined) => `No message ${v ?? ""}names `)
    .replace(/, so I can't list late purchase orders yet/g, ", so late purchase orders can't be listed yet")
    .replace(/\bI couldn't confirm\b/g, "Couldn't confirm")
    .replace(/\s*\(it is on the late list on your home page\)/g, "");

/**
 * Server text that leaks an internal name ("purchaseOrder.renumbered to@portal.app", a bare
 * "PurchaseOrder", "Link key not served") says nothing to a reader: such a line is left out.
 */
export const internalWords = (s: string) =>
  /@[a-z]+\.app\b|\b[a-z]+[A-Z][A-Za-z]*\.[a-z]|^\s*[A-Z][a-z]+[A-Z][A-Za-z]+\.?\s*$|\bLink key not served\b|\b(?:PurchaseOrder|SalesOrder|AssemblyOrder)\b/.test(s);

export type TurnPhase = "thinking" | "streaming" | "done" | "error";

export interface Turn {
  id: string;
  question: string;
  phase: TurnPhase;
  understanding?: Understanding;
  status?: string;
  /** The plain-words progress steps so far ("Finding PO 90412", "Reading Business Central receipts"). */
  steps?: string[];
  /** The answer's first line, sent once the answer gate accepted it (before the full answer). */
  verdict?: string;
  sourcesFound?: number;
  streamed: string;
  answer?: Answer;
  error?: string;
  /** Set when the error is a server state (busy, ask switched off): shown as a banner. */
  notice?: NoticeKind;
}

/** What the person picked to disambiguate: an object id plus its label. */
export interface Pick {
  object_id: string;
  type: string;
  label: string;
}

function refHref(r: ObjectRef): string {
  return safeHref(r.open_url) ?? objectHref(r.type, r.object_id);
}

/** The answer's records as link chips: "PO 106093 · Viva Distribution (Canada) Ltd". */
function SubjectsLine({ refs, timeFrame }: { refs: ObjectRef[]; timeFrame?: string | null }) {
  const [all, setAll] = useState(false);
  if (refs.length === 0 && !timeFrame) return null;
  const shown = all ? refs : refs.slice(0, 4);
  return (
    <div className="flex flex-wrap items-baseline gap-x-1 text-meta text-muted" data-testid="understanding">
      <span>About</span>
      {shown.map((r, i) => (
        <span key={r.object_id}>
          <Link href={refHref(r)} className="text-accent hover:underline" data-testid="entity-link">
            {recordLabel(r.type, r.label)}
          </Link>
          {i < shown.length - 1 || refs.length > 4 ? "," : ""}
        </span>
      ))}
      {refs.length > 4 && (
        <button type="button" onClick={() => setAll((v) => !v)} className="text-accent hover:underline cursor-pointer" aria-expanded={all}>
          {all ? "fewer" : `${refs.length - 4} more`}
        </button>
      )}
      {timeFrame && <span className="text-muted">({timeFrame})</span>}
    </div>
  );
}

/** A choice's label as a reader reads it: "PO PO100109" once, ISO dates as dates, "po number" as "PO". */
export function choiceLabel(c: Pick): string {
  return readable(recordLabel(c.type, c.label))
    .replace(/\bfm po number\b/gi, "Portal PO")
    .replace(/\bpo number\b/gi, "PO")
    .replace(/\bPO (?=PO\d)/g, "");
}

function DidYouMean({ text, choices, onPick }: { text?: string; choices: Pick[]; onPick?: (p: Pick) => void }) {
  if (choices.length === 0) return null;
  const ids = choices.map((c) => c.object_id);
  const last = lastPick(ids);
  const ordered = last ? [...choices].sort((a, b) => Number(b.object_id === last) - Number(a.object_id === last)) : choices;
  return (
    <div className="text-row" data-testid="did-you-mean">
      <p className="mb-1.5">
        <span className="font-medium">Which one{text ? <> is “{text}”</> : null}?</span>
      </p>
      <div className="flex flex-wrap gap-1.5">
        {ordered.map((c) => (
          <button
            key={c.object_id}
            type="button"
            onClick={() => {
              rememberPick(ids, c.object_id);
              onPick?.(c);
            }}
            disabled={!onPick}
            data-last-pick={c.object_id === last ? "true" : undefined}
            className="min-h-8 max-w-full rounded-sm border border-control px-2.5 py-1 text-left text-meta hover:bg-surface-2 disabled:cursor-default disabled:opacity-60 cursor-pointer"
          >
            {choiceLabel(c)}
            <span className="font-normal text-muted"> · {typeLabel(c.type)}</span>
            {c.object_id === last && <span className="font-normal text-muted"> (last picked)</span>}
          </button>
        ))}
      </div>
    </div>
  );
}

function SaveButton({ question, href }: { question: string; href: string }) {
  const saved = useSaved();
  const key = `q:${question}`;
  const on = saved.some((s) => s.key === key);
  return (
    <button
      type="button"
      aria-pressed={on}
      aria-label={on ? "Remove from saved" : "Save this question"}
      title={on ? "Saved" : "Save"}
      onClick={() => toggleSaved({ key, kind: "question", label: question, href })}
      className={cn("rounded-sm px-2 py-1 text-meta hover:bg-surface-2 cursor-pointer", on ? "font-medium text-fg" : "text-accent")}
    >
      {on ? "Saved" : "Save"}
    </button>
  );
}

/** Seconds since mount, ticking while `on`. */
function useElapsed(on: boolean): number {
  const [s, setS] = useState(0);
  useEffect(() => {
    if (!on) return;
    const t0 = Date.now();
    const id = setInterval(() => setS(Math.round((Date.now() - t0) / 1000)), 1000);
    return () => clearInterval(id);
  }, [on]);
  return s;
}

/** Three steps that tick as the stream's events arrive. */
function Progress({ turn }: { turn: Turn }) {
  const elapsed = useElapsed(true);
  const understood = (turn.understanding?.entities ?? []).filter((e) => e.resolved && !e.ambiguous).map((e) => recordLabel(e.resolved!.type, e.resolved!.label));
  // the work's own steps, in plain words, each once; the latest is the one in progress
  const work = (turn.steps ?? []).filter((t) => t !== "Reading your question" && t !== "Writing the answer").map((t) => readable(t).replace(/…$/, ""));
  const writing = turn.phase === "streaming" || !!turn.verdict;
  const found = turn.sourcesFound ? ` (${plural(turn.sourcesFound, "source")} found)` : "";
  const steps: { label: string; state: "done" | "active" | "todo" }[] = [
    ...(understood.length && !work.length ? [{ label: `Found ${understood.slice(0, 2).join(", ")}`, state: "done" as const }] : []),
    ...(work.length
      ? work.map((label, i) => ({
          label: i === work.length - 1 ? `${label}${writing ? "" : "…"}${found}` : label,
          state: (i < work.length - 1 || writing ? "done" : "active") as "done" | "active",
        }))
      : [
          {
            label: `Searching BC, the Portal, mail and files${found}`,
            state: (writing ? "done" : "active") as "done" | "active" | "todo",
          },
        ]),
    ...(writing ? [{ label: "Writing", state: "active" as const }] : []),
  ];
  return (
    <div className="space-y-1" role="status" aria-live="polite" data-testid="progress">
      <ol className="space-y-0.5 text-meta" data-pending="">
        {steps.map((s, i) => (
          <li key={i} className={s.state === "done" ? "text-muted" : "text-fg"}>
            {s.label}
            {s.state === "active" ? "…" : ""}
            <span className="sr-only">{s.state === "done" ? "(done)" : s.state === "active" ? "(in progress)" : ""}</span>
          </li>
        ))}
      </ol>
      <p className="text-meta tabular-nums text-faint">
        {elapsed} s{elapsed >= 15 && turn.phase === "thinking" ? ". Questions about one order take about a minute." : ""}
      </p>
    </div>
  );
}

/** The answer's own "What I couldn't confirm: …" line (any case, bulleted or bold). */
const MISSING_LINE = /^\s*(?:[-*•]\s+)?(?:\*\*)?What I couldn['’]t confirm\b(?:\*\*)?:?(?:\*\*)?\s*/i;

/**
 * The text without its "What I couldn't confirm" line, and that line's words (null when
 * absent, or when it only points at the list below).
 */
export function missingLine(text: string): { text: string; missing: string | null } {
  let missing: string | null = null;
  const kept = text.split("\n").filter((line) => {
    if (!MISSING_LINE.test(line)) return true;
    const said = line.replace(MISSING_LINE, "").replace(/\*\*/g, "").trim();
    if (!missing && said && !/^from a source you can open is listed below/i.test(said)) missing = said.replace(/^./, (c) => c.toUpperCase());
    return false;
  });
  return { text: kept.join("\n").trim(), missing };
}

/**
 * The direct answer and what moves out of it: at most two paragraphs lead (18 px); later
 * paragraphs and bullets become fact lines. A "Label: value" dump is never the lead: its
 * pairs become facts and a short lead is composed from the record. A verdict line followed
 * by bullets is the answer's own shape: the verdict leads, never read as a field dump.
 */
export function splitAnswer(text: string, subject?: ObjectRef | null): { lead: Block[]; rest: Block[]; composed: string | null } {
  const shaped = /^[^\n\-*•\d][^\n]*\n\s*[-*•]\s/.test(text.trim());
  const pairs = shaped ? null : fieldPairs(text);
  if (pairs) {
    const who = subject ? recordLabel(subject.type, subject.label) : null;
    const said = pairs.slice(0, 3).map(([k, v]) => `${k.toLowerCase()} ${v}`);
    const composed = `${who ? `${who}: ` : ""}${said.join(", ")}.`;
    return { lead: [], rest: [{ kind: "ul", items: pairs.map(([k, v]) => [{ text: `${k}: ${v}` }]) }], composed };
  }
  const blocks = parseMarkdown(text);
  const leadCount = blocks[0]?.kind === "p" && blocks[1]?.kind === "p" ? 2 : 1;
  return { lead: blocks.slice(0, leadCount), rest: blocks.slice(leadCount), composed: null };
}

// Figures and record numbers in a line (commas out of numbers), and its longer words.
const norm = (s: string) => prose(s).toLowerCase().replace(/(\d),(?=\d{3}\b)/g, "$1");
const figures = (s: string) => new Set((norm(s).match(/[a-z$]*\d[\w.\-/]*/g) ?? []).map((t) => t.replace(/[.\-/]+$/, "")));
const STOP = new Set(["that", "this", "with", "from", "have", "been", "were", "which", "their", "there", "about", "still", "they"]);
const longWords = (s: string) => new Set((norm(s).match(/[a-z]{4,}/g) ?? []).filter((w) => !STOP.has(w)));

/**
 * Which fact a bullet of the answer states, by index (or null): the fact whose every figure
 * the bullet writes, else one sharing at least half of its words. A bullet only borrows the
 * evidence chips of the gated fact it restates; it never adds one.
 */
export function bulletFact(item: string, facts: Fact[], used: Set<number>): number | null {
  const fig = figures(item);
  const wd = longWords(item);
  let best: number | null = null;
  let score = 0;
  facts.forEach((f, i) => {
    if (used.has(i)) return;
    const ff = [...figures(f.text)];
    let sc = 0;
    if (ff.length && ff.every((t) => fig.has(t))) sc = 2 + ff.length;
    else if (ff.length === 0) {
      // a fact with no figure: by its words; one whose figure the bullet lacks never matches
      const fw = longWords(f.text);
      const shared = [...fw].filter((w) => wd.has(w)).length;
      const j = shared / Math.max(1, new Set([...fw, ...wd]).size);
      if (j >= 0.5) sc = j;
    }
    if (sc > score) {
      score = sc;
      best = i;
    }
  });
  return best;
}

/** Blocks after the lead, one fact line each; a bullet that restates a fact carries its chips. */
function restLines(rest: Block[], facts: Fact[], record: (s: Source) => string | null): { lines: React.ReactNode[]; used: Set<number> } {
  const used = new Set<number>();
  const lines = rest.flatMap((b, i) =>
    b.kind === "p"
      ? [<Inlines key={`p${i}`} xs={b.inlines} map={prose} />]
      : b.items.map((it, j) => {
          const k = bulletFact(it.map((x) => x.text).join(""), facts, used);
          if (k !== null) used.add(k);
          const f = k !== null ? facts[k] : null;
          return (
            <span key={`l${i}-${j}`} data-testid="answer-bullet">
              <Inlines xs={it} map={prose} />
              {f && unconfirmed(f) && <span className="text-muted"> (not confirmed)</span>}
              {f && (
                <>
                  {" "}
                  <SourceChips sources={f.sources} record={record} />
                </>
              )}
            </span>
          );
        }),
  );
  return { lines, used };
}

/** One late-order fact: "PO100095: 114 days late. An open purchase order needed by 14 Jul 2026 has no supplier confirmation." */
const LATE_FACT = /^(?:PO\s*)?(\d[\w-]*):\s*(\d+) days? late\.\s*An open purchase order needed by (.+?)( has no supplier confirmation)?\.?$/i;

export function lateRows(facts: Fact[]): { po: string; days: number; due: string; unconfirmed: boolean; fact: Fact }[] | null {
  if (facts.length < 3) return null;
  const rows = facts.map((f) => {
    const m = LATE_FACT.exec(readable(f.text).trim());
    return m ? { po: `PO ${m[1]}`, days: Number(m[2]), due: m[3], unconfirmed: !!m[4], fact: f } : null;
  });
  return rows.every(Boolean) ? (rows as NonNullable<(typeof rows)[number]>[]) : null;
}

/** A list answer about late orders as a ledger (one row per order, its source beside it), not twelve sentences. */
function LateLedger({ rows }: { rows: NonNullable<ReturnType<typeof lateRows>> }) {
  const allUnconfirmed = rows.every((r) => r.unconfirmed);
  return (
    <div className="space-y-2" data-testid="fact-list">
      <div className="overflow-x-auto">
        <table className="w-full text-row" aria-label="Late purchase orders">
          <thead>
            <tr className="h-8 border-y border-border-strong bg-bg-subtle text-left text-meta text-muted">
              <th scope="col" className="px-2 font-medium">PO</th>
              <th scope="col" className="px-2 font-medium">Needed by</th>
              <th scope="col" className="px-2 text-right font-medium">Days late</th>
              {!allUnconfirmed && <th scope="col" className="px-2 font-medium">Supplier date</th>}
              <th scope="col" className="hidden px-2 text-right font-medium sm:table-cell">Source</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={`${r.po}-${i}`} className="h-9 border-b border-border">
                <td className="whitespace-nowrap px-2">{r.po}</td>
                <td className="whitespace-nowrap px-2 tabular-nums">{r.due}</td>
                <td className="whitespace-nowrap px-2 text-right tabular-nums text-late">{r.days}</td>
                {!allUnconfirmed && <td className="px-2 text-meta text-muted">{r.unconfirmed ? "None" : "On record"}</td>}
                <td className="hidden px-2 text-right sm:table-cell">
                  <SourceChips sources={r.fact.sources} max={1} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {allUnconfirmed && <p className="text-meta text-muted">None of these has a supplier date on record.</p>}
    </div>
  );
}

/** Citation numbers for an answer's sources, in the order a reader meets them. */
function citationsOf(a: Answer | undefined, facts: Fact[], used: Set<number>): { numbers: ReadonlyMap<string, number>; cited: Source[] } {
  const numbers = new Map<string, number>();
  const cited: Source[] = [];
  if (!a) return { numbers, cited };
  const add = (src: Source | null | undefined) => {
    if (!src?.handle || numbers.has(src.handle)) return;
    numbers.set(src.handle, numbers.size + 1);
    cited.push(src);
  };
  const order = [...used, ...facts.map((_, i) => i).filter((i) => !used.has(i))];
  for (const i of order) for (const src of facts[i]?.sources ?? []) add(src);
  for (const c of a.conflicts ?? []) for (const v of c.values) add(v.source);
  return { numbers, cited };
}

/** The answer's sources, numbered as the markers on its claims; each opens its evidence. */
function SourcesList({ sources }: { sources: Source[] }) {
  const [all, setAll] = useState(false);
  if (sources.length === 0) return null;
  const shown = all ? sources : sources.slice(0, 6);
  return (
    <section className="border-t border-border pt-3" aria-label="Sources" data-testid="answer-sources">
      <h3 className="text-meta font-semibold text-fg">Sources</h3>
      <ol className="mt-1.5 grid grid-cols-1 gap-x-6 sm:grid-cols-2">
        {shown.map((src, i) => (
          <li key={src.handle} className="min-w-0">
            <SourceListRow source={src} n={i + 1} />
          </li>
        ))}
      </ol>
      {sources.length > shown.length && (
        <button type="button" onClick={() => setAll(true)} className="mt-1 text-meta text-accent hover:underline cursor-pointer">
          All {sources.length} sources
        </button>
      )}
    </section>
  );
}

/** Questions to ask next, as chips that ask them. */
function FollowUps({ items, onAsk }: { items: string[]; onAsk?: (q: string) => void }) {
  if (items.length === 0) return null;
  return (
    <div className="text-row" aria-label="Ask next" data-testid="follow-ups">
      <p className="text-meta text-muted">Ask next</p>
      <ul>
        {items.map((q) => (
          <li key={q}>
            <button
              type="button"
              onClick={() => onAsk?.(q)}
              disabled={!onAsk}
              className="py-0.5 text-left text-accent hover:underline disabled:cursor-default disabled:text-muted disabled:no-underline cursor-pointer"
            >
              {q}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** One line of confidence: answered from N records, partly answered and why, not on record. */
function Confidence({ a }: { a: Answer }) {
  const records = new Set((a.facts ?? []).flatMap((f) => f.sources.map((s) => s.handle))).size;
  if (a.assistant_unavailable)
    return (
      <p className="text-meta text-risk" data-testid="outcome-badge">
        Assistant unavailable. The facts below come straight from the records.
      </p>
    );
  if (a.outcome === "partial" && (a.did_you_mean ?? []).length > 0 && (a.facts ?? []).length === 0)
    return (
      <p className="text-meta text-muted" data-testid="outcome-badge">
        Pick one to get the answer
      </p>
    );
  if (a.outcome === "partial") {
    const stale = (a.not_found ?? []).find((n) => /^(?:stale source|source stale)$/i.test(n.where_it_would_live.trim()));
    const why = (a.conflicts ?? [])[0]
      ? `the systems disagree on ${conflictField(a.conflicts![0])}`
      : (a.not_found ?? []).some((n) => /conflicting sources/i.test(n.where_it_would_live))
        ? "the sources disagree"
        : stale
          ? missingText(stale).replace(/\.$/, "") // "Portal data is out of date": the system keeps its capital
          : (a.not_found ?? []).find((n) => !internalWords(n.what))
          ? `${missingText((a.not_found ?? []).find((n) => !internalWords(n.what))!).replace(/[.:].*$/, "").replace(/^./, (c) => c.toLowerCase())} isn't on record`
          : null;
    return (
      <p className="text-meta text-risk" data-testid="outcome-badge">
        Partly answered{why ? `: ${why}` : ""}
      </p>
    );
  }
  if (a.outcome === "not_found" || a.outcome === "abstained")
    return (
      <p className="text-meta text-muted" data-testid="outcome-badge">
        Not on record{(a.not_found ?? [])[0] ? `: ${readable(a.not_found![0].where_it_would_live)}` : ""}
      </p>
    );
  if (a.outcome === "refused")
    return (
      <p className="text-meta text-muted" data-testid="outcome-badge">
        Can&apos;t answer this
      </p>
    );
  if (records === 0) return null;
  return (
    <p className="text-meta text-muted" data-testid="confidence">
      From {plural(records, "record")}
    </p>
  );
}

/** "Who to ask": from the trail's next step, else where the first missing item would live. */
function whoToAsk(a: Answer): string | null {
  const next = trailNextAction(a.trail);
  if (next) return readable(next.text);
  const nf = (a.not_found ?? [])[0];
  if (nf && /\bask\b/i.test(nf.where_it_would_live)) {
    const m = /\bask ([^.;]+)/i.exec(readable(nf.where_it_would_live));
    if (m) return `Ask ${m[1].trim()}, about ${readable(nf.what).replace(/^./, (c) => c.toLowerCase())}.`;
  }
  return null;
}

export function AnswerCard({ turn, onPick, onRetry, onAsk }: { turn: Turn; onPick?: (p: Pick) => void; onRetry?: () => void; onAsk?: (q: string) => void }) {
  const a = turn.answer;
  const busy = turn.phase === "thinking" || turn.phase === "streaming";
  const u = turn.understanding;
  const understood = (u?.entities ?? []).filter((e) => e.resolved && !e.ambiguous).map((e) => e.resolved!);
  const subjects = a ? (a.subjects ?? []) : [];
  const refs = subjects.length ? subjects : understood;
  const ambiguous = (a?.did_you_mean ?? []).length > 0 ? [] : (u?.entities ?? []).filter((e) => e.ambiguous && (e.candidates ?? []).filter((c) => !internalType(c.type)).length > 1);
  const primary = subjects[0] ?? null;
  // before the words stream, the gate-accepted first line stands in for the answer
  const shownText = a ? a.direct_answer : turn.streamed || turn.verdict || "";
  const { text, missing: missingSaid } = missingLine(shownText ?? "");
  const { lead, rest, composed } = splitAnswer(text, primary);
  // a lead longer than a short paragraph is read, not displayed: set in the body face, not the large serif
  const leadChars = lead.reduce((n, b) => n + (b.kind === "p" ? b.inlines.reduce((m, x) => m + x.text.length, 0) : 0), 0);
  const longLead = leadChars > 320;
  const [fullLead, setFullLead] = useState(false);
  // the answer stopped or failed before it landed: what was shown early (the first line or
  // streamed words) was never confirmed, so it is muted and said as such beside the error
  const unfinished = !a && turn.phase === "error";
  // a source of the answer's one subject is named by it ("PO 106093"), not by its system
  const record = (s: Source): string | null => {
    if (subjects.length !== 1) return null;
    const kind = recordKind(s.handle, s.label);
    return kind && kind === typeLabel(subjects[0].type) && s.system !== "Email" ? recordLabel(subjects[0].type, subjects[0].label) : null;
  };
  const notFound = (a?.not_found ?? []).filter((n) => !internalWords(`${n.what} ${n.where_it_would_live}`));
  const missingCount = notFound.length + (a?.hidden && (a.hidden.count === null || (a.hidden.count ?? 0) > 0) ? 1 : 0);
  const steps = trailStepCount(a?.trail);
  // the work's own steps, kept after the answer lands: how it was pulled
  const pulled = (turn.steps ?? []).filter((t) => t !== "Reading your question").map((t) => readable(t).replace(/…$/, ""));
  const ask = a ? whoToAsk(a) : null;
  const sourcesN = a ? Math.max((a.freshness?.sources ?? []).length, new Set((a.facts ?? []).map((f) => f.sources[0]?.system)).size) : 0;
  const usedSystems = a ? [...new Set((a.facts ?? []).flatMap((f) => f.sources.map((s) => s.system)))] : [];
  const facts = a?.facts ?? [];
  // a field dump's pairs stay plain lines; the answer's own bullets carry their facts' chips
  const { lines, used } = restLines(rest, composed ? [] : facts, record);
  const otherFacts = facts.filter((_, i) => !used.has(i));
  // one number per cited source, in reading order: the bullets' facts, then the rest, then
  // the systems that disagree; each claim carries its numbers and the list below names them
  const { numbers, cited } = citationsOf(a, facts, used);
  // a list of late orders reads as a ledger when every fact is one late-order line
  const ledger = typeof a?.list_total === "number" && lines.length === 0 ? lateRows(otherFacts) : null;
  // what the answer couldn't confirm, in one line: its own words, else the gaps it lists
  const gaps = (a?.not_found ?? []).filter((n) => !internalWords(n.what)).map((n) => missingText(n).replace(/[.:].*$/, "")).filter(Boolean);
  const missing = a ? ((missingSaid && !internalWords(missingSaid) ? missingSaid : null) ?? (gaps.length ? gaps.slice(0, 2).join("; ") + (gaps.length > 2 ? `; ${gaps.length - 2} more` : "") : null)) : null;
  return (
    <CiteNumbers.Provider value={a ? numbers : null}>
    <article className="space-y-3" aria-busy={busy} data-testid="answer-card">
      <SubjectsLine refs={refs} timeFrame={u?.time_frame} />
      {ambiguous.map((e, i) => (
        <DidYouMean
          key={i}
          text={e.text}
          choices={(e.candidates ?? [])
            .filter((c) => !internalType(c.type))
            .slice(0, 5)
            .map((c) => ({ object_id: c.object_id, type: c.type, label: c.label }))}
          onPick={onPick}
        />
      ))}

      {busy && <Progress turn={turn} />}
      {a && <Confidence a={a} />}

      {composed || lead.length > 0 ? (
        <div
          className={cn("break-words", longLead ? "max-w-[68ch] space-y-3 text-body" : "text-answer", unfinished ? "text-muted" : "text-fg")}
          data-testid="direct-answer"
          data-unconfirmed={unfinished || undefined}
        >
          {unfinished && (
            <p className="mb-1 text-meta font-medium text-muted" data-testid="early-reading">
              Early reading, not confirmed
            </p>
          )}
          {!a && !turn.streamed && turn.verdict ? (
            <p data-testid="verdict">{prose(turn.verdict)}</p>
          ) : composed ? (
            <p>{composed}</p>
          ) : (
            <RichText blocks={lead} map={prose} className={cn(longLead && !fullLead && turn.phase !== "streaming" && "line-clamp-4")} />
          )}
          {longLead && turn.phase !== "streaming" && (
            <button type="button" onClick={() => setFullLead((v) => !v)} aria-expanded={fullLead} className="mt-1 text-meta font-medium text-accent hover:underline cursor-pointer">
              {fullLead ? "Show less" : "Show all"}
            </button>
          )}
    
        </div>
      ) : turn.phase === "thinking" ? (
        null
      ) : null}

      {turn.phase === "error" && (
        <ErrorNotice
          message={
            turn.notice || /^Stopped\./.test(turn.error ?? "")
              ? (turn.error ?? "")
              : `This answer didn't finish. ${(turn.error ?? "Try again.").replace(/^Server error\.\s*/, "")}`
          }
          tone={turn.notice ? "notice" : "error"}
          onRetry={turn.notice === "off" ? undefined : onRetry}
        />
      )}

      {a && (
        <div className="space-y-4">
          <DidYouMean
            choices={(a.did_you_mean ?? [])
              .filter((r) => !internalType(r.type))
              .slice(0, 6)
              .map((r) => ({ object_id: r.object_id, type: r.type, label: r.label }))}
            onPick={onPick}
          />
          {ledger ? (
            <LateLedger rows={ledger} />
          ) : (
            <FactList facts={otherFacts} record={record} extra={lines} max={typeof a.list_total === "number" ? undefined : a.profitability ? 4 : composed ? 8 : 5} />
          )}
          <SourcesList sources={cited} />
          {missing && (
            <p className="text-row text-muted" data-testid="could-not-confirm">
              <span className="font-medium text-fg">Not found: </span>
              {prose(missing).replace(/\.?$/, ".")}
            </p>
          )}
          <FollowUps items={(a.follow_ups ?? []).filter((q) => !internalWords(q))} onAsk={onAsk} />
          {typeof a.list_total === "number" && a.list_total > (a.facts ?? []).length && (
            <Link href="/late" className="inline-flex text-row text-accent hover:underline" data-testid="show-all">
              {(a.facts ?? []).length} of {a.list_total} shown. Show all
            </Link>
          )}
          {a.profitability && <ProfitabilityView p={a.profitability} />}
          {(a.conflicts ?? []).map((c, i) => (
            <ConflictCard key={i} conflict={c} />
          ))}
          {(ask || primary) && (
            <div className="flex flex-wrap items-center justify-between gap-3 py-1" data-testid="who-to-ask">
              <p className="min-w-0 flex-1 text-row">
                {ask ? (
                  <>
                    <span className="text-muted">Ask: </span>
                    {ask.replace(/^Ask /, "")}
                  </>
                ) : null}
              </p>
              {primary && (
                <Link
                  href={refHref(primary)}
                  className="inline-flex h-8 items-center rounded-sm border border-control px-3 text-row hover:bg-surface-2"
                  data-testid="subject-link"
                >
                  Open {recordLabel(primary.type, primary.label)}
                </Link>
              )}
            </div>
          )}
          <div className="border-b border-border">
            <Disclosure label={`Systems searched (${sourcesN})`} testId="what-checked" defaultOpen={a.outcome === "not_found"}>
              <CheckedList freshness={a.freshness} facts={a.facts} />
              {pulled.length > 0 && (
                <p className="mt-2 text-meta text-muted" data-testid="how-pulled">
                  Steps: {pulled.join("; ")}.
                </p>
              )}
            </Disclosure>
            {missingCount > 0 && (
              <Disclosure label={`Not found (${notFound.length || missingCount})`} testId="whats-missing" defaultOpen={a.outcome === "partial"}>
                <NotFoundSection items={notFound} bare />
                <HiddenNotice hidden={a.hidden} />
              </Disclosure>
            )}
            {steps > 0 && (
              <Disclosure label={`Evidence (${steps})`} testId="how-we-know">
                <EvidenceTrailCard trail={a.trail} bare showNext={false} />
              </Disclosure>
            )}
            {(a.proposals ?? []).length > 0 && (
              <Disclosure label={`Drafts (${(a.proposals ?? []).length})`} testId="answer-proposals">
                <ProposalCards proposals={a.proposals} />
              </Disclosure>
            )}
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <FreshnessLabel freshness={a.freshness} used={usedSystems} />
            <div className="flex items-center gap-1">
              <SaveButton question={turn.question} href={`/ask?q=${encodeURIComponent(turn.question)}`} />
              <Feedback answerId={a.answer_id} />
            </div>
          </div>
        </div>
      )}
    </article>
    </CiteNumbers.Provider>
  );
}

