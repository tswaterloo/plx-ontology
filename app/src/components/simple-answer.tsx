"use client";

import type { ReactNode } from "react";
import { formatDate, plural } from "@/lib/format";
import { parseMarkdown } from "@/lib/markdown";
import type { Answer } from "@/lib/types";
import { Disclosure } from "./answer-parts";
import { AskDrafts } from "./ask-drafts";
import { Feedback } from "./feedback";
import { PreparedChanges } from "./prepared-change";
import { ProposalCards } from "./proposal-cards";
import { RichText } from "./rich-text";

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

function Table({ head, rows, caption }: { head: string[]; rows: (string | null)[][]; caption?: ReactNode }) {
  return (
    <div className="max-w-full overflow-x-auto">
      <table className="w-full border-collapse text-[13px]">
        {caption && <caption className="pb-1.5 text-left text-[12px] text-muted">{caption}</caption>}
        <thead>
          <tr>
            {head.map((h, i) => (
              <th key={i} className="border-b border-border px-2 py-1 text-left font-medium whitespace-nowrap">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-border/60 last:border-0">
              {head.map((_, j) => (
                <td key={j} className="num px-2 py-1 align-top">
                  {r[j] ?? ""}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The answer as written: prose in the body face, its tables as tables. */
export function AnswerText({ text, muted = false }: { text: string; muted?: boolean }) {
  return (
    <div className={muted ? "space-y-3 text-muted" : "space-y-3"} data-testid="simple-answer-text">
      {splitTables(text).map((s, i) =>
        s.kind === "table" ? <Table key={i} head={s.head} rows={s.rows} /> : <RichText key={i} blocks={parseMarkdown(s.text)} className="space-y-2 text-[15px] leading-relaxed" />,
      )}
    </div>
  );
}

/** The simple Ask's answer: the answer, then one collapsed "Sources (n records)" line that opens to the rows it read. */
export function SimpleAnswer({ answer, streamed, busy, status }: { answer?: Answer; streamed: string; busy: boolean; status?: string }) {
  const text = answer ? answer.direct_answer : streamed;
  const cited = answer?.cited ?? [];
  const n = sourceCount(cited);
  return (
    <article className="min-w-0 space-y-5 [overflow-wrap:anywhere]" aria-busy={busy} data-testid="simple-answer">
      {text ? <AnswerText text={text} /> : busy && <p className="text-[13px] text-muted">{status ?? "Working on it"}</p>}
      {busy && text && status && <p className="text-[12px] text-muted">{status}</p>}
      {answer && cited.length > 0 && (
        <Disclosure label={`Sources (${plural(n, "record")})`} testId="simple-sources">
          {cited.map((t, i) => (
            <Table key={i} head={t.columns} rows={t.rows} caption={`${t.about}${t.as_of ? `, as of ${formatDate(t.as_of)}` : ""}${t.rows.length < t.records ? ` (first ${t.rows.length} of ${t.records})` : ""}`} />
          ))}
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
            </li>
          ))}
        </ul>
      )}
      {answer && (answer.prepared ?? []).length > 0 && <PreparedChanges ids={answer.prepared ?? []} />}
      {answer && (
        <footer className="flex items-center gap-1">
          <Feedback answerId={answer.answer_id} />
        </footer>
      )}
    </article>
  );
}
