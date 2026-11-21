"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { AnswerCharts } from "@/components/answer-chart";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { deleteInsight, getInsight, setInsightSchedule } from "@/lib/api";
import { noticeKind, plainError, tileError } from "@/lib/errors";
import { formatDate, formatDateTime } from "@/lib/format";
import { human } from "@/components/charts/scale";
import type { InsightInfo, InsightList, InsightResult } from "@/lib/types";

const quiet = "h-7 rounded-md px-2 text-[12px] font-medium hover:bg-hover cursor-pointer disabled:cursor-default disabled:opacity-50";

// a chart with more names than this shows its first ones (the tile's own order: the largest
// first) and a "Show all" switch; the slanted names of 48 people were cut off and unreadable
export const TOP = 15;
type Drawn = NonNullable<InsightResult["drawn"]>;

/** Pure: a chart cut to its first `n` names (bars, lines and their series alike); a chart
 * with no more names than that, or of a kind with no names along an axis, as it is. */
export function topOf(chart: Drawn, n = TOP): Drawn {
  if (!["bar", "line", "area", "combo", "stacked_bar"].includes(chart.kind) || chart.panels?.length || chart.labels.length <= n) return chart;
  return {
    ...chart,
    labels: chart.labels.slice(0, n),
    series: chart.series.map((s) => ({ ...s, values: s.values.slice(0, n) })),
    // the tile's own line says "Top 15 of 48" with its Show all: the chart does not say it again
  };
}

/** What to type into Ask about a tile: its title as a question the reader can edit. */
export function askAbout(title: string): string {
  return `About the Insights chart "${title}": what changed lately, and why?`;
}

/** Pure: the dashboard's groups in the order they first appear, each with its insights. */
export function grouped(items: InsightInfo[]): [string, InsightInfo[]][] {
  const out = new Map<string, InsightInfo[]>();
  for (const it of items) {
    const key = it.saved ? "Pinned by you" : it.group || "Other";
    out.set(key, [...(out.get(key) ?? []), it]);
  }
  return [...out.entries()];
}

// columns that hold a person's name: it opens that person on Team, by the row's Team key when it
// has one, else by the name (Team decides what you may see)
const PEOPLE = new Set(["person", "who", "busiest_person"]);

/** The rows behind an insight, as read now (cells as text). */
function Rows({ data }: { data: InsightResult }) {
  if (data.rows.length === 0) return <p className="text-[13px] text-muted">No rows you can see right now.</p>;
  return (
    <div className="max-h-80 overflow-auto">
      <table className="w-full text-left text-[12px]">
        <thead className="sticky top-0 bg-bg text-muted">
          <tr>{data.columns.map((c) => <th key={c} className="px-2 py-1 font-medium">{human(c)}</th>)}</tr>
        </thead>
        <tbody>
          {data.rows.map((r, i) => (
            <tr key={i} className="border-t border-border">
              {r.map((c, j) => (
                <td key={j} className="px-2 py-1 tabular-nums">
                  {PEOPLE.has(data.columns[j]) && c ? <Link href={`/team?person=${encodeURIComponent(data.people?.[i] || c)}`} className="hover:underline">{c}</Link> : (c ?? "")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {data.truncated && <p className="pt-1 text-[12px] text-muted">Only the first rows are shown.</p>}
    </div>
  );
}

const SCHEDULES: [InsightInfo["schedule"], string][] = [["", "Off"], ["hourly", "Every hour"], ["daily", "Every morning"], ["weekly", "Monday mornings"]];
const NOTIFY: [InsightInfo["notify"], string][] = [["new_rows", "When a new row appears"], ["rows", "When it has any rows"], ["crosses", "When a number crosses a line"], ["every_run", "Every time"], ["never", "Never"]];
type Watch = NonNullable<InsightInfo["watch"]>;

/** A saved insight's schedule: it runs by itself as you (your own access) and what it finds
 * comes to you on Home; with its last run in plain words. "When a number crosses a line":
 * a column of what it reads, above or below a number; it tells you when a row goes past it. */
export function ScheduleControl({ item, columns = [] }: { item: InsightInfo; columns?: string[] }) {
  const [now, setNow] = useState({ schedule: item.schedule, notify: item.notify, watch: item.watch ?? null });
  const [line, setLine] = useState<{ column: string; op: Watch["op"]; value: string }>({
    column: item.watch?.column ?? "", op: item.watch?.op ?? "above", value: item.watch ? String(item.watch.value) : "" });
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const save = async (next: { schedule: InsightInfo["schedule"]; notify: InsightInfo["notify"]; watch: Watch | null }) => {
    setBusy(true);
    setProblem(null);
    try {
      const got = await setInsightSchedule(item.id, { schedule: next.schedule, notify: next.notify, watch: next.watch });
      setNow({ schedule: got.schedule, notify: got.notify, watch: got.watch ?? null });
    } catch (e) {
      setProblem(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const value = Number(line.value);
  const ready = line.column !== "" && line.value.trim() !== "" && Number.isFinite(value);
  const saveLine = () => ready && save({ schedule: now.schedule, notify: "crosses", watch: { column: line.column, op: line.op, value } });
  const choose = (notify: InsightInfo["notify"]) => {
    if (notify === "crosses") {
      setNow({ ...now, notify });   // saved once the line is complete
      if (ready) saveLine();
    } else {
      save({ schedule: now.schedule, notify, watch: null });
    }
  };
  const field = "h-7 rounded-md border border-border bg-bg px-1 text-[12px]";
  const numeric = columns.length ? columns : now.watch ? [now.watch.column] : [];
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-muted" data-testid={`schedule-${item.id}`}>
      <label className="flex items-center gap-1">
        Check it
        <select className={field} value={now.schedule} disabled={busy} onChange={(e) => save({ schedule: e.target.value as InsightInfo["schedule"], notify: now.notify === "never" && e.target.value ? "new_rows" : now.notify, watch: now.watch })}>
          {SCHEDULES.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
        </select>
      </label>
      {now.schedule && (
        <label className="flex items-center gap-1">
          Tell me on Home
          <select className={field} value={now.notify} disabled={busy} onChange={(e) => choose(e.target.value as InsightInfo["notify"])}>
            {NOTIFY.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
          </select>
        </label>
      )}
      {now.schedule && now.notify === "crosses" && (
        <span className="flex items-center gap-1" data-testid={`watch-${item.id}`}>
          <select className={field} aria-label="Column to watch" value={line.column} disabled={busy} onChange={(e) => setLine({ ...line, column: e.target.value })}>
            <option value="">Column</option>
            {numeric.map((c) => <option key={c} value={c}>{human(c)}</option>)}
          </select>
          <select className={field} aria-label="Above or below" value={line.op} disabled={busy} onChange={(e) => setLine({ ...line, op: e.target.value as Watch["op"] })}>
            <option value="above">above</option>
            <option value="below">below</option>
          </select>
          <input className={`${field} w-20`} aria-label="Line" inputMode="decimal" value={line.value} disabled={busy} onChange={(e) => setLine({ ...line, value: e.target.value })} />
          <button type="button" className={field} disabled={busy || !ready} onClick={saveLine}>Watch</button>
          {now.watch && <span>Watching {human(now.watch.column).toLowerCase()} {now.watch.op} {now.watch.value}</span>}
        </span>
      )}
      {now.schedule && item.last_run && <span>{item.last_run}</span>}
      {problem && <span role="alert" className="text-fg">{problem}</span>}
    </div>
  );
}

/** One insight: read now as you, drawn, with its definition, statement and rows a click away. */
export function InsightTile({ item, onRemoved }: { item: InsightInfo; onRemoved?: () => void }) {
  const [data, setData] = useState<InsightResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [open, setOpen] = useState(false);
  const [round, setRound] = useState(0);
  const [all, setAll] = useState(false);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    getInsight(item.id, ctl.signal, round > 0).then(setData, (e) => !ctl.signal.aborted && setError(e));
    return () => ctl.abort();
  }, [item.id, round]);
  // opened from Home ("/insights#insight-s12"): brought into view once it is drawn
  const here = useRef<HTMLElement>(null);
  useEffect(() => {
    if (data && typeof window !== "undefined" && window.location.hash === `#insight-${item.id}`) here.current?.scrollIntoView?.({ block: "start" });
  }, [data, item.id]);
  const remove = async () => {
    try {
      await deleteInsight(item.id);
      onRemoved?.();
    } catch (e) {
      setError(e);
    }
  };
  return (
    <section id={`insight-${item.id}`} ref={here} className="space-y-2 border-t border-border pt-3" data-testid={`insight-${item.id}`} aria-labelledby={`insight-${item.id}-title`}>
      <div className="flex items-baseline gap-2">
        <h3 id={`insight-${item.id}-title`} className="flex-1 text-[14px] font-semibold">{item.title}</h3>
        <Link href={`/ask?q=${encodeURIComponent(askAbout(item.title))}`} className={`${quiet} inline-flex items-center`}>Ask about this</Link>
        <button type="button" className={quiet} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? "Hide details" : "Details"}
        </button>
        {item.saved && <button type="button" className={quiet} onClick={remove}>Remove</button>}
      </div>
      {item.saved && <ScheduleControl item={item} columns={data?.columns} />}
      {error ? (
        <LoadProblem message={tileError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
      ) : !data ? (
        <Pending label="Reading" />
      ) : (
        <>
          {data.drawn ? <AnswerCharts charts={[all ? data.drawn : topOf(data.drawn)]} caption={false} /> : <Rows data={data} />}
          <p className="flex flex-wrap items-center gap-x-2 text-[12px] text-muted">
            <span>
              {data.drawn && !all && topOf(data.drawn) !== data.drawn ? `Top ${TOP} of ${data.drawn.labels.length}` : `${data.row_count} ${data.row_count === 1 ? "row" : "rows"}`}
              {data.drawn?.as_of ? "" : data.as_of ? `, data as of ${formatDate(data.as_of)}` : `, read ${formatDateTime(data.read_at)}`}
            </span>
            {data.drawn && topOf(data.drawn) !== data.drawn && (
              <button type="button" className={quiet} onClick={() => setAll((a) => !a)}>{all ? `Show top ${TOP}` : `Show all ${data.drawn.labels.length}`}</button>
            )}
            <button type="button" className={quiet} onClick={retry}>Refresh</button>
          </p>
          {open && (
            <div className="space-y-2" data-testid="insight-definition">
              {data.about && <p className="text-[13px] leading-5">{data.about}</p>}
              {data.drawn && <Rows data={data} />}
            </div>
          )}
        </>
      )}
    </section>
  );
}

/** The dashboard: every insight the reader may see, grouped, each read live on its own. */
/** What each monitor checks and when, in a line: "Every morning, tells you when a new row appears". */
export function monitorLine(item: InsightInfo): string {
  const when = SCHEDULES.find(([v]) => v === item.schedule)?.[1] ?? "";
  const w = item.watch;
  const tell = item.notify === "crosses" && w
    ? `tells you when ${human(w.column).toLowerCase()} goes ${w.op} ${w.value}`
    : item.notify === "never" ? "nothing comes to Home" : `tells you ${(NOTIFY.find(([v]) => v === item.notify)?.[1] ?? "").toLowerCase()}`;
  return `${when}, ${tell}`;
}

/** The monitors you run (your charts that run by themselves), listed first, each linking to its chart. */
function Monitors({ items }: { items: InsightInfo[] }) {
  const on = items.filter((i) => i.saved && i.schedule);
  return (
    <section aria-label="Monitors" className="space-y-1.5" data-testid="monitors">
      <h2 className="text-[15px] font-semibold">Monitors</h2>
      {on.length === 0 ? (
        <p className="text-[13px] text-muted">None yet. Pin a chart from an Ask answer, then set it to check by itself and tell you on Home.</p>
      ) : (
        <ul className="divide-y divide-border border-y border-border">
          {on.map((i) => (
            <li key={i.id} className="flex flex-wrap items-baseline justify-between gap-x-4 py-1.5 text-[13px]">
              <a href={`#insight-${i.id}`} className="font-medium hover:underline">{i.title}</a>
              <span className="text-[12px] text-muted">{monitorLine(i)}{i.last_run ? ` · ${i.last_run}` : ""}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function InsightsDashboard({ list, onChanged }: { list: InsightList; onChanged: () => void }) {
  return (
    <div className="space-y-8">
      {list.scope === "self" && (
        <p className="text-[13px] text-muted">The team views show your own row only.</p>
      )}
      {list.saving && <Monitors items={list.items} />}
      {grouped(list.items).map(([group, items]) => (
        <section key={group} className="space-y-4" aria-label={group}>
          <h2 className="text-[15px] font-semibold">{group}</h2>
          <div className="grid gap-6 xl:grid-cols-2">
            {items.map((it) => <InsightTile key={it.id} item={it} onRemoved={onChanged} />)}
          </div>
        </section>
      ))}
      {list.saving && !list.items.some((i) => i.saved) && (
        <p className="text-[13px] text-muted">Ask for a chart and pin it from the answer: it shows here and stays live.</p>
      )}
    </div>
  );
}
