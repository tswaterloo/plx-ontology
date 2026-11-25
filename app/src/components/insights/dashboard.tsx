"use client";

import Link from "next/link";
import { memo, useCallback, useEffect, useRef, useState } from "react";
import { AnswerCharts } from "@/components/answer-chart";
import { ExcelExportButton } from "@/components/excel-export-button";
import { DefinitionLines } from "@/components/definition-lines";
import { QueryToggle, ShowWork } from "@/components/query-work";
import { LoadProblem } from "@/components/ui/notice";
import { InsightsAsk } from "@/components/insights/ask-here";
import { WhyDidItChange } from "@/components/insights/why";
import { ChartSkeleton } from "@/components/charts/chart-skeleton";
import { Pending } from "@/components/ui/skeleton";
import { deleteInsight, getInsight, proposeMeasureCandidate, readInsightFiltered, setInsightSchedule } from "@/lib/api";
import { ApiError, noticeKind, plainError, tileError } from "@/lib/errors";
import { ago, formatDate, isBeforeToday } from "@/lib/format";
import { full, human } from "@/components/charts/scale";
import type { InsightInfo, InsightList, InsightResult } from "@/lib/types";

// a button that looks like one: hairline, a warm hover, a pressed state, 13 px (legible, 32 px tall)
const quiet = "h-8 rounded-md border border-border px-2.5 text-[13px] font-medium text-fg transition-colors duration-100 hover:border-control hover:bg-hover active:bg-surface-2 cursor-pointer disabled:cursor-default disabled:opacity-50 disabled:hover:bg-transparent";
const primary = "h-9 rounded-md bg-accent px-4 text-[14px] font-semibold text-accent-fg transition-opacity duration-100 hover:opacity-90 active:opacity-80 cursor-pointer";

// a chart with more names than this shows its first ones (the tile's own order: the largest
// first) and a "Show all" switch that opens the rest in the same tile; nothing is cut
export const TOP = 8;
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

function groupKey(i: InsightInfo): string {
  return i.saved ? "Pinned by you" : i.group || "Other";
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

/** Pure: the ids in the order shown after `id` moves one place up (-1) or down (1) among the
 * charts of its own group; the same order when it cannot move. */
export function moved(items: InsightInfo[], id: string, dir: -1 | 1): string[] {
  const ids = items.map((i) => i.id);
  const me = items.find((i) => i.id === id);
  if (!me) return ids;
  const key = (i: InsightInfo) => (i.saved ? "Pinned by you" : i.group || "Other");
  const mates = items.filter((i) => key(i) === key(me)).map((i) => i.id);
  const swap = mates[mates.indexOf(id) + dir];
  if (swap === undefined) return ids;
  const a = ids.indexOf(id), b = ids.indexOf(swap);
  [ids[a], ids[b]] = [ids[b], ids[a]];
  return ids;
}

/** Pure: the ids in the order shown after `from` is dropped on `to`, both of one group: it
 * takes the place of `to`, the others making room; the same order for another group or an unknown id. */
export function placed(items: InsightInfo[], from: string, to: string): string[] {
  const ids = items.map((i) => i.id);
  const a = items.find((i) => i.id === from), b = items.find((i) => i.id === to);
  if (!a || !b || from === to || groupKey(a) !== groupKey(b)) return ids;
  const rest = ids.filter((x) => x !== from);
  rest.splice(ids.indexOf(to), 0, from);
  return rest;
}

// columns that hold a person's name: it opens that person on Team, by the row's Team key when it
// has one, else by the name (Team decides what you may see)
const PEOPLE = new Set(["person", "who", "busiest_person"]);

/** Pure: one cell as people read it. A decimal number (what the database sends as text) gets
 * thousands separators and at most two places (four under 1). It is money only when the data
 * says so: a paired "<column>_currency" column (the business views' own convention) holding a
 * currency code; any other decimal (a percentage, a day count, a quantity) is a plain number.
 * Whole numbers are left alone: they may be order or invoice numbers. */
export function cellText(cell: string | null | undefined, column: string, row: readonly (string | null)[], columns: readonly string[]): string {
  if (cell === null || cell === undefined) return "";
  if (!/^-?\d+\.\d+$/.test(cell)) return cell;
  const n = Number(cell);
  if (!Number.isFinite(n)) return cell;
  const code = row[columns.indexOf(`${column}_currency`)];
  return full(n, /^[A-Z]{3}$/.test(code ?? "") ? code : null);
}

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
                <td key={j} className={`px-2 py-1 tabular-nums${/^-?\d+\.\d+$/.test(c ?? "") ? " text-right" : ""}`}>
                  {PEOPLE.has(data.columns[j]) && c ? <Link href={`/team?person=${encodeURIComponent(data.people?.[i] || c)}`} className="hover:underline">{c}</Link> : cellText(c, data.columns[j], r, data.columns)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {data.truncated && <p className="pt-1 text-[12px] text-muted">{data.total_rows ? `First ${data.row_count} of ${data.total_rows} rows.` : `Only the first ${data.row_count} rows are shown.`}</p>}
      <div className="pt-1">
        <ExcelExportButton
          title={data.title}
          req={() => ({
            title: data.title,
            tables: [{
              title: data.title,
              columns: data.columns,
              rows: data.rows,
              sql: data.sql,
              as_of: data.as_of,
              total_rows: data.total_rows ?? null,
              truncated: Boolean(data.truncated || (data.total_rows != null && data.rows.length < data.total_rows)),
            }],
          })}
        />
      </div>
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
  // a chart along an axis is watched on its latest row (per currency), so an earlier month never comes back
  const axis = item.chart?.x || null;
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
  const saveLine = () => ready && save({ schedule: now.schedule, notify: "crosses", watch: { column: line.column, op: line.op, value, ...(axis ? { row: "latest" as const, order: axis } : { row: "any" as const }) } });
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
          {now.watch && <span>Watching {watchWords(now.watch)}</span>}
        </span>
      )}
      {now.schedule && item.last_run && <span>{item.last_run}</span>}
      <TellMeWhen item={item} />
      {problem && <span role="alert" className="text-fg">{problem}</span>}
    </div>
  );
}

/** What a watched line reads: "the latest revenue in USD" (latest: along the chart's axis; total: the column's sum). */
function watchSubject(w: Watch): string {
  const col = human(w.column).toLowerCase();
  const of = w.where ? ` in ${w.where.value}` : "";
  return w.row === "latest" ? `the latest ${col}${of}` : w.row === "total" ? `the total ${col}${of}` : `${col}${of}`;
}

/** A watched line in words: "the latest revenue in USD below 400000". */
export function watchWords(w: Watch): string {
  return `${watchSubject(w)} ${w.op} ${w.value}`;
}

/** The Ask's question for "Tell me when ..." on a pinned chart: the person's words with the chart they are about. */
export function tellMeWhenHref(item: InsightInfo, words: string): string {
  const q = `Tell me when ${words.trim()} (my pinned chart "${item.title}")`;
  return `/ask?q=${encodeURIComponent(q)}&page=${encodeURIComponent(`/insights#insight-${item.id}`)}`;
}

/** "Tell me when ...": the person's words go to the Ask with this chart, which reads the
 * line back as a sentence and sets the monitor only once they say yes. */
function TellMeWhen({ item }: { item: InsightInfo }) {
  const [words, setWords] = useState("");
  const go = useRef<HTMLAnchorElement>(null);
  const ready = words.trim().length > 2;
  return (
    <span className="flex items-center gap-1" data-testid={`tell-${item.id}`}>
      <label className="flex items-center gap-1">
        Tell me when
        <input className="h-7 w-56 rounded-md border border-border bg-bg px-1 text-[12px]" value={words} maxLength={300}
          placeholder="USD revenue drops under 400k" onChange={(e) => setWords(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && ready) go.current?.click(); }} />
      </label>
      {ready
        ? <Link ref={go} prefetch={false} href={tellMeWhenHref(item, words)} className={`${quiet} inline-flex items-center`}>Set up</Link>
        : <span className={`${quiet} inline-flex items-center opacity-50`} aria-disabled="true">Set up</span>}
    </span>
  );
}

// how far below the view a tile starts its own read (about a screen: it is drawn by the time it is scrolled to)
export const NEAR_MARGIN = "900px 0px";

/** The tile the page was opened at (Home links to "/insights#insight-<id>"). */
function isTarget(id: string): boolean {
  return typeof window !== "undefined" && window.location.hash === `#insight-${id}`;
}

/** What the one-response dashboard has for a tile so far (none yet: still being read;
 * `later`: nothing kept for it, so the tile reads it itself once it nears the view). */
export type TileEntry = { result?: InsightResult; error?: { code: string; message?: string; status: number }; later?: boolean };

/** One insight: read as you, drawn, with its definition, statement and rows a click away.
 * `entry`: given by a dashboard that reads every chart in one request (the tile then asks for
 * nothing itself, until Refresh, unless the stream left it for `later`); `streaming`: that
 * dashboard is still reading. A tile that reads itself does so once it nears the view. */
/** What a person can do to a tile while arranging their dashboard. */
export type Arrange = { up?: () => void; down?: () => void; off: () => void; start: () => void; drop: () => void; dragging: boolean };

export const InsightTile = memo(function InsightTile({ item, onRemoved, entry, streaming, arrange }: { item: InsightInfo; onRemoved?: () => void; entry?: TileEntry; streaming?: boolean; arrange?: Arrange }) {
  const [data, setData] = useState<InsightResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [open, setOpen] = useState(false);
  const [round, setRound] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const [all, setAll] = useState(false);
  const [proposeState, setProposeState] = useState<"idle" | "saving" | "sent" | "failed">("idle");
  const [proposeProblem, setProposeProblem] = useState("");
  const retry = useCallback(() => setRound((n) => n + 1), []);
  // the filters shown are the tile's own statement's, kept while a changed read is shown
  const [filtered, setFiltered] = useState(false);
  const [work, setWork] = useState<InsightResult["work"]>(null);
  useEffect(() => {
    if (data?.work && !filtered) setWork(data.work);
  }, [data, filtered]);
  const here = useRef<HTMLElement>(null);
  const fed = streaming !== undefined && round === 0 && !entry?.later;   // the dashboard's one request supplies it
  useEffect(() => {
    if (!fed) return;
    if (entry?.result) { setData(entry.result); setError(null); }
    else if (entry?.error) setError(new ApiError(entry.error.status, entry.error.code, undefined, entry.error.message));
  }, [fed, entry]);
  // its own read starts once it nears the view (or is the one opened from Home): a dashboard
  // of 61 tiles asked for every one at once, and the server read them all cold together.
  // It is drawn only while it is near the view too: 40 charts drawn at once were 7,172 nodes.
  const noObserver = typeof IntersectionObserver === "undefined";
  const [near, setNear] = useState(() => noObserver || isTarget(item.id));
  const [inView, setInView] = useState(() => noObserver || isTarget(item.id));
  const body = useRef<HTMLDivElement>(null);
  const drawnHeight = useRef(160);   // what it measured when it left, so the page does not jump
  useEffect(() => {
    if (!here.current || typeof IntersectionObserver === "undefined") return;
    const seen = new IntersectionObserver((entries) => {
      const on = entries.some((e) => e.isIntersecting);
      if (on) setNear(true);
      else if (body.current?.offsetHeight) drawnHeight.current = body.current.offsetHeight;
      setInView(on);
    }, { rootMargin: NEAR_MARGIN });
    seen.observe(here.current);
    return () => seen.disconnect();
  }, []);
  useEffect(() => {
    if (fed || !near) return;
    const ctl = new AbortController();
    setError(null);
    setRefreshing(round > 0);
    getInsight(item.id, ctl.signal, round > 0, setData).then((d) => { setData(d); setRefreshing(false); }, (e) => { if (!ctl.signal.aborted) { setError(e); setRefreshing(false); } });
    return () => ctl.abort();
  }, [item.id, round, near, fed]);
  // opened from Home ("/insights#insight-s12"): brought into view once it is drawn
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
  const propose = async () => {
    if (!data?.sql) return;
    setProposeState("saving");
    setProposeProblem("");
    try {
      await proposeMeasureCandidate({
        insight_id: item.id,
        title: item.title,
        question: item.question || undefined,
        sql: data.sql,
        source_kind: "saved_tile",
      });
      setProposeState("sent");
    } catch (e) {
      setProposeProblem(plainError(e));
      setProposeState("failed");
    }
  };
  return (
    <section id={`insight-${item.id}`} ref={here}
      className={`cv-auto cv-tile flex min-w-0 scroll-mt-24 flex-col gap-3.5 rounded-lg border bg-surface/50 p-5 transition-colors duration-100 ${arrange ? "cursor-grab border-dashed border-control hover:border-accent" : "border-border"} ${arrange?.dragging ? "opacity-50" : ""}`}
      draggable={!!arrange}
      onDragStart={arrange ? (e) => { e.dataTransfer?.setData("text/plain", item.id); arrange.start(); } : undefined}
      onDragOver={arrange ? (e) => e.preventDefault() : undefined}
      onDrop={arrange ? (e) => { e.preventDefault(); arrange.drop(); } : undefined}
      data-testid={`insight-${item.id}`} aria-labelledby={`insight-${item.id}-title`}>
      <div className="space-y-1">
        <div className="flex items-start gap-2">
          <h3 id={`insight-${item.id}-title`} className="flex-1 text-[16px] font-semibold leading-6 text-fg">{item.title}</h3>
          {item.saved && <button type="button" className={quiet} onClick={remove}>Remove</button>}
        </div>
        {data?.about && !data.not_measurable && (
          data.about.length > 110 ? (
            <details className="group" data-testid={`takeaway-${item.id}`}>
              <summary className="flex cursor-pointer list-none items-center gap-2 text-[13px] leading-5 text-muted hover:text-fg [&::-webkit-details-marker]:hidden">
                <span className="min-w-0 truncate">{data.about}</span>
                <span className="shrink-0 text-[12px] font-medium text-accent group-open:hidden">How this is calculated</span>
                <span className="hidden shrink-0 text-[12px] font-medium text-muted group-open:inline">Hide</span>
              </summary>
              <p className="mt-2 rounded-md border border-border/70 bg-bg/70 p-3 text-[13px] leading-relaxed text-muted">{data.about}</p>
            </details>
          ) : (
            <p className="text-[13px] leading-5 text-muted" data-testid={`takeaway-${item.id}`}>{data.about}</p>
          )
        )}
      </div>
      {arrange && (
        <div className="flex flex-wrap items-center gap-1.5" data-testid={`arrange-${item.id}`}>
          <span className="text-[13px] text-muted">Drag to move, or</span>
          <button type="button" className={quiet} disabled={!arrange.up} onClick={arrange.up} aria-label={`Move ${item.title} up`}>Up</button>
          <button type="button" className={quiet} disabled={!arrange.down} onClick={arrange.down} aria-label={`Move ${item.title} down`}>Down</button>
          <button type="button" className={quiet} onClick={arrange.off} aria-label={`Take ${item.title} off your dashboard`}>Remove from dashboard</button>
        </div>
      )}
      {item.saved && <ScheduleControl item={item} columns={data?.columns} />}
      {error ? (
        <LoadProblem message={tileError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
      ) : !near && !fed && !data ? (
        <div className="h-60" data-testid={`unread-${item.id}`} />
      ) : !data ? (
        <div className="min-h-60" aria-busy><ChartSkeleton /><Pending label="Reading" bars={0} /></div>
      ) : !inView ? (
        <div style={{ height: drawnHeight.current }} data-testid={`undrawn-${item.id}`} aria-hidden />
      ) : (
        <div ref={body} className="space-y-3">
          {data.not_measurable && <p className="text-[13px] text-muted" data-testid={`unmeasurable-${item.id}`}>{data.not_measurable}</p>}
          {data.not_measurable ? null : data.drawn ? (
            <div className={all ? "max-h-[28rem] overflow-y-auto" : ""} data-testid={`chart-body-${item.id}`}>
              <AnswerCharts charts={[all ? data.drawn : topOf(data.drawn)]} caption={false} />
            </div>
          ) : <Rows data={data} />}
          {(data.excluded ?? []).map((x) => (
            <p key={x.parameter} className="text-[13px] text-muted" data-testid={`excluded-${item.id}-${x.parameter}`}>
              {x.n === null || x.n === undefined ? `Not counted: what is ${x.says} (${x.why ?? "not read"})` : `${x.n.toLocaleString()} more ${x.says}`}
            </p>
          ))}
          {/* a "not checked" line says nothing a reader can use (no second figure exists): only a reconciliation or a difference is shown */}
          {["reconciles", "differs"].includes((data as InsightResult & { checked?: { status: string } | null }).checked?.status ?? "") && (
            <p className={`text-[13px] ${(data as InsightResult & { checked?: { status: string } | null }).checked?.status === "differs" ? "text-red-700" : "text-muted"}`} data-testid={`checked-${item.id}`}>
              Checked: {(data as InsightResult & { checked?: { says: string } | null }).checked?.says}
            </p>
          )}
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-muted">
            {item.fixed_dates && <span data-testid={`fixed-${item.id}`}>Dates fixed when pinned: {item.fixed_dates}</span>}
            {filtered && <span data-testid={`filtered-${item.id}`}>Your filters (not saved)</span>}
            <span>
              {data.drawn && !all && topOf(data.drawn) !== data.drawn ? `Top ${TOP} of ${data.drawn.labels.length}` : data.truncated ? (data.total_rows ? `First ${data.row_count} of ${data.total_rows} rows` : `First ${data.row_count} rows (more not counted)`) : `${data.row_count} ${data.row_count === 1 ? "row" : "rows"}`}
              {isBeforeToday(data.drawn?.as_of ?? data.as_of) ? `, data as of ${formatDate(data.drawn?.as_of ?? data.as_of)}` : ""}
              {data.read_at && (data.truncated || isBeforeToday(data.drawn?.as_of ?? data.as_of)) ? `, value read ${ago(data.read_at)}` : ""}
              {refreshing ? ", updating" : round > 0 && error ? ", refresh failed" : ""}
            </span>
            {data.drawn && topOf(data.drawn) !== data.drawn && (
              <button type="button" className={quiet} onClick={() => setAll((a) => !a)}>{all ? `Show top ${TOP}` : `Show all ${data.drawn.labels.length}`}</button>
            )}
            <button type="button" className={quiet} onClick={() => { setFiltered(false); retry(); }}>Refresh</button>
          </p>
          <div className="flex flex-wrap items-center gap-1.5 border-t border-border pt-3">
            <Link prefetch={false} href={`/ask?q=${encodeURIComponent(askAbout(item.title))}`} className={`${quiet} inline-flex items-center`}>Ask about this</Link>
            <button type="button" className={quiet} aria-expanded={open} onClick={() => setOpen((o) => !o)}>{open ? "Hide details" : "Details"}</button>
            {item.saved && data.sql && (
              proposeState === "sent" ? (
                <span className="px-2 text-[12px] text-muted" data-testid={`proposed-${item.id}`}>Proposed for review</span>
              ) : (
                <button type="button" className={quiet} disabled={proposeState === "saving"} onClick={propose} data-testid={`propose-${item.id}`}>
                  {proposeState === "saving" ? "Proposing" : "Propose as shared definition"}
                </button>
              )
            )}
            {proposeState === "failed" && proposeProblem && <span className="text-[12px] text-muted" role="alert">{proposeProblem}</span>}
          </div>
          {item.measure && <DefinitionLines definitions={[item.measure]} testId={`measure-definition-${item.id}`} />}
          {item.measure && item.id.startsWith("measure_") && data.columns.includes("month") && <WhyDidItChange id={item.id} testId={`why-${item.id}`} />}
          {open && (
            <div className="space-y-2" data-testid="insight-definition">
              {work && (
                <ShowWork
                  key={round}
                  work={work}
                  testId={`insight-work-${item.id}`}
                  read={async (edits) => {
                    setData(await readInsightFiltered(item.id, edits));
                    setFiltered(true);
                  }}
                  onReset={() => {
                    setFiltered(false);
                    retry();
                  }}
                />
              )}
              {data.drawn && <Rows data={data} />}
              <QueryToggle sql={data.sql} testId={`insight-query-${item.id}`} />
            </div>
          )}
        </div>
      )}
    </section>
  );
});

/** The dashboard: every insight the reader may see, grouped, each read live on its own. */
/** What each monitor checks and when, in a line: "Every morning, tells you when a new row appears". */
export function monitorLine(item: InsightInfo): string {
  const when = SCHEDULES.find(([v]) => v === item.schedule)?.[1] ?? "";
  const w = item.watch;
  const tell = item.notify === "crosses" && w
    ? `tells you when ${watchSubject(w)} goes ${w.op} ${w.value}`
    : item.notify === "never" ? "nothing comes to Home" : `tells you ${(NOTIFY.find(([v]) => v === item.notify)?.[1] ?? "").toLowerCase()}`;
  return `${when}, ${tell}`;
}

/** The monitors you run (your charts that run by themselves), listed first, each linking to its chart. */
function Monitors({ items }: { items: InsightInfo[] }) {
  const on = items.filter((i) => i.saved && i.schedule);
  // nothing running by itself: no empty section (the page's closing line says how to start one)
  if (on.length === 0) return null;
  return (
    <section aria-label="Monitors" className="space-y-1.5" data-testid="monitors">
      <h2 className="text-[15px] font-semibold">Monitors</h2>
      <ul className="divide-y divide-border border-y border-border">
        {on.map((i) => (
          <li key={i.id} className="flex flex-wrap items-baseline justify-between gap-x-4 py-1.5 text-[13px]">
            <a href={`#insight-${i.id}`} className="font-medium hover:underline">{i.title}</a>
            <span className="text-[12px] text-muted">{monitorLine(i)}{i.last_run ? ` · ${i.last_run}` : ""}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

type Measure = NonNullable<InsightList["measures"]>[number];

/** The agreed measures Ask and the charts use: each one's definition, who owns it, what it
 * reads and splits by, and a question to try it with. */
export function Measures({ measures }: { measures: Measure[] }) {
  const [open, setOpen] = useState(false);
  if (measures.length === 0) return null;
  return (
    <section className="space-y-3" aria-label="Measures">
      <div className="flex items-baseline gap-2">
        <h2 className="flex-1 text-[15px] font-semibold">Measures</h2>
        <button type="button" className={quiet} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? "Hide" : `Show all ${measures.length}`}
        </button>
      </div>
      <p className="text-[13px] text-muted">The agreed definitions behind every figure Ask and these charts give.</p>
      {open && (
        <ul className="divide-y divide-border text-[13px]">
          {measures.map((m) => (
            <li key={m.id} className="space-y-1 py-2.5">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="font-medium">{m.name}</span>
                <span className="text-[12px] text-muted">{m.owner} · {m.unit} · from {m.source}</span>
              </div>
              <p className="text-muted">{m.definition}</p>
              <p className="text-[12px] text-muted">
                Split by {m.dimensions.join(", ")}. Reads {m.period}.
              </p>
              {m.ask && (
                <Link prefetch={false} href={`/ask?q=${encodeURIComponent(m.ask)}`} className="text-[12px] text-accent hover:underline">
                  {m.ask}
                </Link>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Customize: the one visible button at the top. Arranging lets you drag a chart (or move it
 * up or down) within its group, remove it, add one from the catalogue (the charts you took
 * off, with a search) or go back to the standard dashboard. Kept per person on the server. */
function Customise({
  list,
  editing,
  onToggle,
  onArrange,
  inHeader,
}: {
  list: InsightList;
  editing: boolean;
  onToggle: () => void;
  onArrange: (order: string[], hidden: string[]) => void;
  inHeader?: boolean;
}) {
  const hidden = list.hidden ?? [];
  const [find, setFind] = useState("");
  const q = find.trim().toLowerCase();
  const shownHidden = q ? hidden.filter((h) => `${h.title} ${h.group}`.toLowerCase().includes(q)) : hidden;
  if (inHeader && !editing && !list.customised && !list.layout_problem) return null;
  return (
    <section className="space-y-3" aria-label="Customize your dashboard" data-testid="customise">
      <div className="flex flex-wrap items-center gap-3">
        {!inHeader && (
          <button type="button" className={quiet} aria-pressed={editing} onClick={onToggle}>{editing ? "Done" : "Customize"}</button>
        )}
        {editing && <button type="button" className={quiet} disabled={!list.customised} onClick={() => onArrange([], [])}>Reset to the standard dashboard</button>}
        {!editing && list.customised && <><span className="text-[13px] text-muted">Your own arrangement</span><button type="button" className={quiet} onClick={() => onArrange([], [])}>Back to the standard dashboard</button></>}
        {editing && <span className="text-[13px] text-muted">Drag a chart, or use its Up and Down. Changes are saved for you.</span>}
      </div>
      {list.layout_problem && <p className="text-[14px] text-muted" role="status" data-testid="layout-problem">{list.layout_problem}</p>}
      {editing && (
        <div className="space-y-2 rounded-md border border-border bg-surface p-4" data-testid="hidden-charts">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="flex-1 text-[15px] font-semibold">Add a chart ({hidden.length})</h2>
            {hidden.length > 0 && (
              <input type="search" value={find} onChange={(e) => setFind(e.target.value)} placeholder="Search the charts you took off" aria-label="Search the charts you took off"
                className="h-9 w-72 max-w-full rounded-md border border-control bg-bg px-3 text-[14px]" />
            )}
          </div>
          {hidden.length === 0 ? <p className="text-[14px] text-muted">None. Remove a chart from your dashboard and it waits here to be added back.</p> : shownHidden.length === 0 ? <p className="text-[14px] text-muted">No chart matches that.</p> : (
            <ul className="divide-y divide-border border-y border-border">
              {shownHidden.map((h) => (
                <li key={h.id} className="flex items-center gap-3 py-2 text-[14px]">
                  <span className="flex-1">{h.title}<span className="pl-2 text-[13px] text-muted">{h.group}</span></span>
                  <button type="button" className={quiet} aria-label={`Add ${h.title} back`} onClick={() => onArrange([...list.items.map((i) => i.id), h.id], hidden.filter((x) => x.id !== h.id).map((x) => x.id))}>Add</button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

export function InsightsDashboard({
  list,
  onChanged,
  feed,
  streaming,
  onArrange,
  editing: controlledEditing,
  onEditingChange,
}: {
  list: InsightList;
  onChanged: () => void;
  feed?: Record<string, TileEntry>;
  streaming?: boolean;
  onArrange?: (order: string[], hidden: string[]) => void;
  editing?: boolean;
  onEditingChange?: (next: boolean) => void;
}) {
  const [ownEditing, setOwnEditing] = useState(false);
  const editing = controlledEditing ?? ownEditing;
  const toggleEditing = () => {
    if (onEditingChange) onEditingChange(!editing);
    else setOwnEditing((e) => !e);
  };
  const drag = useRef<string | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const shown = list.items.map((i) => i.id);
  const away = (list.hidden ?? []).map((h) => h.id);
  const arrangeOf = (it: InsightInfo): Arrange | undefined => {
    if (!editing || !onArrange) return undefined;
    const up = moved(list.items, it.id, -1), down = moved(list.items, it.id, 1);
    return {
      up: up.some((x, i) => x !== shown[i]) ? () => onArrange(up, away) : undefined,
      down: down.some((x, i) => x !== shown[i]) ? () => onArrange(down, away) : undefined,
      off: () => onArrange(shown.filter((x) => x !== it.id), [...away, it.id]),
      start: () => { drag.current = it.id; setDragging(it.id); },
      drop: () => {
        const from = drag.current;
        drag.current = null;
        setDragging(null);
        if (!from) return;
        const next = placed(list.items, from, it.id);
        if (next.some((x, i) => x !== shown[i])) onArrange(next, away);
      },
      dragging: dragging === it.id,
    };
  };
  const allGroups = grouped(list.items);
  const measureGroups = allGroups.filter(([g]) => /^Measures:\s*/i.test(g));
  const viewGroups = allGroups.filter(([g]) => !/^Measures:\s*/i.test(g));
  const groupChip = (group: string, items: InsightInfo[], label: string) => (
    <a
      key={group}
      href={`#insight-group-${group.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`}
      className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-1 text-[12px] font-medium text-muted transition-colors hover:border-border-strong hover:bg-hover hover:text-fg"
    >
      <span>{label}</span>
      <span className="num text-[11px] text-faint">{items.length}</span>
    </a>
  );
  return (
    <div className="mx-auto w-full max-w-[1800px] space-y-6 text-[14px] leading-6">
      {list.saving && onArrange && (
        <Customise list={list} editing={editing} onToggle={toggleEditing} onArrange={onArrange} inHeader={onEditingChange !== undefined} />
      )}
      {list.saving && <InsightsAsk onPinned={onChanged} />}
      {list.scope === "self" && (
        <p className="text-[13px] text-muted">The team views show your own row only.</p>
      )}
      {list.saving && <Monitors items={list.items} />}
      {/* many charts: sticky filter pill bar split into Views and Measures when both are present */}
      {allGroups.length > 2 && (
        <nav aria-label="Groups" className="sticky top-0 z-10 space-y-2 rounded-lg border border-border/80 bg-bg/95 px-3.5 py-2.5 backdrop-blur-sm" data-testid="insight-groups">
          {measureGroups.length > 0 && viewGroups.length > 0 ? (
            <>
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="mr-1 text-[11px] font-semibold uppercase tracking-[0.04em] text-faint">Views</span>
                {viewGroups.map(([group, items]) => groupChip(group, items, group))}
              </div>
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="mr-1 text-[11px] font-semibold uppercase tracking-[0.04em] text-faint">Measures</span>
                {measureGroups.map(([group, items]) => groupChip(group, items, group.replace(/^Measures:\s*/i, "")))}
              </div>
            </>
          ) : (
            <div className="flex flex-wrap items-center gap-1.5">
              {allGroups.map(([group, items]) => groupChip(group, items, group))}
            </div>
          )}
        </nav>
      )}
      {allGroups.map(([group, items]) => (
        <section key={group} id={`insight-group-${group.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`} className="scroll-mt-16 space-y-3" aria-label={group}>
          <h2 className="flex items-baseline gap-2 border-b border-border pb-2 text-[17px] font-semibold">{group}<span className="num text-[13px] font-normal text-muted">{items.length}</span></h2>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 2xl:grid-cols-3">
            {items.map((it) => <InsightTile key={it.id} item={it} onRemoved={onChanged} entry={feed?.[it.id]} streaming={feed ? !!streaming : undefined} arrange={arrangeOf(it)} />)}
          </div>
        </section>
      ))}
      {list.saving && !list.items.some((i) => i.saved) && (
        <p className="text-[13px] text-muted">Ask for a chart and pin it from the answer: it shows here and stays live. Set it to check by itself and it is listed under Monitors.</p>
      )}
      <Measures measures={list.measures ?? []} />
    </div>
  );
}
