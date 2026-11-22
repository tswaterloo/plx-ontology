"use client";

import Link from "next/link";
import { memo, useCallback, useEffect, useRef, useState } from "react";
import { AnswerCharts } from "@/components/answer-chart";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { deleteInsight, getInsight, setInsightSchedule } from "@/lib/api";
import { ApiError, noticeKind, plainError, tileError } from "@/lib/errors";
import { formatDate, formatDateTime } from "@/lib/format";
import { full, human } from "@/components/charts/scale";
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
        ? <Link ref={go} href={tellMeWhenHref(item, words)} className={`${quiet} inline-flex items-center`}>Set up</Link>
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
export const InsightTile = memo(function InsightTile({ item, onRemoved, entry, streaming }: { item: InsightInfo; onRemoved?: () => void; entry?: TileEntry; streaming?: boolean }) {
  const [data, setData] = useState<InsightResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [open, setOpen] = useState(false);
  const [round, setRound] = useState(0);
  const [all, setAll] = useState(false);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  const here = useRef<HTMLElement>(null);
  const fed = streaming !== undefined && round === 0 && !entry?.later;   // the dashboard's one request supplies it
  useEffect(() => {
    if (!fed) return;
    if (entry?.result) { setData(entry.result); setError(null); }
    else if (entry?.error) setError(new ApiError(entry.error.status, entry.error.code, undefined, entry.error.message));
  }, [fed, entry]);
  // its own read starts once it nears the view (or is the one opened from Home): a dashboard
  // of 61 tiles asked for every one at once, and the server read them all cold together
  const [near, setNear] = useState(() => typeof IntersectionObserver === "undefined" || isTarget(item.id));
  useEffect(() => {
    if (fed || near || !here.current) return;
    const seen = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        setNear(true);
        seen.disconnect();
      }
    }, { rootMargin: NEAR_MARGIN });
    seen.observe(here.current);
    return () => seen.disconnect();
  }, [near, fed]);
  useEffect(() => {
    if (fed || !near) return;
    const ctl = new AbortController();
    setError(null);
    getInsight(item.id, ctl.signal, round > 0).then(setData, (e) => !ctl.signal.aborted && setError(e));
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
      ) : !near && !fed && !data ? (
        <div className="h-40" data-testid={`unread-${item.id}`} />
      ) : !data ? (
        <Pending label="Reading" />
      ) : (
        <>
          {data.drawn ? <AnswerCharts charts={[all ? data.drawn : topOf(data.drawn)]} caption={false} /> : <Rows data={data} />}
          <p className="flex flex-wrap items-center gap-x-2 text-[12px] text-muted">
            {item.fixed_dates && <span data-testid={`fixed-${item.id}`}>Dates fixed when pinned: {item.fixed_dates}</span>}
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
                <Link href={`/ask?q=${encodeURIComponent(m.ask)}`} className="text-[12px] text-accent hover:underline">
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

export function InsightsDashboard({ list, onChanged, feed, streaming }: { list: InsightList; onChanged: () => void; feed?: Record<string, TileEntry>; streaming?: boolean }) {
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
            {items.map((it) => <InsightTile key={it.id} item={it} onRemoved={onChanged} entry={feed?.[it.id]} streaming={feed ? !!streaming : undefined} />)}
          </div>
        </section>
      ))}
      {list.saving && !list.items.some((i) => i.saved) && (
        <p className="text-[13px] text-muted">Ask for a chart and pin it from the answer: it shows here and stays live.</p>
      )}
      <Measures measures={list.measures ?? []} />
    </div>
  );
}
