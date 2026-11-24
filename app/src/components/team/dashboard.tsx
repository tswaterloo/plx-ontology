"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Sheet } from "@/components/ui/sheet";
import { getInsight, getTeamPerson } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { readAhead as keepAhead, useRead } from "@/lib/use-read";
import { eastern } from "@/lib/format";
import type { InsightResult, TeamItem, TeamPerson, TeamPersonDetail, TeamTrackerWeek, TeamView } from "@/lib/types";

// The Team view: one dense row per person, every captured system over 7 or 28 days, open
// work, reply time, a 28-day trend and busiest hours. Sort by any column, filter by team,
// role and system, open a person for their days, open items and the people they work with.
// The server decides who is listed (everyone for the owner and the named Team readers, else
// only you); nothing here widens it.

type Channel = "mail_sent" | "mail_received" | "teams" | "meetings" | "mc_approvals" | "mc_questions" | "mc_tasks" | "planner" | "sharepoint" | "bc" | "portal" | "sage";

const CHANNELS: { key: Channel; short: string; head: string; title: string; system: string }[] = [
  { key: "mail_sent", short: "Mail sent", head: "Sent", title: "Mail sent", system: "mail" },
  { key: "mail_received", short: "Mail received", head: "Recv", title: "Mail received from people", system: "mail" },
  { key: "teams", short: "Teams messages", head: "Msgs", title: "Teams messages sent", system: "teams" },
  { key: "meetings", short: "Meetings", head: "Mtgs", title: "Meetings organised or attended", system: "meetings" },
  { key: "mc_approvals", short: "MC approvals", head: "Appr", title: "Mission Control approvals requested or decided", system: "mc" },
  { key: "mc_questions", short: "MC questions", head: "Q&A", title: "Mission Control questions asked or answered", system: "mc" },
  { key: "mc_tasks", short: "MC steps", head: "Steps", title: "Other Mission Control task steps", system: "mc" },
  { key: "planner", short: "Planner tasks", head: "Plan", title: "Planner tasks created, assigned or completed", system: "planner" },
  { key: "sharepoint", short: "SharePoint files", head: "Files", title: "SharePoint files created or changed", system: "sharepoint" },
  { key: "bc", short: "BC records", head: "BC", title: "Business Central records created or changed", system: "bc" },
  { key: "portal", short: "Portal records", head: "Portal", title: "Portal records created or changed", system: "portal" },
  { key: "sage", short: "Sage entries", head: "Sage", title: "Sage entries", system: "sage" },
];

const GROUPS: Record<string, { label: string; tone: string }> = {
  mail: { label: "Mail", tone: "var(--sys-mail)" },
  teams: { label: "Teams", tone: "var(--sys-teams)" },
  meetings: { label: "Calendar", tone: "var(--place-meetings)" },
  mc: { label: "Mission Control", tone: "var(--sys-mc)" },
  planner: { label: "Planner", tone: "var(--place-team)" },
  sharepoint: { label: "SharePoint", tone: "var(--sys-file)" },
  bc: { label: "BC", tone: "var(--sys-bc)" },
  portal: { label: "Portal", tone: "var(--sys-portal)" },
  sage: { label: "Sage", tone: "var(--sys-sage)" },
};

type Period = 7 | 28;
type SortKey = "name" | "team" | "total" | "week" | "saved" | Channel | "awaiting_reply" | "tasks" | "alerts" | "reply";

function thisWeek(p: TeamPerson): TeamTrackerWeek | undefined {
  const weeks = p.weeks ?? [];
  return weeks[weeks.length - 1];
}

function lastWeek(p: TeamPerson): TeamTrackerWeek | undefined {
  const weeks = p.weeks ?? [];
  return weeks[weeks.length - 2];
}

function weekOf(iso: string): string {
  return new Date(`${iso}T12:00:00`).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function counts(p: TeamPerson, period: Period): Record<string, number> {
  return (period === 7 ? p.d7 : p.d28) ?? {};
}

function total(p: TeamPerson, period: Period): number {
  const c = counts(p, period);
  return Object.entries(c).reduce((s, [k, v]) => (k === "mail_received" ? s : s + v), 0);
}

function replyOf(p: TeamPerson, period: Period): number | null {
  return (period === 7 ? p.reply_minutes_7 : p.reply_minutes_28) ?? null;
}

/** 42m, 3.5h, 2.1d. */
export function minutes(n: number | null | undefined): string {
  if (n == null) return "";
  if (n < 60) return `${Math.max(1, Math.round(n))}m`;
  if (n < 60 * 24) return `${(n / 60).toFixed(n < 600 ? 1 : 0)}h`;
  return `${(n / 1440).toFixed(1)}d`;
}

function hourLabel(h: number): string {
  const s = h % 12 === 0 ? 12 : h % 12;
  return `${s}${h < 12 ? "a" : "p"}`;
}

/** The two or three busiest hours, as "9a, 10a, 2p". */
export function busiest(hours: number[] | undefined): string {
  const list = (hours ?? []).map((n, h) => [n, h] as const).filter(([n]) => n > 0).sort((a, b) => b[0] - a[0]).slice(0, 3);
  return list.sort((a, b) => a[1] - b[1]).map(([, h]) => hourLabel(h)).join(", ");
}

function Figure({ n, strong }: { n: number | undefined; strong?: boolean }) {
  return n ? <span className={cn("num", strong && "font-medium")}>{n}</span> : <span className="text-faint" aria-label="none">·</span>;
}

function Spark({ values, className }: { values: number[]; className?: string }) {
  const w = 84, h = 18;
  const max = Math.max(1, ...values);
  const step = values.length > 1 ? w / (values.length - 1) : w;
  const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(h - 1 - (v / max) * (h - 3)).toFixed(1)}`).join(" ");
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className={className} aria-hidden>
      <line x1="0" x2={w} y1={h - 0.5} y2={h - 0.5} stroke="var(--border)" />
      <polyline points={pts} fill="none" stroke="var(--accent)" strokeWidth="1.3" strokeLinejoin="round" />
    </svg>
  );
}

const AXIS = [0, 6, 12, 18, 23];

function Hours({ hours, tall }: { hours: number[] | undefined; tall?: boolean }) {
  const list = hours?.length === 24 ? hours : Array(24).fill(0);
  const max = Math.max(1, ...list);
  const h = tall ? 40 : 16, bw = tall ? 10 : 2.5, gap = tall ? 3 : 0.8;
  const w = 24 * (bw + gap);
  // The tall chart carries its hour axis: each label sits under its own bar.
  return (
    <svg width={w} height={tall ? h + 14 : h} aria-hidden data-testid={tall ? "team-hours" : undefined}>
      {list.map((n, i) => (
        <rect key={i} x={i * (bw + gap)} y={h - Math.max(n ? 1.5 : 0.75, (n / max) * h)} width={bw}
          height={Math.max(n ? 1.5 : 0.75, (n / max) * h)}
          fill={n ? "var(--accent)" : "var(--border-strong)"} opacity={n ? 0.35 + 0.65 * (n / max) : 1} />
      ))}
      {tall && AXIS.map((i, k) => (
        <text key={i} x={k === 0 ? 0 : k === AXIS.length - 1 ? w - gap : i * (bw + gap) + bw / 2} y={h + 12}
          textAnchor={k === 0 ? "start" : k === AXIS.length - 1 ? "end" : "middle"}
          fontSize="10.5" fill="var(--fg-faint)" className="num">{hourLabel(i)}</text>
      ))}
    </svg>
  );
}

function Select({ label, value, onChange, options, testId }: { label: string; value: string; onChange: (v: string) => void; options: [string, string][]; testId: string }) {
  return (
    <label className="flex items-center gap-1.5 text-[12px] text-muted">
      {label}
      <select value={value} onChange={(e) => onChange(e.target.value)} data-testid={testId}
        className="h-7 rounded-md border border-control bg-bg px-1.5 text-[12px] text-fg">
        {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>
    </label>
  );
}

// the channels a row shows with no system picked: the rest are one System filter away (the
// table ran to 17 columns and scrolled sideways)
const FIRST: ReadonlySet<Channel> = new Set<Channel>(["mail_sent", "teams", "meetings"]);

/** The Planner insight every screen counts open tasks by (Insights: "Open Planner tasks per
 * person"), read once for the page. */
export const PLANNER_INSIGHT = "planner_open";

/** Pure: a person's open and overdue Planner tasks by their Team key, from the Planner
 * insight's rows (its `people` are the rows' Team keys); a row with no key counts for nobody. */
export function plannerByKey(result: InsightResult | null): Map<string, { open: number; overdue: number }> {
  const out = new Map<string, { open: number; overdue: number }>();
  if (!result) return out;
  const [o, d] = [result.columns.indexOf("open_tasks"), result.columns.indexOf("overdue")];
  if (o < 0) return out;
  result.rows.forEach((r, i) => {
    const key = result.people?.[i];
    if (!key) return;
    const was = out.get(key) ?? { open: 0, overdue: 0 };
    out.set(key, { open: was.open + (Number(r[o]) || 0), overdue: was.overdue + (d < 0 ? 0 : Number(r[d]) || 0) });
  });
  return out;
}

// The planner read is a kept read like the people's (key: its API path): the Team page starts it
// beside its people read (`readPlannerAhead`), so the dashboard does not wait for the people before
// asking, and a visit after the first draws the kept answer and reads again behind it.
const PLANNER_PATH = `/api/insights/${PLANNER_INSIGHT}`;
const plannerRead = (signal?: AbortSignal) => getInsight(PLANNER_INSIGHT, signal);

/** Start the planner read now (the Team page, beside its people read); a failure is dropped. */
export function readPlannerAhead(): void {
  keepAhead(PLANNER_PATH, (s) => plannerRead(s));
}

// A person's panel is read once the pointer rests on their row: the server keeps the answer, so the
// click that follows opens it from memory. Once per person per page load; a failure is the click's.
const readAheadKeys = new Set<string>();
let readAheadTimer: ReturnType<typeof setTimeout> | undefined;
function readAhead(key: string) {
  clearTimeout(readAheadTimer);
  if (readAheadKeys.has(key)) return;
  // only a row the pointer rests on, not every row it passes
  readAheadTimer = setTimeout(() => {
    readAheadKeys.add(key);
    getTeamPerson(key).catch(() => readAheadKeys.delete(key));
  }, 250);
}
const readAheadStop = () => clearTimeout(readAheadTimer);

export function TeamDashboard({ data, person = "" }: { data: TeamView; person?: string }) {
  const { data: plannerResult } = useRead<InsightResult>(PLANNER_PATH, plannerRead);
  const planner = useMemo(() => (plannerResult ? plannerByKey(plannerResult) : null), [plannerResult]);
  const tasksOf = (p: TeamPerson) => planner?.get(p.key)?.open ?? 0;
  // shared mailboxes and accounts with no one on record behind them are listed apart: not people, so
  // out of the people count, the rankings and the totals above
  const people = useMemo(() => (data.people ?? []).filter((p) => !p.shared), [data.people]);
  const accounts = useMemo(() => (data.people ?? []).filter((p) => p.shared).sort((a, b) => a.name.localeCompare(b.name)), [data.people]);
  const [period, setPeriod] = useState<Period>(7);
  const [team, setTeam] = useState("");
  const [role, setRole] = useState("");
  const [system, setSystem] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "total", desc: true });
  const [open, setOpen] = useState<string | null>(null);
  // /team?person=<Team key or name> (a person row on Insights): opens them when exactly one row you
  // can see has that key or name, else narrows the list to it
  useEffect(() => {
    const want = person.trim().toLowerCase();
    if (!want) return;
    const byKey = people.filter((p) => p.key === person.trim());
    const hits = byKey.length ? byKey : people.filter((p) => p.name.toLowerCase() === want);
    if (hits.length === 1) setOpen(hits[0].key);
    else setQuery(person.trim());
  }, [person, people]);

  const teams = useMemo(() => [...new Set(people.map((p) => p.team).filter((t): t is string => !!t))].sort(), [people]);
  const roles = useMemo(() => [...new Set(people.flatMap((p) => p.roles ?? []))].sort(), [people]);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = people.filter((p) =>
      (!team || (team === "-" ? !p.team : p.team === team)) &&
      (!role || (p.roles ?? []).includes(role)) &&
      (!q || p.name.toLowerCase().includes(q)) &&
      (!system || CHANNELS.some((c) => c.system === system && (counts(p, period)[c.key] ?? 0) > 0)));
    const value = (p: TeamPerson): number | string => {
      switch (sort.key) {
        case "name": return p.name.toLowerCase();
        case "team": return (p.team ?? "~").toLowerCase();
        case "total": return total(p, period);
        case "week": return thisWeek(p)?.done ?? 0;
        case "saved": return thisWeek(p)?.saved_minutes ?? -1;
        case "awaiting_reply": return p.awaiting_reply;
        case "tasks": return tasksOf(p);
        case "alerts": return p.alerts + p.tasks;
        case "reply": return replyOf(p, period) ?? (sort.desc ? -1 : Number.MAX_SAFE_INTEGER);
        default: return counts(p, period)[sort.key] ?? 0;
      }
    };
    return [...list].sort((a, b) => {
      const x = value(a), y = value(b);
      const c = x < y ? -1 : x > y ? 1 : a.name.localeCompare(b.name);
      return sort.desc ? -c : c;
    });
  }, [people, team, role, system, query, period, sort]);

  // a column nobody has a figure in (for this period) is left out, so the table carries no rows of zeros
  const shown = CHANNELS.filter((c) => (system ? c.system === system : FIRST.has(c.key)) && people.some((p) => (counts(p, period)[c.key] ?? 0) > 0));
  const groups = shown.reduce<{ system: string; span: number }[]>((acc, c) => {
    const last = acc[acc.length - 1];
    if (last && last.system === c.system) last.span += 1;
    else acc.push({ system: c.system, span: 1 });
    return acc;
  }, []);

  // time saved is only ever filled for rows the reader may see a figure for
  const savedShown = people.some((p) => (thisWeek(p)?.saved_minutes ?? 0) > 0);
  // the other columns too: one nobody has a figure in is left out
  const has = {
    // over 7 days "this week" is all but the same figure as All
    week: period === 28 && people.some((p) => (thisWeek(p)?.done ?? 0) > 0),
    awaiting_reply: people.some((p) => p.awaiting_reply > 0),
    tasks: people.some((p) => tasksOf(p) > 0),
    alerts: people.some((p) => p.alerts + p.tasks > 0),
    reply: people.some((p) => replyOf(p, period) != null),
    trend: people.some((p) => (p.trend ?? []).some((n) => n > 0)),
    hours: people.some((p) => !!busiest(p.hours)),
  };
  const openSpan = [has.awaiting_reply, has.tasks, has.alerts].filter(Boolean).length;
  const tailSpan = [has.reply, has.trend, has.hours].filter(Boolean).length;

  const sum = (f: (p: TeamPerson) => number) => rows.reduce((s, p) => s + f(p), 0);
  const replies = rows.map((p) => replyOf(p, period)).filter((n): n is number => n != null).sort((a, b) => a - b);
  const medianReply = replies.length ? replies[Math.floor((replies.length - 1) / 2)] : null;

  const head = (key: SortKey, label: string, title: string, className?: string) => (
    <th key={key} scope="col" title={title} aria-sort={sort.key === key ? (sort.desc ? "descending" : "ascending") : undefined}
      className={cn("whitespace-nowrap px-2 py-1.5 font-normal", className)}>
      <button type="button" data-tap="inline" onClick={() => setSort((s) => ({ key, desc: s.key === key ? !s.desc : key !== "name" && key !== "team" }))}
        className={cn("cursor-pointer hover:text-fg", sort.key === key && "text-fg font-medium")}>
        {label}{sort.key === key ? (sort.desc ? " ↓" : " ↑") : ""}
      </button>
    </th>
  );

  if (data.state !== "ready") {
    return (
      <p className="text-[13px] text-muted" data-testid="team-not-ready">
        {data.state === "blocked" ? "Activity can't be counted right now. Try again in a few minutes." : "Activity has not been counted yet. It is counted every night."}
      </p>
    );
  }

  return (
    <div className="space-y-3" data-testid="team-dashboard">
      <p className="max-w-[80ch] text-[13px] leading-5 text-muted" data-testid="team-intro">
        What each person sent and did over the period, and the replies and tasks still open with them. Open a name to see their week.
      </p>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div role="radiogroup" aria-label="Period" className="flex h-7 overflow-hidden rounded-md border border-control text-[12px] max-sm:h-10">
          {([7, 28] as Period[]).map((p) => (
            <button key={p} type="button" role="radio" aria-checked={period === p} onClick={() => setPeriod(p)} data-testid={`period-${p}`}
              className={cn("px-2.5 cursor-pointer", period === p ? "bg-accent-soft text-fg font-medium" : "text-muted hover:text-fg")}>
              {p} days
            </button>
          ))}
        </div>
        {data.scope === "everyone" && (
          <>
            {teams.length > 0 && <Select label="Team" value={team} onChange={setTeam} testId="filter-team" options={[["", "All"], ...teams.map((t) => [t, t] as [string, string]), ["-", "No team"]]} />}
            {roles.length > 0 && <Select label="Role" value={role} onChange={setRole} testId="filter-role" options={[["", "All"], ...roles.map((r) => [r, r] as [string, string])]} />}
            <Select label="System" value={system} onChange={setSystem} testId="filter-system" options={[["", "All"], ...[...new Set(CHANNELS.map((c) => c.system))].map((s) => [s, GROUPS[s].label] as [string, string])]} />
            <input type="search" placeholder="Find a person" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Find a person"
              className="h-7 w-44 rounded-md border border-control bg-bg px-2 text-[12px]" />
          </>
        )}
        <span className="ml-auto truncate text-[12px] text-muted" data-testid="team-note">
          {data.as_of ? `Counted ${eastern(data.as_of)}` : ""}
        </span>
      </div>

      <dl className="flex flex-wrap gap-x-6 gap-y-1 border-y border-border py-2 text-[12px] text-muted" data-testid="team-totals">
        {([
          ["People", rows.length],
          ["Mail sent", sum((p) => counts(p, period).mail_sent ?? 0)],
          ["Teams messages", sum((p) => counts(p, period).teams ?? 0)],
          ["Meetings", sum((p) => counts(p, period).meetings ?? 0)],
          ["Awaiting reply", sum((p) => p.awaiting_reply)],
        ] as [string, number][]).map(([k, v]) => (
          <div key={k} className="flex items-baseline gap-1.5"><dt>{k}</dt><dd className="num text-[15px] text-fg">{v.toLocaleString("en-US")}</dd></div>
        ))}
        <div className="flex items-baseline gap-1.5" title="Microsoft Planner tasks not completed, assigned to the people listed (the same count as the Open Planner tasks per person chart)">
          <dt>Open Planner tasks</dt><dd className="num text-[15px] text-fg" data-testid="team-planner-total">{planner ? sum(tasksOf).toLocaleString("en-US") : "·"}</dd>
        </div>
        <div className="flex items-baseline gap-1.5"><dt>Median reply</dt><dd className="num text-[15px] text-fg">{minutes(medianReply) || "·"}</dd></div>
      </dl>

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-[12.5px]" data-testid="team-table">
          <thead className="sticky top-0 z-10 bg-bg text-left text-[11.5px] text-muted">
            <tr className="text-[11px] uppercase tracking-[0.04em] text-faint">
              {/* person, team (when any is on record), all, this week and saved (when shown) */}
              <th colSpan={(teams.length > 0 ? 2 : 1) + (has.week ? 1 : 0) + 1 + (savedShown ? 1 : 0)} />
              {groups.map((g) => (
                <th key={g.system} colSpan={g.span} scope="colgroup" className="whitespace-nowrap border-b-2 px-2 pt-1 text-center font-normal"
                  style={{ borderColor: GROUPS[g.system].tone }}>{GROUPS[g.system].label}</th>
              ))}
              {openSpan > 0 && <th colSpan={openSpan} scope="colgroup" className="whitespace-nowrap border-b-2 border-dot-risk px-2 pt-1 text-center font-normal">Open</th>}
              {tailSpan > 0 && <th colSpan={tailSpan} />}
            </tr>
            <tr className="border-b border-border">
              {head("name", "Person", "Name", "min-w-[150px]")}
              {teams.length > 0 && head("team", "Team", "Team, from the owner's assignment, the directory or the role the work shows")}
              {head("total", "All", "Everything they did in the period except mail received", "text-right")}
              {has.week && head("week", "Week", "Work done this week so far (Monday on): everything but mail received", "text-right")}
              {savedShown && head("saved", "Saved", "Time the Workbench saved them this week (the work log's estimate)", "text-right")}
              {shown.map((c) => head(c.key, c.head, c.title, cn("text-right", system === c.system && "text-fg")))}
              {has.awaiting_reply && head("awaiting_reply", "Reply", "Threads awaiting their reply", "text-right")}
              {has.tasks && head("tasks", "Tasks", "Open Planner tasks assigned to them", "text-right")}
              {has.alerts && head("alerts", "Alerts", "Other open alerts on them, stuck or overdue tasks among them", "text-right")}
              {has.reply && head("reply", "Median", "Median time to reply to a message addressed to them", "text-right")}
              {has.trend && <th scope="col" className="whitespace-nowrap px-2 py-1.5 font-normal" title="Everything they did per day, last 28 days">28-day trend</th>}
              {has.hours && <th scope="col" className="whitespace-nowrap px-2 py-1.5 font-normal" title="Messages sent per hour of the day, Eastern time, last 28 days">Busiest hours</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const c = counts(p, period);
              return (
                <tr key={p.key} data-testid="team-row" onClick={() => setOpen(p.key)} onPointerEnter={() => readAhead(p.key)} onPointerLeave={readAheadStop} onFocus={() => readAhead(p.key)}
                  className="cursor-pointer border-b border-border hover:bg-hover">
                  <td className="px-2 py-1.5">
                    <button type="button" data-tap="inline" className="text-left font-medium hover:underline" onClick={(e) => { e.stopPropagation(); setOpen(p.key); }}>
                      {p.name}
                    </button>
                    {p.self && <span className="ml-1.5 text-[11px] text-muted">you</span>}
                    {(p.roles ?? []).some((r) => r !== p.team) && <div className="truncate text-[11px] text-muted" data-testid="team-roles">{(p.roles ?? []).filter((r) => r !== p.team).join(", ")}</div>}
                  </td>
                  {teams.length > 0 && <td className="whitespace-nowrap px-2 py-1.5 text-muted">{p.team ?? <span className="text-faint">·</span>}</td>}
                  <td className="px-2 py-1.5 text-right"><Figure n={total(p, period)} strong /></td>
                  {has.week && <td className="whitespace-nowrap px-2 py-1.5 text-right" data-testid="team-week"
                    title={lastWeek(p) ? `Last week: ${lastWeek(p)!.done.toLocaleString("en-US")}` : undefined}>
                    <Figure n={thisWeek(p)?.done} />
                  </td>}
                  {savedShown && <td className="whitespace-nowrap px-2 py-1.5 text-right num">{thisWeek(p)?.saved_minutes ? minutes(thisWeek(p)!.saved_minutes) : <span className="text-faint">·</span>}</td>}
                  {shown.map((ch) => (
                    <td key={ch.key} className={cn("px-2 py-1.5 text-right", system === ch.system && "bg-accent-soft/40")}><Figure n={c[ch.key]} /></td>
                  ))}
                  {has.awaiting_reply && <td className="px-2 py-1.5 text-right">{p.awaiting_reply ? <span className="num font-medium text-risk">{p.awaiting_reply}</span> : <Figure n={0} />}</td>}
                  {has.tasks && <td className="px-2 py-1.5 text-right" title={planner?.get(p.key)?.overdue ? `${planner.get(p.key)!.overdue} past due` : undefined}><Figure n={tasksOf(p)} /></td>}
                  {has.alerts && <td className="px-2 py-1.5 text-right"><Figure n={p.alerts + p.tasks} /></td>}
                  {has.reply && <td className="whitespace-nowrap px-2 py-1.5 text-right num">{minutes(replyOf(p, period)) || <span className="text-faint">·</span>}</td>}
                  {has.trend && <td className="px-2 py-1.5"><Spark values={p.trend ?? []} /></td>}
                  {has.hours && <td className="whitespace-nowrap px-2 py-1.5" title={busiest(p.hours) ? `Busiest: ${busiest(p.hours)}` : undefined}>
                    <span className="inline-flex flex-col items-start gap-0.5"><Hours hours={p.hours} /><span className="num text-[11px] leading-3 text-muted">{busiest(p.hours)}</span></span>
                  </td>}
                </tr>
              );
            })}
          </tbody>
        </table>
        {rows.length === 0 && <p className="py-4 text-[13px] text-muted">{people.length === 0 ? "No one has activity counted yet." : "Nobody matches these filters."}</p>}
      </div>
      {data.scope === "everyone" && accounts.length > 0 && (
        <section className="space-y-1.5 pt-4" aria-label="Shared mailboxes and accounts" data-testid="team-accounts">
          <h2 className="text-[13px] font-semibold">Shared mailboxes and accounts <span className="num font-normal text-muted">{accounts.length}</span></h2>
          <p className="max-w-[80ch] text-[12px] text-muted">Named only by their address, with no manager, department or title on record: not counted as people, not ranked.</p>
          <table className="w-full max-w-3xl border-collapse text-[12.5px]">
            <thead className="text-left text-[11.5px] text-muted">
              <tr className="border-b border-border">
                <th scope="col" className="px-2 py-1 font-normal">Account</th>
                <th scope="col" className="px-2 py-1 text-right font-normal">All</th>
                <th scope="col" className="px-2 py-1 text-right font-normal">Mail sent</th>
                <th scope="col" className="px-2 py-1 text-right font-normal">Mail received</th>
                <th scope="col" className="px-2 py-1 text-right font-normal">Teams</th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((a) => {
                const c = counts(a, period);
                return (
                  <tr key={a.key} className="cursor-pointer border-b border-border hover:bg-hover" data-testid="team-account-row" onClick={() => setOpen(a.key)}>
                    <td className="px-2 py-1.5 font-medium">{a.name}</td>
                    <td className="px-2 py-1.5 text-right"><Figure n={total(a, period)} strong /></td>
                    <td className="px-2 py-1.5 text-right"><Figure n={c.mail_sent} /></td>
                    <td className="px-2 py-1.5 text-right"><Figure n={c.mail_received} /></td>
                    <td className="px-2 py-1.5 text-right"><Figure n={c.teams} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      )}
      {open && <PersonSheet key={open} personKey={open} onClose={() => setOpen(null)} onOpen={setOpen} />}
    </div>
  );
}

const BUCKETS: { key: TeamItem["bucket"]; label: string }[] = [
  { key: "awaiting_reply", label: "Replies owed to them" },
  { key: "tasks", label: "Open tasks" },
  { key: "alerts", label: "Other alerts" },
];

// The productivity tracker (internal; the owner is never listed): per week what they got
// done against what came in, alerts closed, reply time and the time the Workbench saved.
export function WeekTracker({ weeks, waiting }: { weeks: TeamTrackerWeek[]; waiting: number | { replies: number; tasks: number; alerts: number } }) {
  const saved = weeks.some((w) => w.saved_minutes != null);
  const cell = "px-2 py-1 text-right num";
  const parts = typeof waiting === "number" ? null : waiting;
  const total = parts ? parts.replies + parts.tasks + parts.alerts : waiting as number;
  return (
    <div className="space-y-1.5">
      <table className="w-full border-collapse text-[12.5px]" data-testid="team-weeks">
        <thead className="text-left text-[11.5px] text-muted">
          <tr className="border-b border-border">
            <th scope="col" className="px-2 py-1 font-normal">Week of</th>
            <th scope="col" className="px-2 py-1 text-right font-normal" title="Everything they sent or did: mail, Teams, meetings and each system's recorded actions">Done</th>
            <th scope="col" className="px-2 py-1 text-right font-normal" title="Mail they received from people">Came in</th>
            <th scope="col" className="px-2 py-1 text-right font-normal" title="Their alerts closed that week">Closed</th>
            <th scope="col" className="px-2 py-1 text-right font-normal" title="Median time to reply that week, and how many replies it rests on">Median reply</th>
            {saved && <th scope="col" className="px-2 py-1 text-right font-normal" title="Time the Workbench saved them: an estimate from the work log">Saved</th>}
          </tr>
        </thead>
        <tbody>
          {weeks.map((w) => (
            <tr key={w.week} className="border-b border-border" data-testid="team-week-row">
              <td className="whitespace-nowrap px-2 py-1">{weekOf(w.week)}{w.partial && <span className="ml-1.5 text-[11px] text-muted">to {weekOf(w.to)}</span>}</td>
              <td className={cn(cell, "font-medium")}>{w.done.toLocaleString("en-US")}</td>
              <td className={cell}>{w.received.toLocaleString("en-US")}</td>
              <td className={cell}>{w.closed ?? 0}</td>
              <td className={cn(cell, "whitespace-nowrap")}>{w.reply_minutes != null ? <>{minutes(w.reply_minutes)}<span className="ml-1 text-[11px] text-muted">({w.replies})</span></> : <span className="text-muted">no replies</span>}</td>
              {saved && <td className={cell}>{w.saved_minutes ? minutes(w.saved_minutes) : <span className="text-muted">none</span>}</td>}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-[12px] text-muted" data-testid="team-waiting">{total ? (parts ? `${total.toLocaleString("en-US")} open on them now: ${parts.replies.toLocaleString("en-US")} replies owed to them, ${parts.tasks.toLocaleString("en-US")} tasks and ${parts.alerts.toLocaleString("en-US")} alerts.` : `${total.toLocaleString("en-US")} waiting on them now (replies, tasks and alerts).`) : "Nothing waiting on them now."}</p>
    </div>
  );
}

function dayLabel(iso: string): string {
  return new Date(`${iso}T12:00:00`).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }).replace("Sept", "Sep");
}

function PersonSheet({ personKey, onClose, onOpen }: { personKey: string; onClose: () => void; onOpen: (key: string) => void }) {
  const [data, setData] = useState<TeamPersonDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    const ctl = new AbortController();
    getTeamPerson(personKey, ctl.signal).then(setData, (e) => { if (!ctl.signal.aborted) setError(e); });
    return () => ctl.abort();
  }, [personKey]);
  const p = data?.person;
  return (
    <Sheet open onOpenChange={(o) => !o && onClose()} title={p?.name ?? "Person"} className="sm:w-[min(720px,calc(100vw-16px))]">
      {error ? <p className="text-[13px]">{plainError(error)}</p> : !data || !p ? <p className="text-[13px] text-muted">Loading</p> : (
        <div className="space-y-6" data-testid="team-person">
          <div>
            <div className="text-[15px] font-semibold">{p.name}</div>
            <div className="text-[12px] text-muted">{[p.team, ...(p.roles ?? []).filter((r) => r !== p.team)].filter(Boolean).join(" · ") || "No team or role on record"}</div>
          </div>

          <section className="space-y-2">
          <h3 className="text-[12px] font-medium text-muted" data-testid="team-window">Counts for the last 28 days</h3>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-[12px] sm:grid-cols-4">
            {CHANNELS.filter((c) => (p.d28?.[c.key] ?? 0) > 0).map((c) => (
              <div key={c.key} title={c.title}><dt className="text-muted">{c.short}</dt><dd className="num text-[15px]">{p.d28?.[c.key]}</dd></div>
            ))}
            <div title="Median time to reply, last 28 days"><dt className="text-muted">Median reply</dt><dd className="num text-[15px]">{minutes(p.reply_minutes_28) || "none yet"}</dd></div>
          </dl>
          </section>

          {(p.weeks ?? []).length > 0 && (
            <section className="cv-auto cv-tile space-y-2">
              <h3 className="text-[12px] font-medium text-muted">Week by week</h3>
              <WeekTracker weeks={p.weeks ?? []} waiting={{ replies: p.awaiting_reply, tasks: p.tasks, alerts: p.alerts }} />
            </section>
          )}

          <section className="cv-auto cv-tile space-y-2">
            <h3 className="text-[12px] font-medium text-muted">Each day, last 28 days</h3>
            <Timeline days={data.timeline ?? []} />
          </section>

          {BUCKETS.map((b) => {
            const items = (data.items ?? []).filter((it) => it.bucket === b.key);
            return (
              <section key={b.key} className="space-y-1.5">
                <h3 className="text-[12px] font-medium text-muted">{b.label} <span className="num">{items.length}</span></h3>
                {items.length === 0 ? <p className="text-[12px] text-faint">None open.</p> : (
                  <ul className="divide-y divide-border border-y border-border text-[12.5px]">
                    {items.map((it) => (
                      <li key={it.id} className="flex items-baseline gap-3 py-1.5" data-testid="team-item">
                        <span className="num w-9 shrink-0 text-right text-muted">{it.age_days == null ? "" : `${it.age_days}d`}</span>
                        <span className="min-w-0 flex-1">
                          <span className="font-medium">{it.title}</span>
                          <span className="block truncate text-[11.5px] text-muted">{[it.counterpart, it.channel === "teams" ? "Teams" : it.channel === "mail" ? "Mail" : null, b.key === "awaiting_reply" ? null : `answered by ${it.waiting_on}`].filter(Boolean).join(" · ")}</span>
                        </span>
                        {it.record_id && it.record_type && (
                          <Link className="shrink-0 text-[12px] text-accent hover:underline" href={`/object/${encodeURIComponent(it.record_type)}/${encodeURIComponent(it.record_id)}`}>Open</Link>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            );
          })}
          {data.items_unread && <p className="text-[12px] text-risk" data-testid="team-items-unread">Their open items could not be read just now. The counts above stand; open the person again in a moment.</p>}
          {data.withheld > 0 && <p className="text-[12px] text-muted" data-testid="team-withheld">{data.withheld} more not shown: marked, or not open to you.</p>}

          <div className="grid gap-6 sm:grid-cols-2">
            <section className="space-y-1.5">
              <h3 className="text-[12px] font-medium text-muted">Works most with</h3>
              {(data.partners ?? []).length === 0 ? <p className="text-[12px] text-faint">Nobody on record.</p> : (
                <ul className="space-y-1 text-[12.5px]">
                  {(() => {
                    const top = Math.max(1, ...(data.partners ?? []).map((x) => x.n));
                    return (data.partners ?? []).map((x) => (
                      <li key={x.name} className="flex items-center gap-2">
                        {x.key ? <button type="button" data-tap="inline" className="w-36 truncate text-left hover:underline" onClick={() => onOpen(x.key!)}>{x.name}</button> : <span className="w-36 truncate">{x.name}</span>}
                        <span className="h-1.5 rounded-sm bg-accent/60" style={{ width: `${Math.max(4, (x.n / top) * 120)}px` }} />
                        <span className="num text-[11.5px] text-muted">{x.n}</span>
                      </li>
                    ));
                  })()}
                </ul>
              )}
            </section>
            <section className="space-y-1.5">
              <h3 className="text-[12px] font-medium text-muted">Busiest hours, Eastern</h3>
              <Hours hours={p.hours} tall />
              <p className="sr-only">{busiest(p.hours) ? `Busiest: ${busiest(p.hours)}` : "No messages counted by hour."}</p>
            </section>
          </div>
        </div>
      )}
    </Sheet>
  );
}

function Timeline({ days }: { days: { day: string; counts?: Record<string, number> }[] }) {
  const rows = CHANNELS.filter((c) => days.some((d) => (d.counts?.[c.key] ?? 0) > 0));
  if (rows.length === 0) return <p className="text-[12px] text-faint">Nothing counted in these days.</p>;
  // Wider than a phone: it scrolls sideways and opens on the latest day (rtl scroller), names pinned.
  return (
    <div className="w-fit max-w-full overflow-x-auto [direction:rtl]" data-testid="team-timeline-scroll">
    <table className="border-collapse text-[11.5px] [direction:ltr]" data-testid="team-timeline">
      <tbody>
        {rows.map((c) => {
          const max = Math.max(1, ...days.map((d) => d.counts?.[c.key] ?? 0));
          return (
            <tr key={c.key}>
              <th scope="row" className="sticky left-0 z-[1] whitespace-nowrap bg-raised pr-3 text-left font-normal text-muted">{c.short}</th>
              {days.map((d) => {
                const n = d.counts?.[c.key] ?? 0;
                return (
                  <td key={d.day} className="p-[1.5px]" title={`${dayLabel(d.day)}: ${n}`}>
                    <div className="size-[13px] rounded-[2px]" style={{ background: n ? GROUPS[c.system].tone : "var(--surface-2)", opacity: n ? 0.25 + 0.75 * (n / max) : 1 }} />
                  </td>
                );
              })}
            </tr>
          );
        })}
        <tr>
          <td className="sticky left-0 bg-raised" />
          {days.map((d, i) => (
            <td key={d.day} className="text-center text-[9.5px] text-faint num">{i % 7 === 0 || i === days.length - 1 ? new Date(`${d.day}T12:00:00`).getDate() : ""}</td>
          ))}
        </tr>
      </tbody>
    </table>
    </div>
  );
}
