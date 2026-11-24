"use client";

import type { Answer } from "@/lib/types";
import Link from "next/link";
import { useRef, useState } from "react";
import { ChartView, chartKey, chartNotes, useChartTheme } from "@/components/charts/chart-view";
import { FILES, saveFile, savePng } from "@/components/charts/export";
import { INK } from "@/components/charts/scale";
import { saveInsight } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { formatDate } from "@/lib/format";

export type AskChart = NonNullable<Answer["charts"]>[number];

/** Save a chart: PNG of the drawing, or Excel / PowerPoint / Word / PDF made from it. */
function ChartSaves({ chart, figure }: { chart: AskChart; figure: React.RefObject<HTMLElement | null> }) {
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
        <summary className={`${quiet} inline-flex list-none items-center`} aria-label="Save the chart">
          {busy ? "Saving" : "Save as"}
        </summary>
        <div className="absolute left-0 z-10 mt-1 flex min-w-[9rem] flex-col rounded-md border border-border bg-bg py-1 shadow-sm" role="menu">
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
        </div>
      </details>
      {problem && <span className="ml-1" role="alert">{problem}</span>}
    </span>
  );
}

const quiet = "h-7 px-1.5 font-medium text-fg underline-offset-2 hover:underline cursor-pointer disabled:cursor-default disabled:opacity-50";
const item = "px-3 py-1 text-left text-[12px] text-fg hover:bg-hover cursor-pointer disabled:cursor-default disabled:opacity-50";

/** The charts an answer drew, each from the rows of one query result it read. */
/** Pin a chart to Insights: its statement reads again, as whoever opens it, every time. */
function PinChart({ chart, question }: { chart: AskChart; question: string }) {
  const [state, setState] = useState<"idle" | "saving" | "pinned" | "failed">("idle");
  const [problem, setProblem] = useState("");
  if (!chart.sql) return null;
  const pin = async () => {
    setState("saving");
    try {
      await saveInsight({
        title: (chart.title || question).slice(0, 160) || "Pinned chart",
        question: question.slice(0, 2000),
        sql: chart.sql ?? "",
        chart: chart.spec ?? { kind: chart.kind, x: chart.x, y: chart.series.map((s) => s.name) },
        schedule: "",
        notify: "never",
      });
      setState("pinned");
    } catch (err) {
      setProblem(plainError(err));
      setState("failed");
    }
  };
  return (
    <span className="text-[12px]">
      {state === "pinned" ? (
        <Link href="/insights" className="text-accent hover:underline">Pinned to your charts</Link>
      ) : (
        <button type="button" onClick={pin} disabled={state === "saving"} className="h-7 rounded-md px-2 font-medium hover:bg-hover cursor-pointer disabled:opacity-50">
          {state === "saving" ? "Pinning" : "Pin to your charts"}
        </button>
      )}
      {state === "failed" && <span className="ml-2 text-muted">{problem}</span>}
    </span>
  );
}

/** Pin every chart of an answer at once: the answer becomes a set of live charts on Insights. */
function PinAll({ charts, question }: { charts: AskChart[]; question: string }) {
  const [state, setState] = useState<"idle" | "saving" | "pinned" | "failed">("idle");
  const [problem, setProblem] = useState("");
  const pinnable = charts.filter((c) => c.sql);
  if (pinnable.length < 2) return null;
  const pin = async () => {
    setState("saving");
    try {
      for (const chart of pinnable) {
        await saveInsight({
          title: (chart.title || question).slice(0, 160) || "Pinned chart",
          question: question.slice(0, 2000),
          sql: chart.sql ?? "",
          chart: chart.spec ?? { kind: chart.kind, x: chart.x, y: chart.series.map((s) => s.name) },
          schedule: "",
          notify: "never",
        });
      }
      setState("pinned");
    } catch (err) {
      setProblem(plainError(err));
      setState("failed");
    }
  };
  return (
    <div className="text-[12px]">
      {state === "pinned" ? (
        <Link href="/insights" className="text-accent hover:underline">All {pinnable.length} charts pinned to your charts</Link>
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
function HowComputed({ chart }: { chart: AskChart }) {
  const m = chart.measure;
  if (!m && !chart.sql) return null;
  return (
    <div className="space-y-1 text-[12px] text-muted" data-testid="chart-definition">
      {m && (
        <p>
          <span className="font-medium text-fg">{m.name}</span>
          {m.owner ? <span> ({m.owner})</span> : null}: {m.definition}
        </p>
      )}
      {chart.sql && (
        <details>
          <summary className="cursor-pointer underline-offset-2 hover:underline">How this was worked out</summary>
          {m?.period && <p className="mt-1">Period: {m.period}</p>}
          <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border p-2 font-mono text-[11px] text-fg">{chart.sql}</pre>
        </details>
      )}
    </div>
  );
}

export function AnswerCharts({ charts, question, caption = true }: { charts: AskChart[]; question?: string; caption?: boolean }) {
  if (charts.length === 0) return null;
  return (
    <div className="space-y-6" data-testid="answer-charts">
      {question !== undefined && <PinAll charts={charts} question={question} />}
      {charts.map((c, i) => <OneChart key={i} chart={c} question={question} caption={caption} />)}
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

function OneChart({ chart, question, caption }: { chart: AskChart; question?: string; caption: boolean }) {
  const figure = useRef<HTMLElement>(null);
  const [all, setAll] = useState(false);
  const cut = firstBars(chart);
  return (
    <figure ref={figure} className="space-y-1.5">
      {caption && <figcaption className="text-[14px] font-semibold">{chart.title}</figcaption>}
      <ChartView chart={all ? chart : cut} />
      {cut !== chart && (
        <button type="button" className="cursor-pointer text-[12px] text-accent hover:underline" onClick={() => setAll((a) => !a)} data-testid="chart-all">
          {all ? `Show the first ${ANSWER_BARS}` : `Showing the first ${ANSWER_BARS} of ${chart.labels.length}: show all`}
        </button>
      )}
      <div className="flex flex-wrap items-center gap-x-3 text-[12px] text-muted">
        {chart.as_of && <span>As of {formatDate(chart.as_of)}</span>}
        <ChartSaves chart={chart} figure={figure} />
        {question !== undefined && <PinChart chart={chart} question={question} />}
      </div>
      {question !== undefined && <HowComputed chart={chart} />}
    </figure>
  );
}

/** Pure: streamed answer text without a chart block the model is still writing. */
export function withoutChartBlocks(text: string): string {
  // a chart, a question asked back or a declared gap: blocks the app reads, never shown as text
  return text.replace(/```(?:chart|clarify|gap)\b[\s\S]*?(```|$)/gi, "").trimEnd();
}
