"use client";

import { Brain } from "lucide-react";
import { useState, type ReactNode } from "react";
import { formatDate, plural } from "@/lib/format";
import { useReveal } from "@/lib/reveal";
import { parseMarkdown } from "@/lib/markdown";
import type { Answer, StatementRead } from "@/lib/types";
import { readStatement } from "@/lib/api";
import { ShowWork } from "./query-work";
import { EmailDraftButton } from "./email-draft-button";
import { ExcelExportButton } from "./excel-export-button";
import { Disclosure } from "./answer-parts";
import { DefinitionLines } from "./definition-lines";
import { AnswerCharts, withoutChartBlocks } from "./answer-chart";
import { AskDrafts } from "./ask-drafts";
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
const NOWRAP_CELL = /^(?:[-+(]?[$€£]?\d[\d,]*(?:\.\d+)?%?\)?(?:\s+[A-Za-z]{1,4})?|\d{1,2}\s+[A-Za-z]{3}\s+\d{4}|\S{1,24}|[A-Za-z0-9][A-Za-z0-9 .,'/-]{0,17})$/;

// a column whose every filled cell is a number (an amount, a count, a percentage, a duration
// with a unit) lines up on the right, in the page's own font with tabular digits
const NUMBER_CELL = /^[-+(]?[$€£]?\d[\d,]*(\.\d+)?%?\)?(\s?[A-Za-z]{1,4}\b)?(\s\(unverified\))?$/;
const numericColumn = (rows: (string | null)[][], j: number) => {
  const cells = rows.map((r) => (r[j] ?? "").replace(/\*\*/g, "").trim()).filter((c) => c !== "");
  return cells.length > 0 && cells.every((c) => NUMBER_CELL.test(c));
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
    <div className="max-w-full overflow-x-auto rounded-lg border border-border bg-surface/40">
      <table className="w-full border-collapse text-[13.5px]">
        {caption && <caption className="border-b border-border/70 bg-surface/70 px-3 py-2 text-left text-[12px] text-muted">{caption}</caption>}
        <thead>
          <tr className="bg-surface/70">
            {head.map((h, i) => (
              <th key={i} className={`border-b border-border px-3 py-2.5 text-[12px] font-medium text-muted whitespace-nowrap ${right[i] ? "text-right" : "text-left"}`}>
                {written ? <Inlines xs={inlines(h)} /> : h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((r, i) => (
            <tr key={i} className="border-b border-border/60 last:border-0 hover:bg-hover/40">
              {head.map((_, j) => {
                const raw = (r[j] ?? "").replace(/\*\*/g, "").trim();
                const nowrap = right[j] || NOWRAP_CELL.test(raw);
                return (
                  <td key={j} className={`px-3 py-2.5 align-top tabular-nums${right[j] ? " text-right" : ""}${nowrap ? " whitespace-nowrap" : ""}`}>
                    {cell(r[j] ?? "")}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {(rows.length > FIRST_ROWS || (!written && rows.length > 0)) && (
        <div className="flex flex-wrap items-center gap-x-3 border-t border-border/70 bg-surface/50 px-3 py-2">
          {rows.length > FIRST_ROWS && (
            <button type="button" className="cursor-pointer text-[12px] font-medium text-accent underline-offset-2 hover:underline" aria-expanded={all} onClick={() => setAll((a) => !a)}>
              {all ? "Show fewer rows" : `Show all ${rows.length.toLocaleString("en-US")} rows`}
            </button>
          )}
          {!written && rows.length > 0 && (
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
          )}
        </div>
      )}
    </div>
  );
}

const FIRST_ROWS = 100;

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
    <div className="space-y-1">
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

export function AnswerText({ text, muted = false }: { text: string; muted?: boolean }) {
  return (
    <div className={muted ? "space-y-3 text-muted" : "space-y-3"} data-testid="simple-answer-text">
      {splitTables(text).map((s, i) =>
        s.kind === "table" ? <Table key={i} head={s.head} rows={s.rows} written /> : <RichText key={i} blocks={parseMarkdown(s.text)} className="space-y-2 text-[15px] leading-relaxed" />,
      )}
    </div>
  );
}

/** The simple Ask's answer: the answer, then one collapsed "Sources (n records)" line and a unified horizontal action bar. */
export function SimpleAnswer({
  answer,
  streamed,
  busy,
  status,
  progress,
  onDeeper,
  noteRecord,
  showNote = false,
}: {
  answer?: Answer;
  streamed: string;
  busy: boolean;
  status?: string;
  progress?: ReactNode;
  onDeeper?: () => void;
  noteRecord?: string;
  showNote?: boolean;
}) {
  // written out from the top as it comes (``useReveal``: the reply reaches the browser whole)
  const text = useReveal(answer ? answer.direct_answer : withoutChartBlocks(streamed), busy);
  const cited = answer?.cited ?? [];
  const n = sourceCount(cited);
  return (
    <article className="min-w-0 space-y-4 [overflow-wrap:anywhere]" aria-busy={busy} data-testid="simple-answer">
      {text ? <AnswerText text={text} /> : busy && !progress && <p className="text-[13px] text-muted">{status ?? "Working on it"}</p>}
      {answer && (answer.work_items ?? []).length > 0 && <WorkItems groups={answer.work_items ?? []} />}
      {busy && progress}
      {busy && !progress && text && status && <p className="text-[12px] text-muted">{status}</p>}
      {answer && (answer.charts ?? []).length > 0 && <AnswerCharts charts={answer.charts ?? []} question={answer.question} />}
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
      {answer && (answer.coverage ?? []).length > 0 && (
        <div className="text-[13px]" data-testid="simple-coverage">
          <p className="text-muted">What was read</p>
          <ul className="space-y-0.5">
            {(answer.coverage ?? []).map((p, i) => (
              <li key={i}>
                {p.part}
                {p.period ? ` (${p.period})` : ""}: <span className="text-muted">{partLine(p)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {answer && (answer.untraced ?? []).length > 0 && (
        <p className="text-[12px] text-muted" data-testid="simple-untraced">Not traced to a record: {(answer.untraced ?? []).join(", ")}.</p>
      )}
      {answer && (answer.working ?? []).length > 0 && (
        <Disclosure label={`Working (${(answer.working ?? []).length})`} testId="simple-working">
          <ul className="text-[12px] text-muted">
            {(answer.working ?? []).map((w) => <li key={w}>{w}</li>)}
          </ul>
        </Disclosure>
      )}
      {answer?.check && <p className="text-[12px] text-muted" data-testid="simple-check">{checkLine(answer.check)}</p>}
      {answer && cited.length > 0 && (
        <Disclosure label={`Sources (${plural(n, "record")})`} testId="simple-sources">
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
                        {c.before && <del className="text-red-700">{c.before}</del>}
                        {c.before && c.after && " "}
                        {c.after && <ins className="text-green-800 no-underline">{c.after}</ins>}
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
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 text-[12.5px] font-medium text-accent hover:border-accent/50 hover:bg-hover cursor-pointer"
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
