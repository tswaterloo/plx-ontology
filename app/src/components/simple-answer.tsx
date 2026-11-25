"use client";

import { Brain, ChevronRight, CornerDownRight, Download, ShieldAlert, ShieldCheck } from "lucide-react";
import { useState, type ReactNode } from "react";
import { formatDate, plural } from "@/lib/format";
import { useReveal } from "@/lib/reveal";
import { parseMarkdown } from "@/lib/markdown";
import type { Answer, StatementRead } from "@/lib/types";
import { readStatement } from "@/lib/api";
import { saveCsv } from "@/components/charts/export";
import { ShowWork } from "./query-work";
import { EmailDraftButton } from "./email-draft-button";
import { ExcelExportButton } from "./excel-export-button";
import { Disclosure } from "./answer-parts";
import { DefinitionLines } from "./definition-lines";
import { AnswerCharts, type AskChart, withoutChartBlocks } from "./answer-chart";
import { AskDrafts, AttachmentNotes } from "./ask-drafts";
import { AskNote } from "./ask-note";
import { WorkItems } from "./work-items";
import { Feedback } from "./feedback";
import { PreparedChanges } from "./prepared-change";
import { ProposalCards } from "./proposal-cards";
import { inlines } from "@/lib/markdown";
import { Inlines, RichText, checked } from "./rich-text";

export type Cited = NonNullable<Answer["cited"]>[number];

/** One piece of an answer: prose, or a Markdown table the answer wrote. */
export type Segment = { kind: "text"; text: string } | { kind: "table"; head: string[]; rows: string[][] };

const ROW = /^\s*\|.*\|\s*$/;
const RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function cells(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

/** Pure: the answer's text with each Markdown table (a header row, a rule row, body rows) taken out as a table. */
export function splitTables(text: string): Segment[] {
  const lines = text.split("\n");
  const out: Segment[] = [];
  let prose: string[] = [];
  const flush = () => {
    if (prose.join("\n").trim()) out.push({ kind: "text", text: prose.join("\n").trim() });
    prose = [];
  };
  for (let i = 0; i < lines.length; i++) {
    if (ROW.test(lines[i]) && i + 1 < lines.length && RULE.test(lines[i + 1])) {
      flush();
      const head = cells(lines[i]);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && ROW.test(lines[i])) rows.push(cells(lines[i++]));
      i--;
      out.push({ kind: "table", head, rows });
    } else prose.push(lines[i]);
  }
  flush();
  return out;
}

/** Pure: how many records the Sources line opens to. */
export function sourceCount(cited: Cited[] | null | undefined): number {
  return (cited ?? []).reduce((n, t) => n + t.records, 0);
}

/** ``written``: a table the answer wrote (its cells' **bold** is bold, never shown as asterisks); else rows as read from the records, shown as they are. */
// a code, number, currency amount, date, or short status stays on one line: the table scrolls
// instead of breaking "16,704.00 CAD" or "Nothing yet" onto two lines
const NOWRAP_CELL = /^(?:[-−+(]?(?:[A-Z]{1,3})?[$€£]?\d[\d,]*(?:\.\d+)?%?\)?(?:\s+\(unverified\))?(?:\s+[A-Za-z]{1,4})?|\d{1,2}\s+[A-Za-z]{3}\s+\d{4}|\S{1,24}|[A-Za-z0-9][A-Za-z0-9 .,'/-]{0,17})$/;

// a column whose every filled cell is a number (an amount, a count, a percentage, a duration
// with a unit, allowing placeholder dashes like "-" or "–") lines up on the right, in the page's
// own font with tabular digits
const NUMBER_CELL = /^[-−+(]?(?:[A-Z]{1,3})?[$€£]?\d[\d,]*(?:\.\d+)?%?\)?(?:\s+\(unverified\))?(?:\s?[A-Za-z]{1,4}\b)?(?:\s+\(unverified\))?$/;
const MISSING_CELL = /^(?:[-–]|n\/a)$/i;
const numericColumn = (rows: (string | null)[][], j: number) => {
  const cells = rows.map((r) => (r[j] ?? "").replace(/\*\*/g, "").trim()).filter((c) => c !== "");
  const nonMissing = cells.filter((c) => !MISSING_CELL.test(c));
  return nonMissing.length > 0 && nonMissing.every((c) => NUMBER_CELL.test(c));
};

function Table({ head, rows, caption, written = false, exportTitle, sql, asOf, totalRows, truncated }: {
  head: string[]; rows: (string | null)[][]; caption?: ReactNode; written?: boolean;
  exportTitle?: string; sql?: string | null; asOf?: string | null;
  totalRows?: number | null; truncated?: boolean;
}) {
  const cell = (text: string) => (written ? <Inlines xs={inlines(text)} /> : checked(text));
  // a long table shows its first rows and "Show all N rows" opens the rest
  const [all, setAll] = useState(false);
  const right = head.map((_, j) => numericColumn(rows, j));
  const shown = all ? rows : rows.slice(0, FIRST_ROWS); // guard-ok: the first rows; the control below shows all
  const tabTitle = exportTitle || (typeof caption === "string" ? caption : "Table");
  return (
    <div className="max-w-full overflow-x-auto">
      <table className="w-full border-collapse text-[13.5px]">
        {caption && <caption className="border-b border-border py-1.5 text-left text-[12px] text-muted">{caption}</caption>}
        <thead>
          <tr className="border-b border-border-strong">
            {head.map((h, i) => (
              <th key={i} className={`px-2.5 py-2 first:pl-0 last:pr-0 text-[11px] font-medium uppercase tracking-[0.04em] text-faint whitespace-nowrap ${right[i] ? "text-right" : "text-left"}`}>
                {written ? <Inlines xs={inlines(h)} /> : h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((r, i) => (
            <tr key={i} className="border-b border-border/70 last:border-0 hover:bg-hover/40">
              {head.map((_, j) => {
                const raw = (r[j] ?? "").replace(/\*\*/g, "").trim();
                const nowrap = right[j] || NOWRAP_CELL.test(raw);
                return (
                  <td key={j} className={`px-2.5 py-2 first:pl-0 last:pr-0 align-top tabular-nums${right[j] ? " text-right" : ""}${nowrap ? " whitespace-nowrap" : ""}`}>
                    {cell(r[j] ?? "")}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {(rows.length > FIRST_ROWS || (!written && rows.length > 0)) && (
        <div className="flex flex-wrap items-center gap-x-3 border-t border-border py-2">
          {rows.length > FIRST_ROWS && (
            <button type="button" className="cursor-pointer text-[12px] font-medium text-accent underline-offset-2 hover:underline" aria-expanded={all} onClick={() => setAll((a) => !a)}>
              {all ? "Show fewer rows" : `Show all ${rows.length.toLocaleString("en-US")} rows`}
            </button>
          )}
          {!written && rows.length > 0 && (
            <>
              <button
                type="button"
                onClick={() => saveCsv(head, rows, tabTitle)}
                className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 text-[12.5px] font-medium text-muted hover:border-border-strong hover:bg-hover hover:text-fg cursor-pointer"
                data-testid="csv-export-btn"
              >
                <Download className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
                CSV
              </button>
              <ExcelExportButton
                title={tabTitle}
                req={() => ({
                  title: tabTitle,
                  tables: [{
                    title: tabTitle,
                    columns: head,
                    rows,
                    sql: sql ?? null,
                    as_of: asOf ?? null,
                    total_rows: totalRows ?? null,
                    truncated: Boolean(truncated || (totalRows != null && rows.length < totalRows)),
                  }],
                })}
              />
            </>
          )}
        </div>
      )}
    </div>
  );
}

const FIRST_ROWS = 100;

/** Structured result profile strip for one cited source (GG2): rows read, time range, views/sources, and coverage note. */
function SourceProfile({ t }: { t: Cited }) {
  const p = t.profile;
  if (!p) return null;
  const views = (p.views ?? []).length > 0 ? (p.views ?? []) : (t.work?.views ?? []);
  const rowsText = p.exact === false
    ? `At least ${p.rows_read.toLocaleString("en-US")} ${p.rows_read === 1 ? "row" : "rows"} read`
    : p.rows_shown < p.rows_read
      ? `${p.rows_read.toLocaleString("en-US")} rows read (showing ${p.rows_shown.toLocaleString("en-US")})`
      : `${p.rows_read.toLocaleString("en-US")} ${p.rows_read === 1 ? "row" : "rows"} read`;
  return (
    <div className="rounded-lg border border-border/70 bg-surface/50 px-3 py-2 text-[12px] text-muted" data-testid="source-profile">
      <dl className="flex flex-wrap gap-x-4 gap-y-1">
        <div className="flex items-baseline gap-1.5">
          <dt className="text-faint">Rows read</dt>
          <dd className="font-medium text-fg tabular-nums">{rowsText}</dd>
        </div>
        {p.time_range && (
          <div className="flex items-baseline gap-1.5">
            <dt className="text-faint">Time range</dt>
            <dd className="text-fg tabular-nums">{p.time_range}</dd>
          </div>
        )}
        {views.length > 0 && (
          <div className="flex items-baseline gap-1.5">
            <dt className="text-faint">Sources consulted</dt>
            <dd className="text-fg">{views.join(", ")}</dd>
          </div>
        )}
      </dl>
      {p.coverage_note && (
        <p className="mt-1 text-risk" data-testid="source-coverage-note">{p.coverage_note}</p>
      )}
    </div>
  );
}

/** Coverage rows and missing/uncaptured coverage summary shown inside the expandable Sources panel (GG2). */
function SourcesCoverage({ coverage, notFound }: { coverage: NonNullable<Answer["coverage"]>; notFound: NonNullable<Answer["not_found"]> }) {
  if (coverage.length === 0 && notFound.length === 0) return null;
  return (
    <div className="space-y-1.5 rounded-lg border border-border/70 bg-surface/50 px-3 py-2 text-[12px]" data-testid="sources-coverage">
      <p className="text-[11px] font-semibold uppercase tracking-[0.04em] text-faint">Coverage</p>
      {coverage.length > 0 && (
        <ul className="space-y-1 text-fg">
          {coverage.map((p, i) => (
            <li key={i} className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 border-t border-border/60 pt-1 first:border-0 first:pt-0">
              <span className="min-w-0">
                {p.part}
                {p.period ? <span className="text-muted"> ({p.period})</span> : null}
              </span>
              <span className="shrink-0 text-muted tabular-nums">{partLine(p)}</span>
            </li>
          ))}
        </ul>
      )}
      {notFound.length > 0 && (
        <ul className="space-y-0.5 border-t border-border/60 pt-1.5 text-muted first:border-0 first:pt-0" data-testid="sources-coverage-gaps">
          {notFound.map((g, i) => (
            <li key={i}>
              Not captured: {g.what} (expected in {g.where_it_would_live}).
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** One source of an answer: its rows, what its query reads and filters on (each filter can be changed, read again as the reader), and the query itself. */
function CitedSource({ t }: { t: Cited }) {
  const [again, setAgain] = useState<StatementRead | null>(null);
  const caption = again
    ? `Read again with your filters: ${plural(again.row_count, "row")}${again.truncated ? " (more not shown)" : ""}${again.as_of ? `, as of ${formatDate(again.as_of)}` : ""}`
    : `${t.about}${t.as_of ? `, as of ${formatDate(t.as_of)}` : ""}${t.rows.length < t.records ? ` (first ${t.rows.length} of ${t.records})` : ""}`;
  const rows = again ? again.rows : t.rows;
  const totalRows = again ? (again.total_rows ?? (again.truncated ? null : again.row_count)) : t.records;
  const truncated = again ? (again.truncated || (again.total_rows != null && again.rows.length < again.total_rows)) : t.rows.length < t.records;
  return (
    <div className="space-y-1.5">
      <SourceProfile t={t} />
      <Table
        head={again ? again.columns : t.columns}
        rows={rows}
        caption={caption}
        exportTitle={t.about}
        sql={again ? again.sql : t.query}
        asOf={again ? again.as_of : t.as_of}
        totalRows={totalRows}
        truncated={truncated}
      />
      {t.query && t.work && (
        <ShowWork
          work={t.work}
          testId="simple-work"
          read={async (edits) => setAgain(await readStatement(t.query ?? "", edits))}
          onReset={() => setAgain(null)}
        />
      )}
      {t.query && (
        <details className="text-[12px]" data-testid="simple-query">
          <summary className="cursor-pointer text-muted">Query</summary>
          <pre className="mt-1 overflow-x-auto whitespace-pre-wrap rounded border border-border p-2 font-mono">{again ? again.sql : t.query}</pre>
        </details>
      )}
    </div>
  );
}

/** The answer as written: prose in the body face, its tables as tables. */
/** One part of the question as read (Ask plan A3b-4): read with what it found, or not read. */
export function partLine(p: { reads?: number; found?: number }): string {
  if (!p.reads) return "not read";
  return p.found ? `read, ${p.found.toLocaleString()} found` : "read, nothing found";
}

/** The second check of the answer against the records (Ask plan A4), in one line: what it found and how long it took;
 * a claim it still disagrees with is written under the answer itself. */
export function checkLine(c: NonNullable<Answer["check"]>): string {
  const n = plural(c.claims ?? 0, "statement");
  switch (c.status) {
    case "checked": return `Checked against what was read: nothing found wrong (${Math.round(c.seconds ?? 0)} s).`;
    case "repaired": return `Checked against what was read: ${n} sent back for correction.`;
    case "flagged": return `Checked against what was read: the check found a conflict or could not confirm ${n}; shown as written.`;
    case "disagrees": return `Checked against what was read: ${n}; it still disagrees with what is noted under the answer.`;
    default: return `Not checked against what was read: ${c.why ?? "the check did not run"}.`;
  }
}

const CHART_TOKEN = /\[\[chart:(\d+)\]\]/g;

function renderProseWithInlineCharts(
  text: string,
  charts: AskChart[],
  placed: Set<number>,
  chartQuestion: string | undefined,
  keyPrefix: string,
): ReactNode[] {
  const parts: ReactNode[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  CHART_TOKEN.lastIndex = 0;
  while ((match = CHART_TOKEN.exec(text)) !== null) {
    const before = text.slice(last, match.index).trim();
    if (before) {
      parts.push(<RichText key={`${keyPrefix}-t-${last}`} blocks={parseMarkdown(before)} className="space-y-2 text-[15px] leading-relaxed" />);
    }
    const idx = Number(match[1]);
    if (Number.isInteger(idx) && idx >= 0 && idx < charts.length && !placed.has(idx)) {
      placed.add(idx);
      parts.push(<AnswerCharts key={`${keyPrefix}-c-${idx}`} charts={[charts[idx]]} question={chartQuestion} />);
    }
    last = match.index + match[0].length;
  }
  const after = text.slice(last).trim();
  if (after) {
    parts.push(<RichText key={`${keyPrefix}-t-${last}`} blocks={parseMarkdown(after)} className="space-y-2 text-[15px] leading-relaxed" />);
  }
  return parts;
}

export function AnswerText({
  text,
  muted = false,
  charts,
  chartQuestion,
}: {
  text: string;
  muted?: boolean;
  charts?: AskChart[];
  chartQuestion?: string;
}) {
  const allCharts = charts ?? [];
  const segments = splitTables(text);

  if (allCharts.length === 0) {
    return (
      <div className={muted ? "space-y-3 text-muted" : "space-y-3"} data-testid="simple-answer-text">
        {segments.map((s, i) =>
          s.kind === "table" ? <Table key={i} head={s.head} rows={s.rows} written /> : <RichText key={i} blocks={parseMarkdown(s.text)} className="space-y-2 text-[15px] leading-relaxed" />,
        )}
      </div>
    );
  }

  // First honor any explicit [[chart:N]] inline markers in the prose.
  const placed = new Set<number>();
  const hasMarkers = segments.some((s) => s.kind === "text" && /\[\[chart:\d+\]\]/.test(s.text));
  if (hasMarkers) {
    for (const s of segments) {
      if (s.kind !== "text") continue;
      for (const m of s.text.matchAll(CHART_TOKEN)) {
        const idx = Number(m[1]);
        if (Number.isInteger(idx) && idx >= 0 && idx < allCharts.length) placed.add(idx);
      }
    }
  }
  const remaining = allCharts.filter((_, idx) => !placed.has(idx));
  const chartBlock = remaining.length > 0 ? <AnswerCharts key="answer-charts-top" charts={remaining} question={chartQuestion} /> : null;
  const renderedInline = new Set<number>();

  // Place unpositioned charts right after the opening summary paragraph (before any
  // Markdown table or bullet list), or at the top if the answer starts with a table/list.
  const nodes: ReactNode[] = [];
  let inserted = !chartBlock;

  segments.forEach((s, i) => {
    if (s.kind === "table") {
      if (!inserted && chartBlock) {
        nodes.push(chartBlock);
        inserted = true;
      }
      nodes.push(<Table key={`seg-${i}`} head={s.head} rows={s.rows} written />);
      return;
    }

    if (hasMarkers && /\[\[chart:\d+\]\]/.test(s.text)) {
      if (!inserted && chartBlock && i === 0) {
        const firstMarker = s.text.search(/\[\[chart:\d+\]\]/);
        const beforeFirst = s.text.slice(0, firstMarker).trim();
        const beforeBlocks = beforeFirst ? parseMarkdown(beforeFirst) : [];
        if (beforeBlocks.length === 0 || beforeBlocks[0].kind !== "p") {
          nodes.push(chartBlock);
          inserted = true;
        }
      }
      nodes.push(...renderProseWithInlineCharts(s.text, allCharts, renderedInline, chartQuestion, `seg-${i}`));
      if (!inserted && chartBlock) {
        nodes.push(chartBlock);
        inserted = true;
      }
      return;
    }

    const blocks = parseMarkdown(s.text);
    if (!inserted && chartBlock) {
      if (blocks.length > 0 && blocks[0].kind === "p") {
        nodes.push(<RichText key={`seg-${i}-lead`} blocks={[blocks[0]]} className="space-y-2 text-[15px] leading-relaxed" />);
        nodes.push(chartBlock);
        inserted = true;
        if (blocks.length > 1) {
          nodes.push(<RichText key={`seg-${i}-rest`} blocks={blocks.slice(1)} className="space-y-2 text-[15px] leading-relaxed" />);
        }
        return;
      }
      nodes.push(chartBlock);
      inserted = true;
    }
    nodes.push(<RichText key={`seg-${i}`} blocks={blocks} className="space-y-2 text-[15px] leading-relaxed" />);
  });

  if (!inserted && chartBlock) {
    nodes.push(chartBlock);
  }

  return (
    <div className={muted ? "space-y-3 text-muted" : "space-y-3"} data-testid="simple-answer-text">
      {nodes}
    </div>
  );
}

/** Plain-language reasoning steps ("How this was calculated") distilled from the answer's verified measures, queried views/filters, and formula working. */
export function calculationSteps(answer: Answer): string[] {
  const steps: string[] = [];
  const seen = new Set<string>();
  const add = (line: string) => {
    const clean = line.trim();
    if (clean && !seen.has(clean)) {
      seen.add(clean);
      steps.push(clean);
    }
  };
  for (const d of answer.definitions ?? []) {
    add(`Used verified measure ${d.name} (${d.owner}): ${d.definition}`);
  }
  for (const t of answer.cited ?? []) {
    if (!t.query) continue;
    const views = (t.work?.views ?? []).length > 0 ? t.work!.views : (t.profile?.views ?? []);
    const viewLabel = views.length > 0 ? views.map((v) => v.replace(/_/g, " ")).join(", ") : t.about;
    const filters = (t.work?.filters ?? []).map((f) => f.text).filter(Boolean);
    const filterPart = filters.length > 0 ? ` filtered by ${filters.slice(0, 3).join(" and ")}${filters.length > 3 ? ` (+${filters.length - 3} more)` : ""}` : "";
    const countPart = t.records > 0 ? ` (${plural(t.records, "row")})` : "";
    add(`Read ${viewLabel}${filterPart}${countPart}.`);
  }
  for (const w of answer.working ?? []) {
    add(w);
  }
  return steps;
}

/** The simple Ask's answer: the answer, then one collapsed "Sources (n records)" line and a unified horizontal action bar. */
export function SimpleAnswer({
  answer,
  streamed,
  charts,
  question,
  busy,
  status,
  progress,
  onAsk,
  onDeeper,
  noteRecord,
  showNote = false,
}: {
  answer?: Answer;
  streamed: string;
  charts?: AskChart[];
  question?: string;
  busy: boolean;
  status?: string;
  progress?: ReactNode;
  onAsk?: (q: string) => void;
  onDeeper?: () => void;
  noteRecord?: string;
  showNote?: boolean;
}) {
  // written out from the top as it comes (``useReveal``: the reply reaches the browser whole)
  const text = useReveal(answer ? answer.direct_answer : withoutChartBlocks(streamed), busy);
  const cited = answer?.cited ?? [];
  const n = sourceCount(cited);
  const activeCharts = (answer?.charts && answer.charts.length > 0 ? answer.charts : charts) ?? [];
  const chartQuestion = answer?.question ?? question;
  const followUps = (answer?.follow_ups ?? []).filter((q) => typeof q === "string" && q.trim().length > 0);
  const steps = answer ? calculationSteps(answer) : [];
  const sqlSources = cited.filter((t) => !!t.query);
  return (
    <article className="min-w-0 space-y-4 [overflow-wrap:anywhere]" aria-busy={busy} data-testid="simple-answer">
      {text ? (
        <AnswerText text={text} charts={activeCharts} chartQuestion={chartQuestion} />
      ) : (
        <>
          {activeCharts.length > 0 && <AnswerCharts charts={activeCharts} question={chartQuestion} />}
          {busy && !progress && <p className="text-[13px] text-muted">{status ?? "Working on it"}</p>}
        </>
      )}
      {answer && (answer.work_items ?? []).length > 0 && <WorkItems groups={answer.work_items ?? []} />}
      {busy && progress}
      {busy && !progress && text && status && <p className="text-[12px] text-muted">{status}</p>}
      {answer && (answer.attachments ?? []).length > 0 && <AttachmentNotes items={answer.attachments ?? []} />}
      {answer && (answer.not_found ?? []).length > 0 && (
        <ul className="space-y-1 text-[13px] text-muted" data-testid="simple-gaps">
          {(answer.not_found ?? []).map((g, i) => (
            <li key={i}>
              Not on record: {g.what} (would be kept in {g.where_it_would_live}).
            </li>
          ))}
        </ul>
      )}
      {answer && <DefinitionLines definitions={answer.definitions ?? []} testId="simple-definitions" />}
      {answer && ((answer.coverage ?? []).length > 0 || (answer.untraced ?? []).length > 0 || (answer.working ?? []).length > 0 || steps.length > 0 || !!answer.check) && (() => {
        const cov = answer.coverage ?? [];
        const untraced = answer.untraced ?? [];
        const working = answer.working ?? [];
        const chk = answer.check;
        const foundCount = n > 0 ? n : cov.reduce((acc, p) => acc + (p.found ?? 0), 0);
        const flagged = chk?.status === "flagged" || chk?.status === "disagrees" || untraced.length > 0;
        const label = chk?.status === "checked"
          ? "Verified"
          : chk?.status === "repaired"
            ? "Verified (corrected)"
            : flagged
              ? "Needs review"
              : steps.length > 0
                ? "How this was calculated"
                : "Read summary";
        const secs = chk?.seconds != null ? `${Math.round(chk.seconds)}s` : null;
        const parts = [label, foundCount > 0 ? plural(foundCount, "source") : null, secs].filter(Boolean);
        return (
          <details className="group" data-testid="simple-verification">
            <summary className="inline-flex h-7 cursor-pointer list-none items-center gap-1.5 rounded-md px-1.5 text-[12px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg [&::-webkit-details-marker]:hidden">
              {flagged ? (
                <ShieldAlert className="size-3.5 shrink-0 text-risk" strokeWidth={2} aria-hidden />
              ) : (
                <ShieldCheck className="size-3.5 shrink-0 text-ok" strokeWidth={2} aria-hidden />
              )}
              <span className="tabular-nums">{parts.join(" · ")}</span>
              <ChevronRight className="size-3.5 shrink-0 text-faint transition-transform group-open:rotate-90" strokeWidth={2} aria-hidden />
            </summary>
            <div className="mt-2 space-y-2.5 border-l-2 border-border pl-3.5 py-1 text-[12.5px]">
              {chk && (
                <p className="text-fg" data-testid="simple-check">{checkLine(chk)}</p>
              )}
              {steps.length > 0 && (
                <div className="space-y-1" data-testid="how-calculated-steps">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.04em] text-faint">How this was calculated</p>
                  <ol className="list-decimal space-y-0.5 pl-4 text-[12px] text-fg">
                    {steps.map((step, i) => <li key={i}>{step}</li>)}
                  </ol>
                </div>
              )}
              {sqlSources.length > 0 && (
                <details className="text-[12px]" data-testid="how-calculated-sql">
                  <summary className="cursor-pointer font-medium text-muted hover:text-fg">
                    Inspect SQL ({sqlSources.length})
                  </summary>
                  <div className="mt-1.5 space-y-2">
                    {sqlSources.map((s, i) => (
                      <div key={i} className="space-y-0.5">
                        <p className="text-[11px] text-muted">{s.about}</p>
                        <pre className="overflow-x-auto whitespace-pre-wrap rounded border border-border bg-bg p-2 font-mono text-[11.5px]">{s.query}</pre>
                      </div>
                    ))}
                  </div>
                </details>
              )}
              {cov.length > 0 && (
                <div className="space-y-1" data-testid="simple-coverage">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.04em] text-faint">What was read</p>
                  <ul className="space-y-1 text-fg">
                    {cov.map((p, i) => (
                      <li key={i} className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 border-t border-border/60 pt-1 first:border-0 first:pt-0">
                        <span className="min-w-0">
                          {p.part}
                          {p.period ? <span className="text-muted"> ({p.period})</span> : ""}
                        </span>
                        <span className="shrink-0 text-[12px] text-muted tabular-nums">{partLine(p)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {untraced.length > 0 && (
                <p className="text-[12px] text-risk" data-testid="simple-untraced">Not traced to a record: {untraced.join(", ")}.</p>
              )}
              {working.length > 0 && (
                <div className="space-y-1" data-testid="simple-working">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.04em] text-faint">Working ({working.length})</p>
                  <ul className="space-y-0.5 text-[12px] text-muted">
                    {working.map((w) => <li key={w}>{w}</li>)}
                  </ul>
                </div>
              )}
            </div>
          </details>
        );
      })()}
      {answer && cited.length > 0 && (
        <Disclosure label={`Sources (${plural(n, "record")})`} testId="simple-sources">
          <SourcesCoverage coverage={answer.coverage ?? []} notFound={answer.not_found ?? []} />
          {cited.map((t, i) => (
            <CitedSource key={i} t={t} />
          ))}
          {(answer.cited_more ?? 0) > 0 && <p className="text-[12px] text-muted" data-testid="simple-sources-more">{plural(answer.cited_more ?? 0, "more result")} read, not listed here.</p>}
        </Disclosure>
      )}
      {answer && (answer.drafts ?? []).length > 0 && <AskDrafts drafts={answer.drafts ?? []} record={null} />}
      {answer && (answer.proposals ?? []).length > 0 && <ProposalCards proposals={answer.proposals} />}
      {answer && (answer.documents ?? []).length > 0 && (
        <ul className="space-y-1 text-[13px]" data-testid="simple-documents">
          {(answer.documents ?? []).map((d) => (
            <li key={d.download_url}>
              <a className="font-medium text-accent hover:underline" href={d.download_url} download={d.name}>
                {d.name}
              </a>
              <span className="text-muted"> {Math.max(1, Math.round(d.size / 1024))} KB</span>
              {(d.changes ?? []).length > 0 && (
                <div className="mt-1 border-l border-border pl-3" data-testid="document-changes">
                  <p className="text-muted">
                    {(d.changes ?? []).length === 1 ? "1 change" : `${(d.changes ?? []).length} changes`} from the original
                  </p>
                  <ol className="space-y-1">
                    {(d.changes ?? []).map((c, i) => (
                      <li key={i}>
                        <span className="text-muted">{c.field}: </span>
                        {c.before && <del className="text-late">{c.before}</del>}
                        {c.before && c.after && " "}
                        {c.after && <ins className="text-ok no-underline">{c.after}</ins>}
                      </li>
                    ))}
                  </ol>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      {answer && (answer.prepared ?? []).length > 0 && <PreparedChanges ids={answer.prepared ?? []} />}
      {answer && !answer.clarify && !answer.assistant_unavailable && followUps.length > 0 && (
        <section className="space-y-1.5" aria-label="Ask next" data-testid="simple-follow-ups">
          <p className="text-[11px] font-semibold uppercase tracking-[0.04em] text-faint">Ask next</p>
          <div className="flex flex-wrap gap-1.5">
            {followUps.map((q) => (
              <button
                key={q}
                type="button"
                onClick={() => onAsk?.(q)}
                disabled={!onAsk}
                className="inline-flex min-h-7 max-w-full items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 py-1 text-left text-[12.5px] text-fg transition-colors hover:border-border-strong hover:bg-hover disabled:cursor-default disabled:opacity-60 cursor-pointer"
              >
                <CornerDownRight className="size-3 shrink-0 text-faint" strokeWidth={2} aria-hidden />
                <span className="truncate">{q}</span>
              </button>
            ))}
          </div>
        </section>
      )}
      {answer && !answer.assistant_unavailable && (
        <footer className="flex flex-wrap items-center gap-1.5 border-t border-border/70 pt-3" data-testid="simple-answer-footer">
          {!answer.clarify && (answer.direct_answer || cited.length > 0 || (answer.charts ?? []).length > 0) && (
            <>
              <ExcelExportButton
                title={answer.question || "Workbench answer"}
                req={() => ({
                  title: answer.question || "Workbench answer",
                  summary: answer.direct_answer || "",
                  tables: cited.map((t) => ({
                    title: t.about,
                    columns: t.columns,
                    rows: t.rows,
                    sql: t.query ?? null,
                    as_of: t.as_of ?? null,
                    total_rows: t.records,
                    records: t.records,
                    truncated: t.rows.length < t.records,
                  })),
                  charts: answer.charts ?? [],
                })}
              />
              <EmailDraftButton
                title={answer.question || "Workbench answer"}
                summary={answer.direct_answer || ""}
                charts={(answer.charts ?? []) as unknown as Record<string, unknown>[]}
                citations={cited.filter((t) => t.query).map((t) => ({ label: t.about, sql: t.query, count: t.records ?? t.rows?.length ?? 0 }))}
                format={(answer.charts ?? []).length > 0 ? "xlsx" : "docx"}
                label="Send by email"
              />
            </>
          )}
          <Feedback answerId={answer.answer_id} />
          {onDeeper && (
            <button
              type="button"
              onClick={onDeeper}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 text-[12.5px] font-medium text-accent transition-[background-color,border-color,transform] duration-150 ease-[cubic-bezier(0.2,0,0,1)] hover:border-accent/50 hover:bg-hover active:scale-[0.98] cursor-pointer"
              title="Takes longer and uses deeper analysis"
            >
              <Brain className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
              Deep research
            </button>
          )}
          {showNote && <AskNote record={noteRecord} inline />}
        </footer>
      )}
    </article>
  );
}
