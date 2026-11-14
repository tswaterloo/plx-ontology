"use client";

import { ArrowRight, ArrowUpRight, Bookmark, Check, ChevronRight, CornerDownRight } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { cn } from "@/lib/cn";
import type { NoticeKind } from "@/lib/errors";
import type { TurnRequest } from "@/lib/thread";
import { formatDate, internalType, objectHref, plural, proseMoney, readable, recordKind, recordLabel, safeHref, SYSTEM_LABEL, typeLabel } from "@/lib/format";
import { fieldPairs, parseMarkdown, type Block } from "@/lib/markdown";
import { lastPick, rememberPick } from "@/lib/picks";
import { toggleSaved, useSaved } from "@/lib/saved";
import type { Answer, Fact, ObjectRef, Source, Understanding } from "@/lib/types";
import { CheckedList, ConflictCard, conflictField, Disclosure, FactList, FreshnessLabel, HiddenNotice, missingText, NotFoundSection, unconfirmed } from "./answer-parts";
import { ProfitabilityView } from "./cost-breakdown";
import { EvidenceTrailCard, trailNextAction, trailStepCount } from "./evidence-trail";
import { Feedback } from "./feedback";
import { ProposalCards } from "./proposal-cards";
import { AskDrafts, AttachmentNotes } from "./ask-drafts";
import { InstructionPart, type InstructAsk } from "./instruction-part";
import { AssumptionLine, ClarifyPart, type ClarifyReply } from "./clarify-part";
import { AskOrganizer } from "./ask-organize";
import { Inlines, RichText } from "./rich-text";
import { CiteNumbers, EvidenceDrawer, SourceChips, sourceName } from "./source-chip";
import { lateFigure, OpenRing, StatusGlyph, SystemCode } from "./ui/glyphs";
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
  /** The request this turn was sent with, so Retry resends it whole. */
  request?: TurnRequest;
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

/** The answer's records as a line of links: "PO 106093 · Viva Distribution (Canada) Ltd". */
function SubjectsLine({ refs, timeFrame, className }: { refs: ObjectRef[]; timeFrame?: string | null; className?: string }) {
  const [all, setAll] = useState(false);
  if (refs.length === 0 && !timeFrame) return null;
  const shown = all ? refs : refs.slice(0, 4);
  return (
    <div className={cn("-ml-1.5 flex flex-wrap items-center gap-x-0.5 gap-y-1 text-[13px]", className)} data-testid="understanding">
      {shown.map((r) => (
        <Link key={r.object_id} href={refHref(r)} className="inline-flex h-7 max-w-full items-center gap-2 rounded-lg px-1.5 hover:bg-hover" data-testid="entity-link">
          <span className="truncate">{recordLabel(r.type, r.label)}</span>
        </Link>
      ))}
      {refs.length > 4 && (
        <button type="button" onClick={() => setAll((v) => !v)} className="inline-flex h-7 items-center rounded-lg px-1.5 text-muted hover:bg-hover hover:text-fg cursor-pointer" aria-expanded={all}>
          {all ? "Fewer" : `${refs.length - 4} more`}
        </button>
      )}
      {timeFrame && <span className="px-1.5 text-muted">{timeFrame}</span>}
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
    <section data-testid="did-you-mean">
      <SectionTitle>Which one{text ? <> is “{text}”</> : null}?</SectionTitle>
      <ul className="-mx-2">
        {ordered.map((c) => (
          <li key={c.object_id}>
            <button
              type="button"
              onClick={() => {
                rememberPick(ids, c.object_id);
                onPick?.(c);
              }}
              disabled={!onPick}
              data-last-pick={c.object_id === last ? "true" : undefined}
              className="group flex min-h-9 w-full items-center gap-3 rounded-lg px-2 py-1 text-left text-[13px] hover:bg-hover disabled:cursor-default disabled:opacity-60 cursor-pointer"
            >
              <span className="min-w-0 flex-1">
                <span className="font-medium">{choiceLabel(c)}</span>
                <span className="text-muted"> · {typeLabel(c.type)}</span>
                {c.object_id === last && <span className="text-muted"> (last picked)</span>}
              </span>
              <ArrowRight className="size-3.5 shrink-0 text-faint opacity-0 group-hover:opacity-100" aria-hidden />
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function SectionTitle({ children, aside }: { children: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <div className="mb-2.5 flex items-baseline justify-between gap-3">
      <h3 className="text-[12px] leading-4 font-medium text-muted">{children}</h3>
      {aside}
    </div>
  );
}

/** A quiet button for the answer's own toolbar (Save, Correct, Wrong). */
export const quietButton = "inline-flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-[13px] text-muted hover:bg-hover hover:text-fg disabled:opacity-50 cursor-pointer [&_svg]:size-[15px]";

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
      className={cn(quietButton, on && "text-fg")}
    >
      <Bookmark className={cn(on && "fill-current")} strokeWidth={2} aria-hidden />
      <span className="max-sm:hidden">{on ? "Saved" : "Save"}</span>
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

/** A small ring that turns while a step is in progress. */
function Spinner() {
  return (
    <svg viewBox="0 0 14 14" className="size-3.5 shrink-0 animate-spin text-muted" aria-hidden>
      <circle cx="7" cy="7" r="5.5" fill="none" stroke="var(--border-strong)" strokeWidth="1.5" />
      <path d="M7 1.5a5.5 5.5 0 0 1 5.5 5.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

const SEARCHED = ["BC", "Portal", "Email", "File"];

/** The work's own steps as they arrive: done ones in grey with a tick, the current one turning. */
function Progress({ turn }: { turn: Turn }) {
  const elapsed = useElapsed(true);
  const understood = (turn.understanding?.entities ?? []).filter((e) => e.resolved && !e.ambiguous).map((e) => recordLabel(e.resolved!.type, e.resolved!.label));
  // the work's own steps, in plain words, each once; the latest is the one in progress
  const work = (turn.steps ?? []).filter((t) => t !== "Reading your question" && t !== "Writing the answer").map((t) => readable(t).replace(/…$/, ""));
  const writing = turn.phase === "streaming" || !!turn.verdict;
  const found = turn.sourcesFound ? plural(turn.sourcesFound, "source") : null;
  const steps: { label: string; state: "done" | "active"; tiles?: boolean }[] = [
    ...(understood.length && !work.length ? [{ label: `Found ${understood.slice(0, 2).join(", ")}`, state: "done" as const }] : []),
    ...(work.length
      ? work.map((label, i) => ({ label, state: (i < work.length - 1 || writing ? "done" : "active") as "done" | "active" }))
      : [{ label: "Searching", state: (writing ? "done" : "active") as "done" | "active", tiles: true }]),
    ...(writing ? [{ label: "Writing", state: "active" as const }] : []),
  ];
  return (
    <div role="status" aria-live="polite" data-testid="progress">
      <ol className="space-y-1.5 text-[13px] leading-5" data-pending="">
        {steps.map((s, i) => (
          <li key={i} className={cn("flex items-center gap-2.5", s.state === "done" ? "text-muted" : "text-fg")}>
            {s.state === "active" ? <Spinner /> : <Check className="size-3.5 shrink-0 text-muted" strokeWidth={2.2} aria-hidden />}
            <span className="min-w-0 truncate">{s.label}</span>
            {s.tiles && (
              <span className="flex shrink-0 gap-1" aria-hidden>
                {SEARCHED.map((x) => (
                  <SystemCode key={x} system={x} size={14} />
                ))}
              </span>
            )}
            {i === steps.length - 1 && s.label !== "Writing" && found && <span className="shrink-0 text-muted">{found}</span>}
            {s.state === "active" && <span className="shrink-0 text-[12px] tabular-nums text-faint">{elapsed} s</span>}
            <span className="sr-only">{s.state === "done" ? "(done)" : "(in progress)"}</span>
          </li>
        ))}
      </ol>
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

/** The headings the server puts over an answer's uncited part (``operating.general_answer``). */
export const GENERAL_HEADINGS = ["General knowledge (not from PLX records)", "From your attachment (not from PLX records)"] as const;

/**
 * The answer's words cut at the uncited part's heading: the company part (the cited path's
 * own words) and the general part with its heading, which never takes a source chip or a
 * fact line. The first heading wins: anything after it is shown as uncited.
 */
export function splitGeneral(text: string): { company: string; general: { heading: string; text: string } | null } {
  let at = -1;
  let heading = "";
  for (const h of GENERAL_HEADINGS) {
    const i = text.startsWith(`${h}\n`) || text === h ? 0 : text.indexOf(`\n${h}\n`);
    const start = i > 0 ? i + 1 : i;
    if (start >= 0 && (at < 0 || start < at)) {
      at = start;
      heading = h;
    }
  }
  if (at < 0) return { company: text, general: null };
  return { company: text.slice(0, at).trim(), general: { heading, text: text.slice(at + heading.length).trim() } };
}

/** The uncited part, in its own labelled section: plain text, no chips, never a fact line. */
function GeneralPart({ heading, text }: { heading: string; text: string }) {
  if (!text) return null;
  return (
    <section className="space-y-2 border-l border-[color-mix(in_srgb,var(--fg)_15%,transparent)] pl-4" aria-label={heading} data-testid="general-part">
      <h3 className="text-[12px] leading-4 font-medium text-muted">{heading}</h3>
      <RichText blocks={parseMarkdown(text)} map={prose} className="max-w-[68ch] space-y-2 text-[13px] leading-[21px] text-fg" />
    </section>
  );
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

// Figures and record numbers in a line (commas out of numbers).
const norm = (s: string) => prose(s).toLowerCase().replace(/(\d),(?=\d{3}\b)/g, "$1");
const figures = (s: string) => new Set((norm(s).match(/[a-z$]*\d[\w.\-/]*/g) ?? []).map((t) => t.replace(/[.\-/]+$/, "")));

// The words a fact says besides its figures: what the figure is (quantity, late, price,
// due), never grammar. Three letters up, so "PO" and "of" never count.
const FILLER = new Set(["the", "and", "for", "has", "have", "had", "was", "were", "are", "its", "this", "that", "with", "from", "not", "yet", "per", "all", "any", "one", "our", "but"]);
const words = (s: string) =>
  new Set(
    (norm(s).replace(/[a-z$]*\d[\w.\-/]*/g, " ").match(/[a-z]{3,}/g) ?? [])
      .filter((w) => !FILLER.has(w))
      .map((w) => w.replace(/(?:ies|es|s)$/, "")),
  );

/**
 * Which fact a bullet of the answer's cited part states, by index (or null): the fact
 * whose every figure the bullet writes and, when the fact says what its figure is (its
 * words: "quantity", "days late"), one of those words too. Words alone never match, and
 * the same figures never lend another property's or record's evidence (WEB-027: "PO123
 * quantity 12" never cites "PO123 is 12 days late"). Two facts the bullet matches equally
 * well lend nothing: no citation beats a guessed one. A bullet only borrows the evidence
 * chips of the gated fact it restates; it never adds one, and the uncited part
 * (``splitGeneral``) never comes here.
 */
export function bulletFact(item: string, facts: Fact[], used: Set<number>): number | null {
  const fig = figures(item);
  const said = words(item);
  let best: number | null = null;
  let score = 0;
  let tied = false;
  facts.forEach((f, i) => {
    if (used.has(i)) return;
    const ff = [...figures(f.text)];
    if (!ff.length || !ff.every((t) => fig.has(t))) return;
    const fw = [...words(f.text)];
    const hits = fw.filter((w) => said.has(w)).length;
    if (fw.length && !hits) return;
    const sc = 2 + ff.length * 100 + hits;
    if (sc > score) {
      score = sc;
      best = i;
      tied = false;
    } else if (sc === score) tied = true;
  });
  return tied ? null : best;
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

function LateLedger({ rows }: { rows: NonNullable<ReturnType<typeof lateRows>> }) {
  const allUnconfirmed = rows.every((r) => r.unconfirmed);
  return (
    <div className="space-y-3" data-testid="fact-list">
      <div className="-mx-1 overflow-x-auto px-1">
        <table className="w-full text-[13px]" aria-label="Late purchase orders">
          <thead>
            <tr className="h-8 border-b border-border text-left text-[12px] text-muted">
              <th scope="col" className="pr-3 font-normal">PO</th>
              <th scope="col" className="px-3 font-normal">Needed by</th>
              <th scope="col" className="px-3 text-right font-normal">Late</th>
              {!allUnconfirmed && <th scope="col" className="px-3 font-normal">Supplier date</th>}
              <th scope="col" className="hidden pl-3 text-right font-normal sm:table-cell">Source</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={`${r.po}-${i}`} className="h-9 border-b border-border last:border-0">
                <td className="whitespace-nowrap pr-3 font-medium">{r.po}</td>
                <td className="whitespace-nowrap px-3 tabular-nums">{r.due}</td>
                <td className="whitespace-nowrap px-3 text-right tabular-nums">
                  <span className={cn("inline-flex items-center gap-1.5", lateFigure(r.days))}>
                    {r.days}d
                  </span>
                </td>
                {!allUnconfirmed && <td className="px-3 text-muted">{r.unconfirmed ? "None" : "On record"}</td>}
                <td className="hidden pl-3 text-right sm:table-cell">
                  <SourceChips sources={r.fact.sources} max={1} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {allUnconfirmed && (
        <p className="flex items-center gap-2 text-[12px] leading-4 text-muted">
          <OpenRing size={12} tone="neutral" />
          None of these has a supplier date on record.
        </p>
      )}
    </div>
  );
}

/** Citation numbers for an answer's sources, in the order a reader meets them. */
function citationsOf(a: Answer | undefined, facts: Fact[], used: Set<number>): { numbers: ReadonlyMap<string, number>; cited: Source[]; owners: ReadonlyMap<string, string> } {
  const numbers = new Map<string, number>();
  const cited: Source[] = [];
  const owners = new Map<string, string>();
  if (!a) return { numbers, cited, owners };
  const add = (src: Source | null | undefined, owner?: string | null) => {
    if (!src?.handle || numbers.has(src.handle)) return;
    numbers.set(src.handle, numbers.size + 1);
    cited.push(src);
    if (owner) owners.set(src.handle, owner);
  };
  const order = [...used, ...facts.map((_, i) => i).filter((i) => !used.has(i))];
  for (const i of order) {
    // a fact about one order ("106001: 134 days late") names the record its sources are of
    const m = LATE_FACT.exec(readable(facts[i]?.text ?? "").trim());
    for (const src of facts[i]?.sources ?? []) add(src, m ? `PO ${m[1]}` : null);
  }
  for (const c of a.conflicts ?? []) for (const v of c.values) add(v.source);
  // one owner for every source says nothing: the rail then names each by what it is
  if (new Set(owners.values()).size < 2) owners.clear();
  return { numbers, cited, owners };
}

/** One numbered source in the rail: its number, its system's tile, the record it is of and what it is; it opens the evidence. */
function SourceRow({ source, n, owner }: { source: Source; n: number; owner?: string }) {
  const [open, setOpen] = useState(false);
  const name = sourceName(source);
  const sys = SYSTEM_LABEL[source.system as keyof typeof SYSTEM_LABEL] ?? source.system;
  const message = source.system === "Email" || source.system === "Teams";
  const what = message && source.label && !/^(?:email|teams)? ?message$/i.test(source.label.trim())
    ? source.label.replace(/^(?:email|teams|message)\s+/i, "").replace(/^from\s+/i, "")
    : name.includes(" · ")
      ? name.split(" · ").slice(1).join(" · ")
      : name;
  const read = source.as_of && !message ? `read ${formatDate(source.as_of, { short: true })}` : null;
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="group grid h-8 w-full grid-cols-[14px_54px_minmax(0,1fr)] items-center gap-x-2 rounded-[3px] px-2 text-left text-[13px] hover:bg-hover cursor-pointer"
        aria-label={`Source ${n}: ${sys}, ${owner ? `${owner}, ` : ""}${name}`}
        title={[sys, owner, what, read].filter(Boolean).join(" · ")}
        data-testid="answer-source"
      >
        <span className="text-right text-[11px] num text-muted">{n}</span>
        <SystemCode system={source.system} size={18} />
        <span className="min-w-0 truncate">
          {owner ? (
            <>
              <span className="font-medium">{owner}</span> <span className="text-muted">{what}</span>
            </>
          ) : (
            <>
              {what} <span className="text-muted">{read ?? ""}</span>
            </>
          )}
        </span>
      </button>
      {open && <EvidenceDrawer source={source} open={open} onOpenChange={setOpen} />}
    </>
  );
}

/** The answer's sources, numbered as the marks on its claims; each opens its evidence. */
function SourcesList({ sources, owners }: { sources: Source[]; owners: ReadonlyMap<string, string> }) {
  const [all, setAll] = useState(false);
  if (sources.length === 0) return null;
  const shown = all ? sources : sources.slice(0, 9);
  return (
    <section aria-label="Sources" data-testid="answer-sources">
      <SectionTitle
        aside={
          sources.length > shown.length && (
            <button type="button" onClick={() => setAll(true)} className="text-[12px] font-medium text-muted hover:text-fg cursor-pointer">
              All {sources.length}
            </button>
          )
        }
      >
        Sources
      </SectionTitle>
      <ol className="-mx-2">
        {shown.map((src, i) => (
          <li key={src.handle} className="min-w-0">
            <SourceRow source={src} n={i + 1} owner={src.handle ? owners.get(src.handle) : undefined} />
          </li>
        ))}
      </ol>
    </section>
  );
}

/** The records the answer is about, as the rail lists them. */
function RecordsList({ refs }: { refs: ObjectRef[] }) {
  if (refs.length === 0) return null;
  return (
    <section aria-label="Records">
      <SectionTitle>Records</SectionTitle>
      <ul className="-mx-2">
        {refs.slice(0, 5).map((r) => (
          <li key={r.object_id}>
            <Link href={refHref(r)} className="group flex h-8 items-center gap-2.5 rounded-lg px-2 text-[13px] hover:bg-hover">
              <span className="min-w-0 flex-1 truncate">{recordLabel(r.type, r.label)}</span>
              <ArrowUpRight className="size-3.5 shrink-0 text-faint group-hover:text-fg" aria-hidden />
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Questions to ask next; each asks itself. */
function FollowUps({ items, onAsk }: { items: string[]; onAsk?: (q: string) => void }) {
  if (items.length === 0) return null;
  return (
    <section aria-label="Ask next" data-testid="follow-ups">
      <SectionTitle>Ask next</SectionTitle>
      <ul className="-mx-2">
        {items.map((q) => (
          <li key={q}>
            <button
              type="button"
              onClick={() => onAsk?.(q)}
              disabled={!onAsk}
              className="group flex min-h-8 w-full items-center gap-2.5 rounded-lg px-2 py-1 text-left text-[13px] hover:bg-hover disabled:cursor-default disabled:text-muted disabled:hover:bg-transparent cursor-pointer"
            >
              <CornerDownRight className="size-3.5 shrink-0 text-faint" strokeWidth={2} aria-hidden />
              <span className="min-w-0 flex-1">{q}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** One line of confidence: answered from N records, partly answered and why, not on record. Words in ink; the glyph carries the state. */
function Confidence({ a, systems }: { a: Answer; systems: string[] }) {
  const records = new Set((a.facts ?? []).flatMap((f) => f.sources.map((s) => s.handle))).size;
  const line = "flex items-center gap-2 text-[12px] leading-4";
  if (a.assistant_unavailable)
    return (
      <p className={cn(line, "text-fg")} data-testid="outcome-badge">
        <StatusGlyph status="risk" size={13} />
        Assistant unavailable. The facts below come straight from the records.
      </p>
    );
  if (a.outcome === "partial" && (a.did_you_mean ?? []).length > 0 && (a.facts ?? []).length === 0)
    return (
      <p className={cn(line, "text-muted")} data-testid="outcome-badge">
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
      <p className={cn(line, "text-fg")} data-testid="outcome-badge">
        <StatusGlyph status="risk" size={13} />
        <span className="min-w-0">Partly answered{why ? `: ${why}` : ""}</span>
      </p>
    );
  }
  if (a.outcome === "not_found" || a.outcome === "abstained")
    return (
      <p className={cn(line, "text-muted")} data-testid="outcome-badge">
        <StatusGlyph status="neutral" size={13} />
        Not on record{(a.not_found ?? [])[0] ? `: ${readable(a.not_found![0].where_it_would_live)}` : ""}
      </p>
    );
  if (a.outcome === "refused")
    return (
      <p className={cn(line, "text-muted")} data-testid="outcome-badge">
        Can&apos;t answer this
      </p>
    );
  if (records === 0) return null;
  return (
    <p className={cn(line, "text-muted")} data-testid="confidence">
      {systems.length > 0 && (
        <span className="flex gap-1" aria-hidden>
          {systems.slice(0, 4).map((x) => (
            <SystemCode key={x} system={x} size={16} />
          ))}
        </span>
      )}
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

export function AnswerCard({ turn, onPick, onRetry, onAsk, onInstruct, onClarify }: { turn: Turn; onPick?: (p: Pick) => void; onRetry?: () => void; onAsk?: (q: string) => void; onInstruct?: (q: string, extra: InstructAsk) => void; onClarify?: (words: string, reply: ClarifyReply) => void }) {
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
  const { company, general } = splitGeneral(shownText ?? "");
  const { text, missing: missingSaid } = missingLine(company);
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
  const ask = a ? whoToAsk(a) : null;
  const usedSystems = a ? [...new Set((a.facts ?? []).flatMap((f) => f.sources.map((s) => s.system)))] : [];
  const facts = a?.facts ?? [];
  // a field dump's pairs stay plain lines; the answer's own bullets carry their facts' chips
  const { lines, used } = restLines(rest, composed ? [] : facts, record);
  const otherFacts = facts.filter((_, i) => !used.has(i));
  // one number per cited source, in reading order: the bullets' facts, then the rest, then
  // the systems that disagree; each claim carries its numbers and the list below names them
  const { numbers, cited, owners } = citationsOf(a, facts, used);
  // a list of late orders reads as a ledger when every fact is one late-order line
  const ledger = typeof a?.list_total === "number" && lines.length === 0 ? lateRows(otherFacts) : null;
  // what the answer couldn't confirm, in one line: its own words, else the gaps it lists
  const gaps = (a?.not_found ?? []).filter((n) => !internalWords(n.what)).map((n) => missingText(n).replace(/[.:].*$/, "")).filter(Boolean);
  const missing = a ? ((missingSaid && !internalWords(missingSaid) ? missingSaid : null) ?? (gaps.length ? gaps.slice(0, 2).join("; ") + (gaps.length > 2 ? `; ${gaps.length - 2} more` : "") : null)) : null;
  const followUps = (a?.follow_ups ?? []).filter((q) => !internalWords(q));
  const proposals = a?.proposals ?? [];
  return (
    <CiteNumbers.Provider value={a ? numbers : null}>
    <article className="grid grid-cols-1 gap-x-12 gap-y-8 xl:grid-cols-[minmax(0,1fr)_248px]" aria-busy={busy} data-testid="answer-card">
      <div className="min-w-0 space-y-7">
        <div className="space-y-3">
          <SubjectsLine refs={refs} timeFrame={u?.time_frame} className={ledger ? "hidden" : a && refs.length ? "xl:hidden" : undefined} />
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

          {a && !a.clarify && <Confidence a={a} systems={usedSystems} />}
          {a && <AssumptionLine a={a} onReply={onClarify} />}

          {composed || lead.length > 0 ? (
            <div
              className={cn("break-words", longLead ? "max-w-[68ch] space-y-3 text-[13px] leading-[21px]" : "text-[15px] leading-[24px] tracking-[-0.006em]", unfinished ? "text-muted" : "text-fg")}
              data-testid="direct-answer"
              data-unconfirmed={unfinished || undefined}
            >
              {unfinished && (
                <p className="mb-1 text-[12px] leading-4 font-medium text-muted" data-testid="early-reading">
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
                <button type="button" onClick={() => setFullLead((v) => !v)} aria-expanded={fullLead} className="-mx-2 mt-1 inline-flex h-7 items-center rounded-md px-2 text-[12px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer">
                  {fullLead ? "Show less" : "Show all"}
                </button>
              )}
            </div>
          ) : null}

          {busy && <Progress turn={turn} />}
        </div>

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
          <>
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
            {typeof a.list_total === "number" && a.list_total > (a.facts ?? []).length && (
              <Link href="/late" className="-mt-4 -mx-2 inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-[12px] font-medium text-muted hover:bg-hover hover:text-fg" data-testid="show-all">
                {(a.facts ?? []).length} of {a.list_total} shown. Show all
                <ArrowRight className="size-3.5" aria-hidden />
              </Link>
            )}
            {general && <GeneralPart heading={general.heading} text={general.text} />}
            {missingCount > 0 ? (
              // what wasn't found, said in one line; opened, the line gives way to each item and who to ask
              <details className="group" open={a.outcome === "partial"} data-testid="whats-missing">
                <summary className="-mx-2 flex cursor-pointer list-none items-start gap-3 rounded-lg px-2 py-1.5 text-[13px] leading-5 hover:bg-hover [&::-webkit-details-marker]:hidden">
                  <OpenRing size={14} tone="neutral" className="mt-[3px] group-open:hidden" />
                  <span className="min-w-0 flex-1" data-testid={missing ? "could-not-confirm" : undefined}>
                    <span className="font-medium">{notFound.length ? "Not found" : "Hidden from you"}</span>
                    {missing && (
                      <span className="group-open:hidden">
                        <span className="font-medium">: </span>
                        <span className="text-muted">{prose(missing).replace(/\.?$/, ".")}</span>
                      </span>
                    )}
                    <span className={cn("ml-1.5 tabular-nums text-muted", missing && "hidden group-open:inline")}>{notFound.length || missingCount}</span>
                    <ChevronRight className="ml-1 inline size-3.5 -translate-y-px align-middle text-muted transition-transform group-open:rotate-90" strokeWidth={2.2} aria-hidden />
                  </span>
                </summary>
                <div className="space-y-3 pt-1.5 pb-1">
                  <NotFoundSection items={notFound} bare />
                  <HiddenNotice hidden={a.hidden} />
                </div>
              </details>
            ) : missing ? (
              <p className="flex gap-3 text-[13px] leading-5" data-testid="could-not-confirm">
                <OpenRing size={14} tone="neutral" className="mt-[3px]" />
                <span className="min-w-0">
                  <span className="font-medium">Not found: </span>
                  <span className="text-muted">{prose(missing).replace(/\.?$/, ".")}</span>
                </span>
              </p>
            ) : null}
            {a.profitability && <ProfitabilityView p={a.profitability} />}
            {(a.conflicts ?? []).map((c, i) => (
              <ConflictCard key={i} conflict={c} />
            ))}
            <AttachmentNotes items={a.attachments ?? []} />
            <AskDrafts drafts={a.drafts ?? []} />
            {a.clarify && <ClarifyPart a={a} onReply={onClarify} />}
            {a.instruction && <InstructionPart part={a.instruction} answerId={a.answer_id} onInstruct={onInstruct} />}
            {ask && (
              <section data-testid="who-to-ask">
                <SectionTitle>Next step</SectionTitle>
                <p className="text-[15px] leading-[22px] font-medium tracking-[-0.01em]">{ask}</p>
              </section>
            )}
            {(steps > 0 || proposals.length > 0) && (
              <div className="space-y-0.5">
                {steps > 0 && (
                  <Disclosure label="Evidence" count={steps} testId="how-we-know">
                    <EvidenceTrailCard trail={a.trail} bare showNext={false} />
                  </Disclosure>
                )}
                {proposals.length > 0 && (
                  <Disclosure label="Drafts" count={proposals.length} testId="answer-proposals">
                    <ProposalCards proposals={a.proposals} bare />
                  </Disclosure>
                )}
              </div>
            )}
            <FollowUps items={followUps} onAsk={onAsk} />
          </>
        )}
      </div>

      {/* a question asked back read nothing yet: no sources or checks to list */}
      {a && !a.clarify && (
        <aside className="grid min-w-0 content-start gap-x-10 gap-y-7 sm:grid-cols-2 xl:col-start-2 xl:row-span-2 xl:row-start-1 xl:grid-cols-1 xl:self-start" aria-label="Sources and records">
          {refs.length > 0 && (
            <div className="hidden xl:block">
              <RecordsList refs={refs} />
            </div>
          )}
          <SourcesList sources={cited} owners={owners} />
          <section data-testid="what-checked">
            <SectionTitle>Checked</SectionTitle>
            <CheckedList freshness={a.freshness} facts={a.facts} />
          </section>
        </aside>
      )}

      {a && (
        <footer className="flex min-w-0 flex-wrap items-center gap-x-0.5 gap-y-3 xl:col-start-1">
          <div className="order-last w-full min-w-0">
            <FreshnessLabel freshness={a.freshness} used={usedSystems} />
          </div>
          {!a.clarify && <SaveButton question={turn.question} href={`/ask?c=${encodeURIComponent(a.conversation_id)}`} />}
          {!a.clarify && <AskOrganizer conversationId={a.conversation_id} records={a.subjects ?? []} />}
          <Feedback answerId={a.answer_id} facts={(a.facts ?? []).map((f) => f.text)} />
          {primary && (
            <Link
              href={refHref(primary)}
              className="order-first mr-1.5 inline-flex h-8 items-center gap-1 rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-3 text-[13px] font-medium text-fg hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)]"
              data-testid="subject-link"
            >
              Open {recordLabel(primary.type, primary.label)}
              <ArrowUpRight className="size-3.5" strokeWidth={2.2} aria-hidden />
            </Link>
          )}
        </footer>
      )}
    </article>
    </CiteNumbers.Provider>
  );
}
