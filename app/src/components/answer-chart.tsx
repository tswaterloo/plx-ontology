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

export function AnswerCharts({ charts, question, caption = true }: { charts: AskChart[]; question?: string; caption?: boolean }) {
  if (charts.length === 0) return null;
  return (
    <div className="space-y-6" data-testid="answer-charts">
      {charts.map((c, i) => <OneChart key={i} chart={c} question={question} caption={caption} />)}
    </div>
  );
}

function OneChart({ chart, question, caption }: { chart: AskChart; question?: string; caption: boolean }) {
  const figure = useRef<HTMLElement>(null);
  return (
    <figure ref={figure} className="space-y-1.5">
      {caption && <figcaption className="text-[14px] font-semibold">{chart.title}</figcaption>}
      <ChartView chart={chart} />
      <div className="flex flex-wrap items-center gap-x-3 text-[12px] text-muted">
        {chart.as_of && <span>As of {formatDate(chart.as_of)}</span>}
        <ChartSaves chart={chart} figure={figure} />
        {question !== undefined && <PinChart chart={chart} question={question} />}
      </div>
    </figure>
  );
}

/** Pure: streamed answer text without a chart block the model is still writing. */
export function withoutChartBlocks(text: string): string {
  return text.replace(/```chart[\s\S]*?(```|$)/g, "").trimEnd();
}
