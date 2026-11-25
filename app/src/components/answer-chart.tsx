"use client";

import { BarChart3, ChevronDown, Download, ShieldCheck, Table2 } from "lucide-react";
import type { Answer, StatementRead } from "@/lib/types";
import Link from "next/link";
import { useContext, useRef, useState } from "react";
import { PinnedHere } from "@/components/insights/pinned";
import { ChartView, chartKey, chartNotes, useChartTheme } from "@/components/charts/chart-view";
import { FILES, chartToTable, saveChartCsv, saveFile, savePng } from "@/components/charts/export";
import { EmailDraftButton } from "@/components/email-draft-button";
import { INK, full } from "@/components/charts/scale";
import { QueryToggle, ShowWork } from "@/components/query-work";
import { proposeMeasureCandidate, readStatement, saveInsight } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { formatDate } from "@/lib/format";

export type AskChart = NonNullable<Answer["charts"]>[number];
type ChartSpec = NonNullable<AskChart["spec"]>;

/** Save or share a chart: PNG of the drawing, Excel / PowerPoint / Word / PDF, and optional Email this. */
export function ChartSaves({
  chart,
  figure,
  label = "Save as",
  align = "left",
  includeEmail = false,
  question,
}: {
  chart: AskChart;
  figure: React.RefObject<HTMLElement | null>;
  label?: string;
  align?: "left" | "right";
  includeEmail?: boolean;
  question?: string;
}) {
  const theme = useChartTheme();
  const [busy, setBusy] = useState("");
  const [problem, setProblem] = useState("");
  const run = async (what: string, job: () => Promise<void>) => {
    setBusy(what);
    setProblem("");
    try {
      await job();
    } catch (err) {
      setProblem(plainError(err));
    } finally {
      setBusy("");
    }
  };
  const svgs = () => Array.from(figure.current?.querySelectorAll<SVGSVGElement>("svg[data-chart-svg]") ?? []);
  // tables and KPI tiles are not drawings: Excel / PowerPoint / Word / PDF carry them
  const png = !["table", "kpi"].includes(chart.kind);
  const menu = useRef<HTMLDetailsElement>(null);
  const pick = (what: string, job: () => Promise<void>) => {
    if (menu.current) menu.current.open = false;
    return run(what, job);
  };
  return (
    <span className="inline-flex items-center gap-x-1 text-[12px] text-muted">
      <details ref={menu} className="relative">
        <summary className={saveTrigger} aria-label="Save the chart">
          <Download className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
          <span>{busy ? "Saving" : label}</span>
          <ChevronDown className="size-3 shrink-0 opacity-70" strokeWidth={2} aria-hidden />
        </summary>
        <div
          className={`absolute ${align === "right" ? "right-0" : "left-0"} z-20 mt-1 flex min-w-[10.5rem] flex-col rounded-lg border border-border bg-raised py-1 shadow-pop`}
          role="menu"
        >
          {png && (
            <button type="button" role="menuitem" className={item} disabled={!!busy}
              onClick={() => pick("png", async () => {
                const s = svgs();
                const panels = chart.panels?.length ? chart.panels.map((p) => p.title) : undefined;
                if (s.length) await savePng(panels ? s : s[0], chart.title, INK[theme].ground, INK[theme].text, { ...chartKey(chart, theme), panels, notes: [...chartNotes(chart), ...(chart.as_of ? [`As of ${formatDate(chart.as_of)}.`] : [])], muted: INK[theme].muted });
              })}>
              Picture (PNG)
            </button>
          )}
          {FILES.map((f) => (
            <button key={f.format} type="button" role="menuitem" className={item} disabled={!!busy} onClick={() => pick(f.format, () => saveFile(chart, f.format))}>
              {f.label}
            </button>
          ))}
          <button
            type="button"
            role="menuitem"
            className={item}
            disabled={!!busy}
            onClick={() => pick("csv", async () => { saveChartCsv(chart); })}
          >
            CSV
          </button>
          {includeEmail && (
            <div className="mt-1 border-t border-border px-2 pt-1.5 pb-1">
              <EmailDraftButton title={chart.title || question || "Chart"} summary={question || ""} charts={[chart as unknown as Record<string, unknown>]} />
            </div>
          )}
        </div>
      </details>
      {problem && <span className="ml-1" role="alert">{problem}</span>}
    </span>
  );
}

const saveTrigger = "inline-flex h-8 list-none items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 text-[12.5px] font-medium text-muted hover:border-border-strong hover:bg-hover hover:text-fg cursor-pointer [&::-webkit-details-marker]:hidden";
const item = "px-3 py-1 text-left text-[12px] text-fg hover:bg-hover cursor-pointer disabled:cursor-default disabled:opacity-50";

/** The charts an answer drew, each from the rows of one query result it read. */
/** Propose an ad-hoc chart's SQL as a candidate shared measure definition for owner review. */
function ProposeChartDefinition({ chart, question }: { chart: AskChart; question: string }) {
  const [state, setState] = useState<"idle" | "saving" | "sent" | "failed">("idle");
  const [problem, setProblem] = useState("");
  if (!chart.sql || chart.measure) return null;
  const propose = async () => {
    setState("saving");
    setProblem("");
    try {
      await proposeMeasureCandidate({
        title: chart.title || undefined,
        question: question || undefined,
        sql: chart.sql ?? "",
        source_kind: "manual",
      });
      setState("sent");
    } catch (err) {
      setProblem(plainError(err));
      setState("failed");
    }
  };
  return (
    <span className="text-[12px]">
      {state === "sent" ? (
        <span data-testid="chart-proposed">Proposed for review</span>
      ) : (
        <button type="button" onClick={propose} disabled={state === "saving"} className="h-7 rounded-md px-2 font-medium hover:bg-hover cursor-pointer disabled:opacity-50" data-testid="chart-propose">
          {state === "saving" ? "Proposing" : "Propose as shared definition"}
        </button>
      )}
      {state === "failed" && problem && <span className="ml-2 text-muted" role="alert">{problem}</span>}
    </span>
  );
}

/** Pure: build a valid ChartSpec payload for POST /api/insights when pinning a chart. */
export function pinChartSpec(chart: AskChart): ChartSpec | undefined {
  if (chart.spec) return chart.spec;
  const rawSeries = chart.series.length > 0 ? chart.series : (chart.panels?.[0]?.series ?? []);
  const y = rawSeries.map((s) => s.name).filter(Boolean);
  if (y.length === 0) return undefined;
  const rawKind = chart.kind as string;
  const isStacked = rawKind === "stacked_bar";
  const kind = (isStacked ? "bar" : chart.kind) as ChartSpec["kind"];
  return {
    kind,
    x: chart.x ?? null,
    y,
    ...(isStacked ? { stacked: true } : {}),
  };
}

/** Pin a chart to Insights: its statement reads again, as whoever opens it, every time. */
function PinChart({ chart, question }: { chart: AskChart; question: string }) {
  const here = useContext(PinnedHere);
  const [state, setState] = useState<"idle" | "saving" | "pinned" | "failed">("idle");
  const [problem, setProblem] = useState("");
  if (!chart.sql) return null;
  const pin = async () => {
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
      here?.();
    } catch (err) {
      setProblem(plainError(err));
      setState("failed");
    }
  };
  return (
    <span className="text-[12px]">
      {state === "pinned" ? (
        here ? <span>Added to your charts below</span> : <Link href="/insights" className="text-accent hover:underline">Pinned to your charts</Link>
      ) : (
        <button type="button" onClick={pin} disabled={state === "saving"} className="h-7 rounded-md px-2 font-medium hover:bg-hover cursor-pointer disabled:opacity-50" data-testid="chart-pin">
          {state === "saving" ? "Pinning" : "Pin to your charts"}
        </button>
      )}
      {state === "failed" && <span className="ml-2 text-muted" role="alert">{problem}</span>}
    </span>
  );
}

/** Pin every chart of an answer at once: the answer becomes a set of live charts on Insights. */
function PinAll({ charts, question }: { charts: AskChart[]; question: string }) {
  const here = useContext(PinnedHere);
  const [state, setState] = useState<"idle" | "saving" | "pinned" | "failed">("idle");
  const [problem, setProblem] = useState("");
  const pinnable = charts.filter((c) => c.sql);
  if (pinnable.length < 2) return null;
  const pin = async () => {
    setState("saving");
    setProblem("");
    try {
      for (const chart of pinnable) {
        await saveInsight({
          title: (chart.title || question).slice(0, 160) || "Pinned chart",
          question: question.slice(0, 2000),
          sql: chart.sql ?? "",
          chart: pinChartSpec(chart),
          schedule: "",
          notify: "never",
        });
      }
      setState("pinned");
      here?.();
    } catch (err) {
      setProblem(plainError(err));
      setState("failed");
    }
  };
  return (
    <div className="text-[12px]">
      {state === "pinned" ? (
        here ? <span>All {pinnable.length} charts added to your charts below</span> : <Link href="/insights" className="text-accent hover:underline">All {pinnable.length} charts pinned to your charts</Link>
      ) : (
        <button type="button" onClick={pin} disabled={state === "saving"} className="h-7 rounded-md border border-border px-2 font-medium hover:bg-hover cursor-pointer disabled:opacity-50">
          {state === "saving" ? "Pinning" : `Pin all ${pinnable.length} charts`}
        </button>
      )}
      {state === "failed" && <span className="ml-2 text-muted">{problem}</span>}
    </div>
  );
}

/** What a chart's numbers mean and how they were read: the agreed measure's definition, then
 * the period and the statement itself, a click away. */
const num = (c: string | null | undefined) => (c === null || c === undefined || c === "" || !Number.isFinite(Number(c)) ? null : Number(c));

/** Pure: the "checked" line: the statement read again now, its rows added up against the
 * figures the chart draws, series by series; said not compared when the chart draws other
 * points than the statement's rows (a split, panels, a cut). */
export function checkedLine(chart: Pick<AskChart, "labels" | "series" | "panels">, read: Pick<StatementRead, "columns" | "rows" | "row_count" | "truncated">): { ok: boolean | null; says: string } {
  if (chart.panels?.length || read.truncated || chart.labels.length !== read.row_count)
    return { ok: null, says: `Not compared: the chart draws ${chart.labels.length} points and the statement read now gives ${read.row_count} rows${read.truncated ? " (more than shown)" : ""}.` };
  const parts: string[] = [];
  let all = true;
  for (const s of chart.series) {
    const at = read.columns.indexOf(s.name);
    if (at < 0) continue;
    const fresh = read.rows.reduce((t, r) => t + (num(r[at]) ?? 0), 0);
    const drawn = s.values.reduce((t: number, v) => t + (typeof v === "number" ? v : 0), 0);
    const same = Math.abs(fresh - drawn) <= 0.005 * Math.max(1, read.row_count);
    all = all && same;
    parts.push(`${s.name} ${same ? "adds up to" : `reads ${fresh.toLocaleString()} now but the chart shows`} ${drawn.toLocaleString()}`);
  }
  if (parts.length === 0) return { ok: null, says: "Not compared: no drawn series is a column of the statement." };
  return { ok: all, says: `Read again now: ${parts.join("; ")}.` };
}

/** The statement behind a chart: read once when opened, its views and filters (a filter can be changed or taken out,
 * read as you, nothing saved), the rows, and whether they add up to what the chart draws. */
function ChartWork({ chart }: { chart: AskChart }) {
  const [first, setFirst] = useState<StatementRead | null>(null);
  const [now, setNow] = useState<StatementRead | null>(null);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const open = async () => {
    if (first || busy) return;
    setBusy(true);
    try {
      const got = await readStatement(chart.sql ?? "", []);
      setFirst(got);
      setNow(got);
    } catch (e) {
      setProblem(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const shown = now ?? first;
  const check = first && now === first ? checkedLine(chart, first) : null;
  return (
    <details onToggle={(e) => { if ((e.currentTarget as HTMLDetailsElement).open) void open(); }}>
      <summary className="cursor-pointer underline-offset-2 hover:underline">How this was worked out</summary>
      {chart.measure?.period && <p className="mt-1">Period: {chart.measure.period}</p>}
      {busy && <p className="mt-1">Reading the statement</p>}
      {problem && <p className="mt-1" role="alert">{problem}</p>}
      {first?.work && (
        <ShowWork work={first.work} testId="chart-work" read={async (edits) => setNow(await readStatement(chart.sql ?? "", edits))} onReset={() => setNow(first)} />
      )}
      {check && <p className={check.ok === false ? "mt-1 text-late" : "mt-1"} data-testid="chart-checked">Checked: {check.says}</p>}
      {now && now !== first && <p className="mt-1" data-testid="chart-filtered">Your filters (not saved): {now.row_count} rows{now.truncated ? " (more than shown)" : ""}.</p>}
      {shown && shown.rows.length > 0 && (
        <div className="mt-1 max-h-60 overflow-auto">
          <table className="w-full text-left text-[11px]">
            <thead className="sticky top-0 bg-bg"><tr>{shown.columns.map((c) => <th key={c} className="px-2 py-0.5 font-medium">{c}</th>)}</tr></thead>
            <tbody>{shown.rows.map((r, i) => <tr key={i} className="border-t border-border">{r.map((c, j) => <td key={j} className="px-2 py-0.5 tabular-nums">{c}</td>)}</tr>)}</tbody>
          </table>
        </div>
      )}
      <QueryToggle sql={now?.sql ?? chart.sql ?? ""} testId="chart-query" />
    </details>
  );
}

function HowComputed({ chart }: { chart: AskChart }) {
  const m = chart.measure;
  if (!m && !chart.sql) return null;
  return (
    <div className="space-y-1 text-[12px] text-muted" data-testid="chart-definition">
      {m && (
        <p className="leading-5">
          <span
            className="mr-1.5 inline-flex items-center gap-1 rounded border border-ok/30 bg-ok/10 px-1.5 py-0.5 align-middle text-[11px] font-medium leading-none text-ok"
            data-testid="verified-measure-badge"
          >
            <ShieldCheck className="size-3 shrink-0" strokeWidth={2.2} aria-hidden />
            Verified measure
          </span>
          <span className="font-medium text-fg">{m.name}</span>
          {m.owner ? <span> ({m.owner})</span> : null}: {m.definition}
        </p>
      )}
      {chart.sql && <ChartWork chart={chart} />}
    </div>
  );
}

/** Interactive data table view of any drawn chart (single-series, multi-series, scatter, or faceted panels). */
function ChartDataTable({ chart }: { chart: AskChart }) {
  const { columns, rows } = chartToTable(chart);
  const numericCol = columns.map((_, j) => {
    const filled = rows.map((r) => r[j]).filter((c) => c !== null && c !== undefined && c !== "");
    return filled.length > 0 && filled.every((c) => typeof c === "number");
  });
  return (
    <div className="max-h-80 overflow-auto rounded-md border border-border" data-testid="chart-data-table">
      <table className="w-full border-collapse text-left text-[12px]">
        <thead className="sticky top-0 bg-bg text-muted">
          <tr className="border-b border-border">
            {columns.map((c, j) => (
              <th key={`${c}-${j}`} className={`px-2.5 py-1.5 font-medium whitespace-nowrap ${numericCol[j] ? "text-right" : ""}`}>
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-border/70 first:border-t-0 hover:bg-hover/40">
              {r.map((cell, j) => (
                <td key={j} className={`px-2.5 py-1.5 tabular-nums ${numericCol[j] ? "text-right whitespace-nowrap" : ""}`}>
                  {typeof cell === "number" ? full(cell, chart.currency) : (cell ?? "")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function AnswerCharts({ charts, question, caption = true, hideFooter = false }: { charts: AskChart[]; question?: string; caption?: boolean; hideFooter?: boolean }) {
  if (charts.length === 0) return null;
  return (
    <div className="space-y-6" data-testid="answer-charts">
      {question !== undefined && <PinAll charts={charts} question={question} />}
      {charts.map((c, i) => <OneChart key={i} chart={c} question={question} caption={caption} hideFooter={hideFooter} />)}
    </div>
  );
}

/** Bars past this many names show the first ones and a "Show all" switch (a line keeps every point). */
export const ANSWER_BARS = 60;

/** Pure: a bar chart cut to its first `n` names; any other chart, or one with no more names, as it is. */
export function firstBars(chart: AskChart, n = ANSWER_BARS): AskChart {
  if (!["bar", "stacked_bar"].includes(chart.kind) || chart.panels?.length || chart.labels.length <= n) return chart;
  return { ...chart, labels: chart.labels.slice(0, n), series: chart.series.map((s) => ({ ...s, values: s.values.slice(0, n) })) };
}

function OneChart({ chart, question, caption, hideFooter = false }: { chart: AskChart; question?: string; caption: boolean; hideFooter?: boolean }) {
  const figure = useRef<HTMLElement>(null);
  const [all, setAll] = useState(false);
  const [view, setView] = useState<"chart" | "table">("chart");
  const cut = firstBars(chart);
  const canToggleTable = !hideFooter && chart.kind !== "table";
  return (
    <figure ref={figure} className="space-y-1.5">
      {caption && <figcaption className="text-[14px] font-semibold">{chart.title}</figcaption>}
      {view === "table" && canToggleTable ? (
        <ChartDataTable chart={chart} />
      ) : (
        <ChartView chart={all ? chart : cut} />
      )}
      {view === "chart" && cut !== chart && (
        <button type="button" className="cursor-pointer text-[12px] text-accent hover:underline" onClick={() => setAll((a) => !a)} data-testid="chart-all">
          {all ? `Show the first ${ANSWER_BARS}` : `Showing the first ${ANSWER_BARS} of ${chart.labels.length}: show all`}
        </button>
      )}
      {!hideFooter && (
        <div className="flex flex-wrap items-center gap-2 text-[12px] text-muted">
          {chart.as_of && <span>As of {formatDate(chart.as_of)}</span>}
          {canToggleTable && (
            <div className="inline-flex items-center rounded-md border border-border bg-bg p-0.5" role="group" aria-label="Visualization view" data-testid="chart-view-toggle">
              <button
                type="button"
                aria-pressed={view === "chart"}
                onClick={() => setView("chart")}
                className={`inline-flex h-6 items-center gap-1 rounded px-2 text-[11.5px] font-medium cursor-pointer ${view === "chart" ? "bg-selected text-fg" : "text-muted hover:text-fg"}`}
              >
                <BarChart3 className="size-3 shrink-0" strokeWidth={2} aria-hidden />
                Chart
              </button>
              <button
                type="button"
                aria-pressed={view === "table"}
                onClick={() => setView("table")}
                className={`inline-flex h-6 items-center gap-1 rounded px-2 text-[11.5px] font-medium cursor-pointer ${view === "table" ? "bg-selected text-fg" : "text-muted hover:text-fg"}`}
              >
                <Table2 className="size-3 shrink-0" strokeWidth={2} aria-hidden />
                Table
              </button>
            </div>
          )}
          <ChartSaves chart={chart} figure={figure} />
          <EmailDraftButton title={chart.title || question || "Chart"} summary={question || ""} charts={[chart as unknown as Record<string, unknown>]} />
          {question !== undefined && <PinChart chart={chart} question={question} />}
          {question !== undefined && <ProposeChartDefinition chart={chart} question={question} />}
        </div>
      )}
      {question !== undefined && <HowComputed chart={chart} />}
    </figure>
  );
}

/** Pure: streamed answer text without a chart block the model is still writing. */
export function withoutChartBlocks(text: string): string {
  // a chart, a question asked back, a declared gap or the items list: blocks the app reads, never shown as text
  return text.replace(/```(?:chart|clarify|gap|items)\b[\s\S]*?(```|$)/gi, "").trimEnd();
}
