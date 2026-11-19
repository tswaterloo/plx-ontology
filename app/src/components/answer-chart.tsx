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
  const png = !["table", "kpi"].includes(chart.kind) && !chart.panels?.length;
  return (
    <span className="inline-flex flex-wrap items-center gap-x-1 text-[12px] text-muted" aria-label="Save the chart">
      <span>Save as</span>
      {png && (
        <button type="button" className={quiet} disabled={!!busy}
          onClick={() => run("png", async () => {
            const s = svgs()[0];
            if (s) await savePng(s, chart.title, INK[theme].ground, INK[theme].text, { ...chartKey(chart, theme), notes: [...chartNotes(chart), ...(chart.as_of ? [`As of ${formatDate(chart.as_of)}.`] : [])], muted: INK[theme].muted });
          })}>
          PNG
        </button>
      )}
      {FILES.map((f) => (
        <button key={f.format} type="button" className={quiet} disabled={!!busy} onClick={() => run(f.format, () => saveFile(chart, f.format))}>
          {busy === f.format ? `${f.label}…` : f.label}
        </button>
      ))}
      {problem && <span className="ml-1">{problem}</span>}
    </span>
  );
}

const quiet = "h-7 px-1.5 font-medium text-fg underline-offset-2 hover:underline cursor-pointer disabled:cursor-default disabled:opacity-50";

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
