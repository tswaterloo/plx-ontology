"use client";

import { BarChart3, Bookmark, Brain, Check, ChevronRight, Code2, CornerDownRight, Download, FileText, PanelRightOpen, Pin, ShieldAlert, ShieldCheck, StickyNote, Table2 } from "lucide-react";
import Link from "next/link";
import { useId, useState, type ReactNode } from "react";
import { formatDate, plural } from "@/lib/format";
import { useReveal } from "@/lib/reveal";
import { parseMarkdown } from "@/lib/markdown";
import { savedUser, toggleSaved, useSaved } from "@/lib/saved";
import type { Answer, StatementRead } from "@/lib/types";
import { readStatement, saveInsight, saveMemory } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { saveCsv } from "@/components/charts/export";
import { ShowWork } from "./query-work";
import { EmailDraftButton } from "./email-draft-button";
import { ExcelExportButton } from "./excel-export-button";
import { DefinitionLines } from "./definition-lines";
import { AnswerCharts, pinChartSpec, type AskChart, withoutChartBlocks } from "./answer-chart";
import { AskDrafts, AttachmentNotes } from "./ask-drafts";
import { AskSchedulePopover } from "./ask-schedule-popover";
import { WorkItems } from "./work-items";
import { Feedback } from "./feedback";
import { PreparedChanges } from "./prepared-change";
import { ProposalCards } from "./proposal-cards";
import { inlines } from "@/lib/markdown";
import { Inlines, RichText, checked } from "./rich-text";

export type Cited = NonNullable<Answer["cited"]>[number];

export interface CanvasArtifact {
  id: string;
  kind: "chart" | "table" | "document";
  title: string;
  subtitle: string;
  question: string;
  charts?: AskChart[];
  table?: {
    head: string[];
    rows: (string | null)[][];
    sql?: string | null;
    asOf?: string | null;
    totalRows?: number | null;
    truncated?: boolean;
  };
  documents?: NonNullable<Answer["documents"]>;
  cited?: Cited[];
  summary?: string;
}

/** Pure: extract side-panel deliverable artifacts (charts, structured tables, generated documents) from an answer. */
export function artifactsFromAnswer(
  answer: Answer | undefined,
  charts: AskChart[] | undefined,
  question: string | undefined,
  turnId = "turn",
): CanvasArtifact[] {
  const out: CanvasArtifact[] = [];
  const activeCharts = (answer?.charts && answer.charts.length > 0 ? answer.charts : charts) ?? [];
  const cited = answer?.cited ?? [];
  const q = (answer?.question || question || "Workbench result").trim();
  const summary = answer?.direct_answer ?? "";

  if (activeCharts.length > 0) {
    const first = activeCharts[0];
    const pointCount = first.labels?.length ?? first.rows ?? 0;
    const subtitleParts = [
      activeCharts.length > 1 ? `${activeCharts.length} charts` : "Interactive chart",
      pointCount > 0 ? plural(pointCount, "point") : null,
      first.as_of ? `as of ${formatDate(first.as_of)}` : null,
    ].filter(Boolean);
    out.push({
      id: `${turnId}:chart`,
      kind: "chart",
      title: first.title || q,
      subtitle: subtitleParts.join(" · "),
      question: q,
      charts: activeCharts,
      cited,
      summary,
    });
  } else {
    const dataSource = cited.find((t) => (t.columns ?? []).length > 0 && (t.rows ?? []).length > 0);
    const mdTable = !dataSource && summary ? splitTables(summary).find((s) => s.kind === "table") : undefined;
    if (dataSource) {
      const dsRows = dataSource.rows ?? [];
      out.push({
        id: `${turnId}:table`,
        kind: "table",
        title: dataSource.about || q,
        subtitle: `Data table · ${plural(dataSource.records || dsRows.length, "row")}`,
        question: q,
        table: {
          head: dataSource.columns ?? [],
          rows: dsRows,
          sql: dataSource.query ?? null,
          asOf: dataSource.as_of ?? null,
          totalRows: dataSource.records,
          truncated: dsRows.length < dataSource.records,
        },
        cited,
        summary,
      });
    } else if (mdTable && mdTable.kind === "table" && mdTable.rows.length > 0) {
      out.push({
        id: `${turnId}:table`,
        kind: "table",
        title: q,
        subtitle: `Data table · ${plural(mdTable.rows.length, "row")}`,
        question: q,
        table: {
          head: mdTable.head,
          rows: mdTable.rows,
        },
        cited,
        summary,
      });
    }
  }

  const docs = answer?.documents ?? [];
  if (docs.length > 0) {
    const first = docs[0];
    const kb = Math.max(1, Math.round(first.size / 1024));
    out.push({
      id: `${turnId}:document`,
      kind: "document",
      title: docs.length === 1 ? first.name : `${first.name} (${docs.length} files)`,
      subtitle: docs.length === 1 ? `Generated file · ${kb} KB` : `${docs.length} generated files`,
      question: q,
      documents: docs,
      cited,
      summary,
    });
  }

  return out;
}

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
    <div className="max-w-full overflow-x-auto lg:-mx-4 lg:px-4">
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
            <button type="button" className="cursor-pointer text-[12px] font-medium text-fg underline decoration-border-strong underline-offset-4 hover:decoration-fg" aria-expanded={all} onClick={() => setAll((a) => !a)}>
              {all ? "Show fewer rows" : `Show all ${rows.length.toLocaleString("en-US")} rows`}
            </button>
          )}
          {!written && rows.length > 0 && (
            <>
              <button
                type="button"
                onClick={() => saveCsv(head, rows, tabTitle)}
                className="inline-flex h-7 items-center gap-1.5 rounded-lg border border-transparent px-2 py-1 text-[12px] font-medium text-muted transition-all duration-150 hover:border-border hover:bg-surface-2 hover:text-fg active:scale-[0.98] cursor-pointer"
                data-testid="csv-export-btn"
              >
                <Download className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
                CSV
              </button>
              <ExcelExportButton
                title={tabTitle}
                className={UTIL_BTN}
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
  const tRows = t.rows ?? [];
  const tCols = t.columns ?? [];
  const caption = again
    ? `Read again with your filters: ${plural(again.row_count, "row")}${again.truncated ? " (more not shown)" : ""}${again.as_of ? `, as of ${formatDate(again.as_of)}` : ""}`
    : `${t.about}${t.as_of ? `, as of ${formatDate(t.as_of)}` : ""}${tRows.length < t.records ? ` (first ${tRows.length} of ${t.records})` : ""}`;
  const rows = again ? again.rows : tRows;
  const totalRows = again ? (again.total_rows ?? (again.truncated ? null : again.row_count)) : t.records;
  const truncated = again ? (again.truncated || (again.total_rows != null && again.rows.length < again.total_rows)) : tRows.length < t.records;
  return (
    <div className="space-y-1.5">
      <SourceProfile t={t} />
      <Table
        head={again ? again.columns : tCols}
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

const UTIL_BTN =
  "inline-flex h-7 items-center gap-1.5 rounded-lg border border-transparent px-2 py-1 text-[12px] font-medium text-muted transition-all duration-150 hover:border-border hover:bg-surface-2 hover:text-fg active:scale-[0.98] disabled:opacity-50 cursor-pointer disabled:cursor-default";

function SaveAnswerButton({ question, conversationId }: { question: string; conversationId?: string | null }) {
  const saved = useSaved();
  const [localOn, setLocalOn] = useState(false);
  const clean = question.trim();
  if (!clean) return null;
  const key = `q:${clean}`;
  const persisted = saved.some((s) => s.key === key);
  const on = savedUser() ? persisted : localOn;
  const href = conversationId ? `/ask?c=${encodeURIComponent(conversationId)}` : "/ask";

  return (
    <button
      type="button"
      aria-pressed={on}
      aria-label={on ? "Remove saved answer" : "Save answer"}
      title={on ? "Saved in your bookmarks" : "Save answer"}
      data-testid="answer-save-btn"
      onClick={() => {
        if (savedUser()) {
          toggleSaved({ key, kind: "question", label: clean, href });
        } else {
          setLocalOn((v) => !v);
        }
      }}
      className={`${UTIL_BTN}${on ? " border-border bg-surface-2/80 text-fg" : ""}`}
    >
      <Bookmark className={`size-3.5 shrink-0${on ? " fill-current" : ""}`} strokeWidth={2} aria-hidden />
      <span>{on ? "Saved" : "Save"}</span>
    </button>
  );
}

function PinToInsightsButton({ chart, question }: { chart: AskChart; question: string }) {
  const [state, setState] = useState<"idle" | "saving" | "pinned" | "failed">("idle");
  const [problem, setProblem] = useState("");
  if (!chart.sql) return null;

  const pin = async () => {
    if (state === "saving") return;
    setState("saving");
    setProblem("");
    try {
      await saveInsight({
        title: (chart.title || question).slice(0, 160) || "Pinned chart",
        question: question.slice(0, 2000),
        sql: chart.sql ?? "",
        chart: pinChartSpec(chart),
        schedule: "",
        notify: "never",
      });
      setState("pinned");
    } catch (err) {
      setProblem(plainError(err));
      setState("failed");
    }
  };

  if (state === "pinned") {
    return (
      <Link
        href="/insights"
        data-testid="answer-pin-insights-done"
        className="inline-flex h-7 items-center gap-1.5 rounded-lg border border-ok-line bg-ok-soft px-2 py-1 text-[12px] font-medium text-ok transition-opacity hover:opacity-85"
      >
        <Check className="size-3.5 shrink-0" strokeWidth={2.2} aria-hidden />
        <span>Pinned to charts</span>
      </Link>
    );
  }

  return (
    <span className="inline-flex items-center gap-1.5">
      <button
        type="button"
        onClick={() => void pin()}
        disabled={state === "saving"}
        data-testid="answer-pin-insights"
        className={UTIL_BTN}
      >
        <Pin className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
        <span>{state === "saving" ? "Pinning" : "Pin to charts"}</span>
      </button>
      {state === "failed" && problem && (
        <span className="text-[12px] text-late" role="alert">
          {problem}
        </span>
      )}
    </span>
  );
}

function SaveInstructionAction({ record }: { record?: string }) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<"fact" | "instruction">("instruction");
  const [text, setText] = useState("");
  const [savedKind, setSavedKind] = useState<"fact" | "instruction" | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const fieldId = useId();

  async function save() {
    if (!text.trim() || busy) return;
    setBusy(true);
    setProblem(null);
    try {
      await saveMemory({ kind, text: text.trim(), ...(record && kind === "fact" ? { records: [record] } : {}) });
      setSavedKind(kind);
      setText("");
      setOpen(false);
    } catch (e) {
      setProblem(plainError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <span className="inline-flex items-center gap-2" data-testid="save-instruction">
        <button
          type="button"
          onClick={() => {
            setOpen((v) => !v);
            setSavedKind(null);
            setProblem(null);
          }}
          aria-expanded={open}
          data-testid="save-instruction-open"
          className={`${UTIL_BTN}${open ? " border-border bg-surface-2 text-fg" : ""}`}
        >
          <StickyNote className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
          <span>Note</span>
        </button>
        {savedKind && !open && (
          <span role="status" className="text-[12px] text-ok" data-testid="save-instruction-status">
            {savedKind === "instruction"
              ? "Instruction saved for Ask, briefs and Assist."
              : "Context note saved for Ask, briefs and Assist."}
          </span>
        )}
      </span>
      {open && (
        <div
          className="order-last mt-2 w-full space-y-2.5 rounded-xl border border-border bg-surface/80 p-3"
          data-testid="save-instruction-panel"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <label className="block text-[12px] font-medium text-muted" htmlFor={fieldId}>
              {kind === "instruction" ? "Standing instruction (private to you)" : "Context note (private to you)"}
            </label>
            <div className="inline-flex items-center rounded-lg border border-border bg-bg p-0.5 text-[11.5px]" role="radiogroup" aria-label="Memory type">
              <button
                type="button"
                role="radio"
                aria-checked={kind === "fact"}
                onClick={() => setKind("fact")}
                className={`rounded-md px-2 py-0.5 font-medium transition-colors duration-150 cursor-pointer ${kind === "fact" ? "bg-fg text-bg" : "text-muted hover:text-fg"}`}
                data-testid="save-instruction-kind-fact"
              >
                Context note
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={kind === "instruction"}
                onClick={() => setKind("instruction")}
                className={`rounded-md px-2 py-0.5 font-medium transition-colors duration-150 cursor-pointer ${kind === "instruction" ? "bg-fg text-bg" : "text-muted hover:text-fg"}`}
                data-testid="save-instruction-kind-instruction"
              >
                Instruction
              </button>
            </div>
          </div>
          <textarea
            id={fieldId}
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={2}
            maxLength={1000}
            className="w-full rounded-lg border border-border bg-bg p-2.5 text-[13px] text-fg placeholder:text-faint transition-all duration-150 focus:border-fg/30 focus:ring-2 focus:ring-fg/10 focus:outline-none focus-visible:outline-none"
            placeholder={kind === "instruction" ? "e.g. Always show amounts in CAD first, or group by supplier" : "What you know that the records do not"}
          />
          <div className="flex items-center gap-2 text-[12.5px]">
            <button
              type="button"
              onClick={() => void save()}
              disabled={busy || !text.trim()}
              className="inline-flex h-8 items-center rounded-lg bg-fg px-3 font-medium text-bg transition-all duration-150 hover:opacity-90 active:scale-[0.97] disabled:opacity-50 cursor-pointer"
              data-testid="save-instruction-submit"
            >
              {busy ? "Saving" : "Save"}
            </button>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="inline-flex h-8 items-center rounded-lg px-2.5 text-muted transition-all duration-150 hover:bg-surface-2 hover:text-fg active:scale-[0.98] cursor-pointer"
            >
              Cancel
            </button>
          </div>
          {problem && <p role="alert" className="text-[12px] text-late">{problem}</p>}
        </div>
      )}
    </>
  );
}

function InspectSqlAction({ sources }: { sources: Cited[] }) {
  const [open, setOpen] = useState(false);
  if (sources.length === 0) return null;
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        data-testid="footer-inspect-sql"
        className={`${UTIL_BTN}${open ? " border-border bg-surface-2 text-fg" : ""}`}
      >
        <Code2 className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
        <span>SQL</span>
      </button>
      {open && (
        <div
          className="order-last mt-2 w-full space-y-2.5 rounded-xl border border-border bg-surface/80 p-3"
          data-testid="footer-sql-panel"
        >
          {sources.map((s, i) => (
            <div key={i} className="space-y-1">
              <p className="text-[11.5px] font-medium text-muted">{s.about}</p>
              <pre className="overflow-x-auto whitespace-pre-wrap rounded-lg border border-border bg-bg p-2.5 font-mono text-[11.5px] leading-5 text-fg">
                {s.query}
              </pre>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

const ARTIFACT_ICON = {
  chart: BarChart3,
  table: Table2,
  document: FileText,
} as const;

function ArtifactStreamCard({
  artifact,
  active,
  onOpen,
}: {
  artifact: CanvasArtifact;
  active: boolean;
  onOpen: (artifact: CanvasArtifact) => void;
}) {
  const Icon = ARTIFACT_ICON[artifact.kind];
  return (
    <div
      data-testid="artifact-card"
      data-artifact-kind={artifact.kind}
      className={`group flex items-center gap-3 rounded-xl border px-3.5 py-2.5 transition-all duration-150 ${
        active
          ? "border-accent/60 bg-row-on"
          : "border-border-strong bg-surface/90 hover:border-accent/50 hover:bg-surface"
      }`}
    >
      <span className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-border bg-surface-2 text-accent" aria-hidden>
        <Icon className="size-4" strokeWidth={2} />
      </span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-[13.5px] leading-5 font-semibold text-fg">{artifact.title}</p>
        <p className="truncate text-[12px] leading-4 text-muted">{artifact.subtitle}</p>
      </div>
      <button
        type="button"
        onClick={() => onOpen(artifact)}
        data-testid="open-side-panel"
        className={`inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border px-2.5 text-[12px] font-medium transition-all duration-150 active:scale-[0.98] cursor-pointer ${
          active
            ? "border-accent/40 bg-accent-soft text-accent"
            : "border-border bg-bg text-fg hover:border-border-strong hover:bg-surface-2"
        }`}
      >
        <PanelRightOpen className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
        <span>{active ? "Viewing in side panel" : "Open in side panel"}</span>
      </button>
    </div>
  );
}

/** The simple Ask's answer: persistent collapsible Thought process, answer body, artifact cards, Sources, and a unified horizontal action bar. */
export function SimpleAnswer({
  turnId,
  answer,
  streamed,
  charts,
  question,
  spaceLabel,
  busy,
  status,
  progress,
  onAsk,
  onDeeper,
  onOpenArtifact,
  activeArtifactId,
  noteRecord,
  showNote = false,
}: {
  turnId?: string;
  answer?: Answer;
  streamed: string;
  charts?: AskChart[];
  question?: string;
  spaceLabel?: string;
  busy: boolean;
  status?: string;
  progress?: ReactNode;
  onAsk?: (q: string) => void;
  onDeeper?: () => void;
  onOpenArtifact?: (artifact: CanvasArtifact) => void;
  activeArtifactId?: string | null;
  noteRecord?: string;
  showNote?: boolean;
}) {
  // written out from the top as it comes (``useReveal``: the reply reaches the browser whole)
  const text = useReveal(answer ? answer.direct_answer : withoutChartBlocks(streamed), busy);
  const cited = answer?.cited ?? [];
  const n = sourceCount(cited);
  const activeCharts = (answer?.charts && answer.charts.length > 0 ? answer.charts : charts) ?? [];
  const chartQuestion = answer?.question ?? question;
  const pinnableChart = activeCharts.find((c) => !!c.sql);
  const followUps = (answer?.follow_ups ?? []).filter((q) => typeof q === "string" && q.trim().length > 0);
  const steps = answer ? calculationSteps(answer) : [];
  const sqlSources = cited.filter((t) => !!t.query);
  const answerQuestion = (answer?.question || question || "").trim();
  const artifacts = (!busy || activeCharts.length > 0) && !answer?.clarify && !answer?.assistant_unavailable
    ? artifactsFromAnswer(answer, activeCharts, answerQuestion, turnId ?? answer?.answer_id ?? "turn")
    : [];
  return (
    <article className="min-w-0 space-y-4 [overflow-wrap:anywhere]" aria-busy={busy} data-testid="simple-answer">
      {progress}
      {text ? (
        <AnswerText text={text} charts={activeCharts} chartQuestion={chartQuestion} />
      ) : (
        <>
          {activeCharts.length > 0 && <AnswerCharts charts={activeCharts} question={chartQuestion} />}
          {busy && !progress && <p className="text-[13px] text-muted">{status ?? "Working on it"}</p>}
        </>
      )}
      {onOpenArtifact && artifacts.length > 0 && (
        <div className="space-y-2" data-testid="artifact-cards">
          {artifacts.map((art) => (
            <ArtifactStreamCard
              key={art.id}
              artifact={art}
              active={activeArtifactId === art.id}
              onOpen={onOpenArtifact}
            />
          ))}
        </div>
      )}
      {answer && (answer.work_items ?? []).length > 0 && <WorkItems groups={answer.work_items ?? []} />}
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
      {answer && (((answer.coverage ?? []).length > 0 || (answer.untraced ?? []).length > 0 || (answer.working ?? []).length > 0 || steps.length > 0 || !!answer.check) || cited.length > 0) && (
        <div className="flex flex-wrap items-start gap-x-2 gap-y-1.5 pt-0.5" data-testid="simple-provenance">
          {((answer.coverage ?? []).length > 0 || (answer.untraced ?? []).length > 0 || (answer.working ?? []).length > 0 || steps.length > 0 || !!answer.check) && (() => {
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
              <details className="group rounded-xl border border-transparent transition-all duration-150 open:w-full open:border-border open:bg-surface/55" data-testid="simple-verification">
                <summary className="inline-flex h-7 cursor-pointer list-none items-center gap-1.5 rounded-lg border border-transparent px-2 py-1 text-[12px] font-medium text-muted transition-all duration-150 hover:border-border hover:bg-surface-2 hover:text-fg group-open:w-full group-open:rounded-xl group-open:border-transparent group-open:px-3 group-open:py-2 group-open:hover:bg-hover/60 [&::-webkit-details-marker]:hidden">
                  {flagged ? (
                    <ShieldAlert className="size-3.5 shrink-0 text-risk" strokeWidth={2} aria-hidden />
                  ) : (
                    <ShieldCheck className="size-3.5 shrink-0 text-ok" strokeWidth={2} aria-hidden />
                  )}
                  <span className="tabular-nums">{parts.join(" · ")}</span>
                  <ChevronRight className="ml-0.5 size-3.5 shrink-0 text-faint transition-transform duration-150 group-open:ml-auto group-open:rotate-90" strokeWidth={2} aria-hidden />
                </summary>
                <div className="space-y-2.5 border-t border-border/60 px-3.5 py-2.5 text-[12.5px]">
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
          {cited.length > 0 && (
            <details className="group open:w-full" data-testid="simple-sources">
              <summary className="inline-flex h-7 cursor-pointer list-none items-center gap-1.5 rounded-lg border border-transparent px-2 py-1 text-[12px] font-medium text-muted transition-all duration-150 hover:border-border hover:bg-surface-2 hover:text-fg [&::-webkit-details-marker]:hidden">
                <ChevronRight className="size-3.5 shrink-0 text-faint transition-transform duration-150 group-open:rotate-90" strokeWidth={2} aria-hidden />
                <span>Sources ({plural(n, "record")})</span>
              </summary>
              <div className="space-y-3 pt-2.5 pb-2 pl-2">
                <SourcesCoverage coverage={answer.coverage ?? []} notFound={answer.not_found ?? []} />
                {cited.map((t, i) => (
                  <CitedSource key={i} t={t} />
                ))}
                {(answer.cited_more ?? 0) > 0 && <p className="text-[12px] text-muted" data-testid="simple-sources-more">{plural(answer.cited_more ?? 0, "more result")} read, not listed here.</p>}
              </div>
            </details>
          )}
        </div>
      )}
      {answer && (answer.drafts ?? []).length > 0 && <AskDrafts drafts={answer.drafts ?? []} record={null} />}
      {answer && (answer.proposals ?? []).length > 0 && <ProposalCards proposals={answer.proposals} />}
      {answer && (answer.documents ?? []).length > 0 && (
        <ul className="space-y-1 text-[13px]" data-testid="simple-documents">
          {(answer.documents ?? []).map((d) => (
            <li key={d.download_url}>
              <a className="font-medium text-fg underline decoration-border-strong underline-offset-4 transition-colors hover:decoration-fg" href={d.download_url} download={d.name}>
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
        <section className="space-y-2" aria-label="Ask next" data-testid="simple-follow-ups">
          <p className="text-[11px] font-semibold uppercase tracking-[0.04em] text-faint">Ask next</p>
          <div className="flex flex-wrap gap-1.5">
            {followUps.map((q) => (
              <button
                key={q}
                type="button"
                onClick={() => onAsk?.(q)}
                disabled={!onAsk}
                className="inline-flex min-h-7 max-w-full items-center gap-1.5 rounded-xl border border-border bg-surface/65 px-3 py-1.5 text-left text-[12.5px] text-muted transition-all duration-150 hover:border-border-strong hover:bg-surface hover:text-fg active:scale-[0.99] disabled:cursor-default disabled:opacity-60 cursor-pointer"
              >
                <CornerDownRight className="size-3 shrink-0 text-faint" strokeWidth={2} aria-hidden />
                <span className="truncate">{q}</span>
              </button>
            ))}
          </div>
        </section>
      )}
      {answer && !answer.assistant_unavailable && (
        <footer className="flex flex-wrap items-center gap-0.5 border-t border-border/60 pt-2.5" data-testid="simple-answer-footer">
          {!answer.clarify && pinnableChart && (
            <PinToInsightsButton chart={pinnableChart} question={answerQuestion || "Pinned chart"} />
          )}
          {!answer.clarify && (
            <SaveAnswerButton question={answerQuestion} conversationId={answer.conversation_id} />
          )}
          {!answer.clarify && answerQuestion && (
            <AskSchedulePopover question={answerQuestion} spaceLabel={spaceLabel} />
          )}
          {!answer.clarify && showNote && <SaveInstructionAction record={noteRecord} />}
          {!answer.clarify && sqlSources.length > 0 && <InspectSqlAction sources={sqlSources} />}
          {!answer.clarify && (answer.direct_answer || cited.length > 0 || (answer.charts ?? []).length > 0) && (
            <>
              <ExcelExportButton
                title={answer.question || "Workbench answer"}
                label="Excel"
                className={UTIL_BTN}
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
                label="Email"
                className={UTIL_BTN}
              />
            </>
          )}
          <Feedback answerId={answer.answer_id} />
          {onDeeper && (
            <button
              type="button"
              onClick={onDeeper}
              className={UTIL_BTN}
              title="Takes longer and uses deeper analysis"
            >
              <Brain className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
              Deep research
            </button>
          )}
        </footer>
      )}
    </article>
  );
}
