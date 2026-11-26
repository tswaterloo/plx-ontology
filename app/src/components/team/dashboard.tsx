"use client";

import { Activity, CalendarClock, ChevronDown, Clock3, MessageCircle, Search, Users, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { LoadProblem } from "@/components/ui/notice";
import { Sheet } from "@/components/ui/sheet";
import { Pending } from "@/components/ui/skeleton";
import { getInsight, getTeamPerson } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { readAhead as keepAhead, useRead } from "@/lib/use-read";
import { eastern, isBeforeToday } from "@/lib/format";
import type { InsightResult, TeamItem, TeamPerson, TeamPersonDetail, TeamTrackerWeek, TeamView } from "@/lib/types";

type Channel = "mail_sent" | "mail_received" | "teams" | "meetings" | "mc_approvals" | "mc_questions" | "mc_tasks" | "planner" | "sharepoint" | "bc" | "portal" | "sage";

const CHANNELS: { key: Channel; short: string; head: string; title: string; system: string }[] = [
  { key: "mail_sent", short: "Mail sent", head: "Mail sent", title: "Mail sent", system: "mail" },
  { key: "mail_received", short: "Mail received", head: "Mail recv", title: "Mail received from people", system: "mail" },
  { key: "teams", short: "Teams messages", head: "Teams", title: "Teams messages sent", system: "teams" },
  { key: "meetings", short: "Meetings", head: "Mtgs", title: "Meetings organised or attended", system: "meetings" },
  { key: "mc_approvals", short: "MC approvals", head: "MC appr", title: "Mission Control approvals requested or decided", system: "mc" },
  { key: "mc_questions", short: "MC questions", head: "MC Q&A", title: "Mission Control questions asked or answered", system: "mc" },
  { key: "mc_tasks", short: "MC steps", head: "MC steps", title: "Other Mission Control task steps", system: "mc" },
  { key: "planner", short: "Planner tasks", head: "Planner", title: "Planner tasks created, assigned or completed", system: "planner" },
  { key: "sharepoint", short: "SharePoint files", head: "Files", title: "SharePoint files created or changed", system: "sharepoint" },
  { key: "bc", short: "BC records", head: "BC", title: "Business Central records created or changed", system: "bc" },
  { key: "portal", short: "Portal records", head: "Portal", title: "Portal records created or changed", system: "portal" },
  { key: "sage", short: "Sage entries", head: "Sage", title: "Sage entries", system: "sage" },
];

const GROUPS: Record<string, { label: string; tone: string }> = {
  mail: { label: "Mail", tone: "var(--sys-mail)" },
  teams: { label: "Teams", tone: "var(--sys-teams)" },
  meetings: { label: "Calendar", tone: "var(--risk)" },
  mc: { label: "Mission Control", tone: "var(--sys-mc)" },
  planner: { label: "Planner", tone: "var(--sys-sage)" },
  sharepoint: { label: "SharePoint", tone: "var(--sys-file)" },
  bc: { label: "BC", tone: "var(--sys-bc)" },
  portal: { label: "Portal", tone: "var(--sys-portal)" },
  sage: { label: "Sage", tone: "var(--sys-sage)" },
};

const AVATAR_MONO = "border border-border-strong bg-surface-2 text-fg/90";

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

function outputMix(p: TeamPerson, period: Period): { comms: number; docs: number; erp: number; sum: number } {
  const c = counts(p, period);
  const comms = (c.mail_sent ?? 0) + (c.teams ?? 0) + (c.meetings ?? 0);
  const docs = (c.sharepoint ?? 0) + (c.planner ?? 0);
  const erp = (c.bc ?? 0) + (c.portal ?? 0) + (c.sage ?? 0) + (c.mc_approvals ?? 0) + (c.mc_questions ?? 0) + (c.mc_tasks ?? 0);
  return { comms, docs, erp, sum: comms + docs + erp };
}

function initialsOf(name: string): string {
  const parts = displayName(name)
    .replace(/@.*/, "")
    .split(/[\s._-]+/)
    .filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return `${parts[0][0]}${parts[parts.length - 1][0]}`.toUpperCase();
}

function replyOf(p: TeamPerson, period: Period): number | null {
  return (period === 7 ? p.reply_minutes_7 : p.reply_minutes_28) ?? null;
}

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

export function busiest(hours: number[] | undefined): string {
  const list = (hours ?? []).map((n, h) => [n, h] as const).filter(([n]) => n > 0).sort((a, b) => b[0] - a[0]).slice(0, 3);
  return list.sort((a, b) => a[1] - b[1]).map(([, h]) => hourLabel(h)).join(", ");
}

function Figure({ n, strong }: { n: number | undefined; strong?: boolean }) {
  return n ? (
    <span className={cn("num", strong && "font-semibold text-fg")}>{n.toLocaleString("en-US")}</span>
  ) : (
    <span className="text-faint/45" aria-label="none">-</span>
  );
}

function OutputBar({
  mix,
  maxTotal,
  thick = false,
}: {
  mix: { comms: number; docs: number; erp: number; sum: number };
  maxTotal: number;
  thick?: boolean;
}) {
  const heightCls = thick ? "h-3 w-full" : "h-2 w-24";
  if (mix.sum <= 0 || maxTotal <= 0) {
    return (
      <div className={cn(heightCls, "overflow-hidden rounded-xs bg-surface-2")} aria-hidden>
        <div className="h-full w-0" />
      </div>
    );
  }
  const scale = Math.max(thick ? 6 : 8, Math.round((mix.sum / maxTotal) * 100));
  const commsPct = (mix.comms / mix.sum) * scale;
  const docsPct = (mix.docs / mix.sum) * scale;
  const erpPct = (mix.erp / mix.sum) * scale;
  return (
    <div
      className={cn("flex overflow-hidden rounded-xs bg-surface-2", heightCls)}
      title={`Comms ${mix.comms}, Docs & Planner ${mix.docs}, ERP & Ops ${mix.erp}`}
    >
      {commsPct > 0 && <span className="h-full bg-accent transition-[width] duration-200" style={{ width: `${commsPct}%` }} />}
      {docsPct > 0 && <span className="h-full bg-ok transition-[width] duration-200" style={{ width: `${docsPct}%` }} />}
      {erpPct > 0 && <span className="h-full bg-risk transition-[width] duration-200" style={{ width: `${erpPct}%` }} />}
    </div>
  );
}

function Spark({ values, className }: { values: number[]; className?: string }) {
  const w = 84, h = 18;
  const max = Math.max(1, ...values);
  const step = values.length > 1 ? w / (values.length - 1) : w;
  const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(h - 1 - (v / max) * (h - 3)).toFixed(1)}`).join(" ");
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className={className} aria-hidden>
      <line x1="0" x2={w} y1={h - 0.5} y2={h - 0.5} stroke="var(--border)" />
      <polyline points={pts} fill="none" stroke="var(--accent)" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

const AXIS = [0, 6, 12, 18, 23];

function Hours({ hours, tall }: { hours: number[] | undefined; tall?: boolean }) {
  const list = hours?.length === 24 ? hours : Array(24).fill(0);
  const max = Math.max(1, ...list);
  const h = tall ? 40 : 16, bw = tall ? 10 : 2.5, gap = tall ? 3 : 0.8;
  const w = 24 * (bw + gap);
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
    <label className="inline-flex items-center gap-1.5 text-[12.5px] font-medium text-muted">
      <span>{label}</span>
      <span className="relative inline-flex items-center">
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          data-testid={testId}
          className="h-8 appearance-none rounded-md border border-border bg-surface pl-2.5 pr-7 text-[12.5px] font-medium text-fg transition-colors duration-150 hover:border-border-strong focus:border-border-strong focus:outline-none focus-visible:ring-2 focus-visible:ring-ring/40 cursor-pointer"
        >
          {options.map(([v, l]) => <option key={v} value={v} className="bg-bg text-fg">{l}</option>)}
        </select>
        <ChevronDown className="pointer-events-none absolute right-2 size-3.5 text-muted" strokeWidth={2} aria-hidden />
      </span>
    </label>
  );
}

function displayName(name: string | null | undefined): string {
  const s = (name ?? "").trim();
  if (!s || s.includes("@") || s !== s.toLowerCase()) return s;
  return s.replace(/\b([a-z])/g, (m) => m.toUpperCase());
}

const FIRST: ReadonlySet<Channel> = new Set<Channel>(["mail_sent", "teams", "meetings"]);

export const PLANNER_INSIGHT = "planner_open";

export function plannerByKey(result: InsightResult | null): Map<string, { open: number; overdue: number }> {
  const out = new Map<string, { open: number; overdue: number }>();
  if (!result || !Array.isArray(result.columns) || !Array.isArray(result.rows)) return out;
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

const PLANNER_PATH = `/api/insights/${PLANNER_INSIGHT}`;
const plannerRead = (signal?: AbortSignal) => getInsight(PLANNER_INSIGHT, signal);

export function readPlannerAhead(): void {
  keepAhead(PLANNER_PATH, (s) => plannerRead(s));
}

const readAheadKeys = new Set<string>();
let readAheadTimer: ReturnType<typeof setTimeout> | undefined;
function readAhead(key: string) {
  clearTimeout(readAheadTimer);
  if (readAheadKeys.has(key)) return;
  readAheadTimer = setTimeout(() => {
    readAheadKeys.add(key);
    getTeamPerson(key).catch(() => readAheadKeys.delete(key));
  }, 250);
}
const readAheadStop = () => clearTimeout(readAheadTimer);

function splitCsv(raw: string | null | undefined): string[] {
  if (!raw) return [];
  return raw
    .split(/[,;\uFF0C]+/)
    .map((x) => x.trim())
    .filter(Boolean);
}

function cleanCsv(raw: string | null | undefined): string {
  return splitCsv(raw).join(", ");
}

export function formatRoleList(roles: string[] | string | null | undefined, team?: string | null): string[] {
  const teamKey = cleanCsv(team).toLowerCase();
  const list = Array.isArray(roles) ? roles : roles ? [roles] : [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const entry of list) {
    for (const part of splitCsv(entry)) {
      const key = part.toLowerCase();
      if (!key || key === teamKey || seen.has(key)) continue;
      seen.add(key);
      out.push(part);
    }
  }
  return out;
}

export function TeamDashboard({ data, person = "" }: { data: TeamView; person?: string }) {
  const { data: plannerResult } = useRead<InsightResult>(PLANNER_PATH, plannerRead);
  const planner = useMemo(() => (plannerResult ? plannerByKey(plannerResult) : null), [plannerResult]);
  const tasksOf = (p: TeamPerson) => planner?.get(p.key)?.open ?? 0;
  const people = useMemo(() => (data.people ?? []).filter((p) => !p.shared), [data.people]);
  const accounts = useMemo(
    () => (data.people ?? []).filter((p) => p.shared).sort((a, b) => (a.name ?? "").localeCompare(b.name ?? "")),
    [data.people],
  );
  const [period, setPeriod] = useState<Period>(7);
  const [team, setTeam] = useState("");
  const [role, setRole] = useState("");
  const [system, setSystem] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "total", desc: true });
  const [open, setOpen] = useState<string | null>(null);
  const handledPerson = useRef<string>("");

  useEffect(() => {
    const raw = person.trim();
    const want = raw.toLowerCase();
    if (!want) {
      handledPerson.current = "";
      return;
    }
    if (handledPerson.current === want) return;
    const allPeople = [...people, ...accounts];
    const byKey = allPeople.filter((p) => p.key === raw);
    const byExact = byKey.length ? byKey : allPeople.filter((p) => (p.name ?? "").toLowerCase() === want);
    const hits = byExact.length ? byExact : people.filter((p) => (p.name ?? "").toLowerCase().includes(want));
    handledPerson.current = want;
    if (hits.length === 1) setOpen(hits[0].key);
    else setQuery(raw);
  }, [person, people, accounts]);

  const closeSheet = () => {
    setOpen(null);
    if (typeof window !== "undefined") {
      const q = new URLSearchParams(window.location.search);
      if (q.has("person")) {
        q.delete("person");
        const qs = q.toString();
        window.history.replaceState(null, "", `${window.location.pathname}${qs ? `?${qs}` : ""}${window.location.hash}`);
      }
    }
  };

  const teams = useMemo(() => [...new Set(people.map((p) => cleanCsv(p.team)).filter(Boolean))].sort(), [people]);
  const roles = useMemo(() => [...new Set(people.flatMap((p) => formatRoleList(p.roles)))].sort(), [people]);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = people.filter((p) => {
      const pt = cleanCsv(p.team);
      const pr = formatRoleList(p.roles);
      return (
        (!team || (team === "-" ? !pt : pt === team)) &&
        (!role || pr.includes(role)) &&
        (!q || (p.name ?? "").toLowerCase().includes(q)) &&
        (!system || CHANNELS.some((c) => c.system === system && (counts(p, period)[c.key] ?? 0) > 0))
      );
    });
    const value = (p: TeamPerson): number | string => {
      switch (sort.key) {
        case "name": return (p.name ?? "").toLowerCase();
        case "team": return (p.team ?? "~").toLowerCase();
        case "total": return total(p, period);
        case "week": return thisWeek(p)?.done ?? 0;
        case "saved": return thisWeek(p)?.saved_minutes ?? -1;
        case "awaiting_reply": return p.awaiting_reply;
        case "tasks": return planner?.get(p.key)?.open ?? 0;
        case "alerts": return p.alerts + p.tasks;
        case "reply": return replyOf(p, period) ?? (sort.desc ? -1 : Number.MAX_SAFE_INTEGER);
        default: return counts(p, period)[sort.key] ?? 0;
      }
    };
    return [...list].sort((a, b) => {
      const x = value(a), y = value(b);
      const c = x < y ? -1 : x > y ? 1 : (a.name ?? "").localeCompare(b.name ?? "");
      return sort.desc ? -c : c;
    });
  }, [people, team, role, system, query, period, sort, planner]);

  const shown = CHANNELS.filter((c) => (system ? c.system === system : FIRST.has(c.key)) && people.some((p) => (counts(p, period)[c.key] ?? 0) > 0));

  const savedShown = people.some((p) => (thisWeek(p)?.saved_minutes ?? 0) > 0);
  const has = {
    week: period === 28 && people.some((p) => (thisWeek(p)?.done ?? 0) > 0),
    awaiting_reply: people.some((p) => p.awaiting_reply > 0),
    tasks: people.some((p) => tasksOf(p) > 0),
    alerts: people.some((p) => p.alerts + p.tasks > 0),
    reply: people.some((p) => replyOf(p, period) != null),
    trend: people.some((p) => (p.trend ?? []).some((n) => n > 0)),
    hours: people.some((p) => !!busiest(p.hours)),
  };

  const sum = (f: (p: TeamPerson) => number) => rows.reduce((s, p) => s + f(p), 0);
  const replies = rows.map((p) => replyOf(p, period)).filter((n): n is number => n != null).sort((a, b) => a - b);
  const medianReply = replies.length ? replies[Math.floor((replies.length - 1) / 2)] : null;
  const maxOutput = useMemo(() => Math.max(1, ...rows.map((p) => outputMix(p, period).sum)), [rows, period]);

  const activePeople = rows.filter((p) => total(p, period) > 0).length;
  const totalOutputs = sum((p) => total(p, period));
  const totalMail = sum((p) => counts(p, period).mail_sent ?? 0);
  const totalTeams = sum((p) => counts(p, period).teams ?? 0);
  const totalMeetings = sum((p) => counts(p, period).meetings ?? 0);
  const totalDocsErp = Math.max(0, totalOutputs - totalMail - totalTeams - totalMeetings);
  const totalAwaiting = sum((p) => p.awaiting_reply);
  const totalAlerts = sum((p) => p.alerts + p.tasks);
  const totalClosed = sum((p) => p.alerts_closed ?? 0);
  const plannerOpenTotal = planner ? sum(tasksOf) : null;
  const totalBacklog = totalAwaiting + totalAlerts + (plannerOpenTotal ?? 0);

  const topActive = useMemo(
    () =>
      [...rows]
        .filter((p) => total(p, period) > 0)
        .sort((a, b) => total(b, period) - total(a, period) || (a.name ?? "").localeCompare(b.name ?? ""))
        .slice(0, 6),
    [rows, period],
  );
  const topActiveMax = Math.max(1, ...topActive.map((p) => outputMix(p, period).sum));

  const watchlist = useMemo(
    () =>
      [...rows]
        .filter((p) => p.awaiting_reply > 0 || p.alerts + p.tasks > 0)
        .sort(
          (a, b) =>
            b.awaiting_reply - a.awaiting_reply ||
            (b.alerts + b.tasks) - (a.alerts + a.tasks) ||
            (replyOf(b, period) ?? -1) - (replyOf(a, period) ?? -1) ||
            (a.name ?? "").localeCompare(b.name ?? ""),
        )
        .slice(0, 5),
    [rows, period],
  );

  const head = (key: SortKey, label: string, title: string, className?: string) => (
    <th key={key} scope="col" title={title} aria-sort={sort.key === key ? (sort.desc ? "descending" : "ascending") : undefined}
      className={cn("h-9 whitespace-nowrap px-2.5 py-2 font-medium", className)}>
      <button type="button" data-tap="inline" onClick={() => setSort((s) => ({ key, desc: s.key === key ? !s.desc : key !== "name" && key !== "team" }))}
        className={cn("cursor-pointer transition-colors duration-150 hover:text-fg", sort.key === key && "font-semibold text-fg")}>
        {label}{sort.key === key ? (sort.desc ? " ↓" : " ↑") : ""}
      </button>
    </th>
  );

  if (data.state !== "ready") {
    return (
      <div
        className="max-w-xl rounded-xl border border-border/80 bg-bg-subtle p-6 space-y-2"
        data-testid="team-not-ready"
      >
        <div className="flex items-center gap-2 text-[14px] font-semibold text-fg">
          <Clock3 className="size-4 text-muted" aria-hidden />
          <span>{data.state === "blocked" ? "Team counts temporarily unavailable" : "Waiting for nightly snapshot"}</span>
        </div>
        <p className="text-[13.5px] leading-[1.55] text-muted">
          {data.state === "blocked"
            ? "Activity can't be counted right now. Try again in a few minutes."
            : "Activity has not been counted yet. It is counted every night."}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-5" data-testid="team-dashboard">
      {/* Compact Header Bar: Liquid Glass Period Switcher + Team / Role / System Filters + Inline Search */}
      <div className="flex flex-wrap items-center gap-2.5">
        <div
          role="radiogroup"
          aria-label="Period"
          className="liquid-glass-pill inline-flex h-8 items-center rounded-lg p-0.5 text-[12.5px] max-sm:h-10"
        >
          {([7, 28] as Period[]).map((p) => (
            <button
              key={p}
              type="button"
              role="radio"
              aria-checked={period === p}
              onClick={() => setPeriod(p)}
              data-testid={`period-${p}`}
              className={cn(
                "h-full rounded-md px-3 font-medium transition-colors duration-150 cursor-pointer",
                period === p ? "liquid-glass-active" : "text-muted hover:text-fg",
              )}
            >
              {p} days
            </button>
          ))}
        </div>
        {data.scope === "everyone" && (
          <>
            {teams.length > 0 && <Select label="Team" value={team} onChange={setTeam} testId="filter-team" options={[["", "All"], ...teams.map((t) => [t, t] as [string, string]), ["-", "No team"]]} />}
            {roles.length > 0 && <Select label="Role" value={role} onChange={setRole} testId="filter-role" options={[["", "All"], ...roles.map((r) => [r, r] as [string, string])]} />}
            <Select label="System" value={system} onChange={setSystem} testId="filter-system" options={[["", "All"], ...[...new Set(CHANNELS.map((c) => c.system))].map((s) => [s, GROUPS[s].label] as [string, string])]} />
            <label className="relative inline-flex items-center">
              <Search className="pointer-events-none absolute left-2.5 size-3.5 text-faint" strokeWidth={2} aria-hidden />
              <input
                type="search"
                placeholder="Find a person"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape" && query) {
                    e.preventDefault();
                    setQuery("");
                  }
                }}
                aria-label="Find a person"
                className="h-8 w-48 rounded-md border border-border bg-surface pl-8 pr-7 text-[12.5px] text-fg outline-none transition-colors duration-150 hover:border-border-strong focus:border-accent/60 placeholder:text-faint"
              />
              {query && (
                <button
                  type="button"
                  aria-label="Clear search"
                  onClick={() => setQuery("")}
                  className="absolute right-1.5 inline-flex size-5 items-center justify-center rounded-sm text-muted transition-colors duration-150 hover:bg-hover hover:text-fg cursor-pointer"
                >
                  <X className="size-3" aria-hidden />
                </button>
              )}
            </label>
            {(team || role || system || query.trim()) && (
              <button
                type="button"
                data-testid="team-clear-filters"
                onClick={() => {
                  setTeam("");
                  setRole("");
                  setSystem("");
                  setQuery("");
                }}
                className="h-8 rounded-md px-2 text-[12.5px] font-medium text-muted transition-colors duration-150 hover:bg-hover hover:text-fg cursor-pointer"
              >
                Clear filters
              </button>
            )}
          </>
        )}
        <span className="ml-auto truncate text-[12.5px] text-faint" data-testid="team-note">
          {data.as_of && (data.stale_since || isBeforeToday(data.as_of)) ? `Counted ${eastern(data.as_of)}` : ""}
        </span>
      </div>

      {/* Flush Hairline Executive Metric Strip */}
      <dl className="grid grid-cols-2 divide-y divide-border/70 border-y border-border/70 lg:grid-cols-4 lg:divide-x lg:divide-y-0" data-testid="team-totals">
        <div className="py-3.5 pr-4 tabular-nums">
          <dt className="flex items-center justify-between text-[12px] font-medium text-muted">
            People
            <Users className="size-3.5 text-faint" aria-hidden />
          </dt>
          <dd className="mt-1 flex items-baseline gap-2">
            <span className="text-[24px] leading-7 font-semibold tracking-[-0.02em] text-fg">
              {rows.length.toLocaleString("en-US")}
            </span>
            <span className="text-[12px] text-muted">
              {activePeople} active in {period}d
            </span>
          </dd>
          <dd className="mt-0.5 text-[11.5px] text-faint">
            {teams.length > 0 ? `${teams.length} departments represented` : "Individual activity scope"}
          </dd>
        </div>

        <div className="py-3.5 lg:px-5 tabular-nums">
          <dt className="flex items-center justify-between text-[12px] font-medium text-muted">
            <span>Human outputs</span>
            <Activity className="size-3.5 text-accent" aria-hidden />
          </dt>
          <dd className="mt-1 text-[24px] leading-7 font-semibold tracking-[-0.02em] text-fg">
            {totalOutputs.toLocaleString("en-US")}
          </dd>
          <dd className="mt-0.5 flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[11.5px] text-muted">
            <span>Mail {totalMail.toLocaleString("en-US")}</span>
            <span>Teams {totalTeams.toLocaleString("en-US")}</span>
            {totalMeetings > 0 && <span>Meetings {totalMeetings.toLocaleString("en-US")}</span>}
            <span>Ops &amp; Docs {totalDocsErp.toLocaleString("en-US")}</span>
          </dd>
        </div>

        <div
          className="py-3.5 lg:px-5 tabular-nums"
          title="Open replies, Planner tasks, and alerts vs. closed alerts"
        >
          <dt className="flex items-center justify-between text-[12px] font-medium text-muted">
            <span>Backlog health</span>
            <span className="num text-[11.5px] font-normal text-faint">
              {totalClosed} closed
            </span>
          </dt>
          <dd className="mt-1 flex items-baseline gap-2">
            <span className={cn("text-[24px] leading-7 font-semibold tracking-[-0.02em]", totalAwaiting > 15 ? "text-risk" : "text-fg")}>
              {totalBacklog.toLocaleString("en-US")}
            </span>
            <span className="text-[12px] text-muted">open items</span>
          </dd>
          <dd className="mt-0.5 flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[11.5px] text-muted">
            <span>Replies {totalAwaiting.toLocaleString("en-US")}</span>
            <span>
              Planner{" "}
              <span data-testid="team-planner-total" className="font-medium text-fg">
                {planner ? sum(tasksOf).toLocaleString("en-US") : "-"}
              </span>
            </span>
          </dd>
        </div>

        <div className="py-3.5 lg:pl-5 tabular-nums">
          <dt className="flex items-center justify-between text-[12px] font-medium text-muted">
            <span>Median reply time</span>
            <Clock3 className="size-3.5 text-faint" aria-hidden />
          </dt>
          <dd className="mt-1 text-[24px] leading-7 font-semibold tracking-[-0.02em] text-fg">
            {minutes(medianReply) || <span className="text-faint/45" aria-label="none">-</span>}
          </dd>
          <dd className="mt-0.5 text-[11.5px] text-faint">
            {totalMeetings > 0
              ? `${totalMeetings.toLocaleString("en-US")} meetings recorded`
              : "Calendar meetings not connected"}
          </dd>
        </div>
      </dl>

      {/* Asymmetric 2-Column Monitoring Overview */}
      {data.scope === "everyone" && rows.length > 0 && (
        <section
          aria-label="Productivity and monitoring overview"
          data-testid="team-monitoring-panel"
          className="grid grid-cols-1 gap-5 lg:grid-cols-[1.35fr_1fr]"
        >
          {/* Left: Top Output & Channel Mix */}
          <div className="flex flex-col justify-between gap-4 rounded-xl border border-border/80 bg-bg-subtle p-5">
            <div className="space-y-4">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div>
                  <h2 className="text-[14px] font-semibold tracking-[-0.01em] text-fg">
                    Top output and channel mix
                  </h2>
                  <p className="mt-0.5 text-[12px] text-faint">
                    Last {period} days across active contributors
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-3 text-[11.5px] text-muted" data-testid="team-channel-legend">
                  <span className="inline-flex items-center gap-1.5">
                    <span className="h-2.5 w-2.5 rounded-xs bg-accent" aria-hidden />
                    Comms
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    <span className="h-2.5 w-2.5 rounded-xs bg-ok" aria-hidden />
                    Docs &amp; Planner
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    <span className="h-2.5 w-2.5 rounded-xs bg-risk" aria-hidden />
                    ERP &amp; Ops
                  </span>
                </div>
              </div>

              {topActive.length === 0 ? (
                <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border/60 py-5 text-[12.5px] text-muted">
                  <span>No activity counted in the last {period} days yet. Counts refresh in the nightly snapshot.</span>
                  {period === 7 && (
                    <button
                      type="button"
                      onClick={() => setPeriod(28)}
                      className="inline-flex h-7 cursor-pointer items-center rounded-md border border-border bg-bg px-2.5 text-[12px] font-medium text-fg transition-colors duration-150 hover:border-border-strong hover:bg-hover"
                    >
                      View 28 days
                    </button>
                  )}
                </div>
              ) : (
                <ul className="space-y-2" data-testid="team-top-output">
                  {topActive.map((p) => {
                    const val = total(p, period);
                    const mix = outputMix(p, period);
                    const dept = cleanCsv(p.team);
                    return (
                      <li key={p.key}>
                        <button
                          type="button"
                          onClick={() => setOpen(p.key)}
                          onPointerEnter={() => readAhead(p.key)}
                          onPointerLeave={readAheadStop}
                          className="group flex w-full items-center gap-3 rounded-md px-2 py-1.5 text-left transition-colors duration-150 hover:bg-row-on cursor-pointer"
                        >
                          <span
                            className={cn(
                              "inline-flex size-6 shrink-0 items-center justify-center rounded-full text-[10.5px] font-semibold tracking-tight",
                              AVATAR_MONO,
                            )}
                            aria-hidden
                          >
                            {initialsOf(p.name)}
                          </span>
                          <div className="w-36 shrink-0 min-w-0">
                            <span className="block truncate text-[12.5px] font-medium text-fg group-hover:underline">
                              {displayName(p.name)}
                            </span>
                            {dept && (
                              <span className="block truncate text-[11px] text-faint">
                                {dept}
                              </span>
                            )}
                          </div>
                          <div className="min-w-0 flex-1">
                            <OutputBar mix={mix} maxTotal={topActiveMax} thick />
                          </div>
                          <span className="num w-12 shrink-0 text-right text-[13px] font-semibold text-fg">
                            {val.toLocaleString("en-US")}
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          </div>

          {/* Right: Workload & Attention Watchlist */}
          <div className="flex flex-col justify-between gap-4 rounded-xl border border-border/80 bg-bg-subtle p-5">
            <div className="space-y-4">
              <div className="flex items-baseline justify-between gap-2">
                <div>
                  <h2 className="text-[14px] font-semibold tracking-[-0.01em] text-fg">
                    Workload and attention
                  </h2>
                  <p className="mt-0.5 text-[12px] text-faint">
                    Reply speed and open load needing support
                  </p>
                </div>
              </div>

              <dl className="grid grid-cols-3 divide-x divide-border/70 border-y border-border/70 py-2.5 tabular-nums">
                <div className="pr-3">
                  <dt className="text-[11.5px] text-faint">Median reply</dt>
                  <dd className="mt-0.5 text-[16px] font-semibold text-fg">
                    {minutes(medianReply) || <span className="text-faint/45" aria-label="none">-</span>}
                  </dd>
                </div>
                <div className="px-3">
                  <dt className="text-[11.5px] text-faint">Awaiting reply</dt>
                  <dd className={cn("mt-0.5 text-[16px] font-semibold", totalAwaiting > 15 ? "text-risk" : "text-fg")}>
                    {totalAwaiting.toLocaleString("en-US")}
                  </dd>
                </div>
                <div className="pl-3">
                  <dt className="text-[11.5px] text-faint">Needs support</dt>
                  <dd className="mt-0.5 text-[16px] font-semibold text-fg">
                    {watchlist.length.toLocaleString("en-US")}
                  </dd>
                </div>
              </dl>

              {watchlist.length === 0 ? (
                <div className="py-4 text-[12.5px] text-muted" data-testid="team-watchlist-empty">
                  {totalOutputs === 0
                    ? "Waiting for the nightly snapshot to populate open reply and alert queues."
                    : "No open replies or alerts need support in this view."}
                </div>
              ) : (
                <ul className="divide-y divide-border/60" data-testid="team-watchlist">
                  {watchlist.map((p) => {
                    const replyMins = replyOf(p, period);
                    const openItems = p.alerts + p.tasks;
                    return (
                      <li key={p.key}>
                        <button
                          type="button"
                          onClick={() => setOpen(p.key)}
                          onPointerEnter={() => readAhead(p.key)}
                          onPointerLeave={readAheadStop}
                          className="group flex w-full items-center gap-2.5 px-1.5 py-2 text-left transition-colors duration-150 hover:bg-row-on cursor-pointer"
                        >
                          <span
                            className={cn(
                              "inline-flex size-6 shrink-0 items-center justify-center rounded-full text-[10.5px] font-semibold tracking-tight",
                              AVATAR_MONO,
                            )}
                            aria-hidden
                          >
                            {initialsOf(p.name)}
                          </span>
                          <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-fg group-hover:underline">
                            {displayName(p.name)}
                          </span>
                          <span className="num shrink-0 text-[12px] text-muted">
                            {p.awaiting_reply > 0 ? (
                              <span className={cn(p.awaiting_reply > 5 ? "font-semibold text-risk" : "font-medium text-fg")}>
                                {p.awaiting_reply} {p.awaiting_reply === 1 ? "reply" : "replies"}
                              </span>
                            ) : (
                              <span className="text-faint/45" aria-label="none">-</span>
                            )}
                          </span>
                          {openItems > 0 && (
                            <span className="num shrink-0 text-[12px] text-muted">
                              {p.alerts > 0 && p.tasks === 0
                                ? `${p.alerts} ${p.alerts === 1 ? "alert" : "alerts"}`
                                : p.tasks > 0 && p.alerts === 0
                                  ? `${p.tasks} ${p.tasks === 1 ? "task" : "tasks"}`
                                  : `${openItems} open items`}
                            </span>
                          )}
                          <span className="num w-14 shrink-0 text-right text-[12px] text-muted">
                            {minutes(replyMins) || <span className="text-faint/45" aria-label="none">-</span>}
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          </div>
        </section>
      )}

      {/* Full-Width Sortable People Table */}
      <div className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2 text-[12px] text-faint">
          <span>Select any person to open their activity timeline, channels, partners, and open items.</span>
          {data.scope !== "everyone" && (
            <div className="flex items-center gap-3">
              <span className="inline-flex items-center gap-1.5">
                <span className="h-2 w-2.5 rounded-xs bg-accent" aria-hidden />
                Comms
              </span>
              <span className="inline-flex items-center gap-1.5">
                <span className="h-2 w-2.5 rounded-xs bg-ok" aria-hidden />
                Docs &amp; Planner
              </span>
              <span className="inline-flex items-center gap-1.5">
                <span className="h-2 w-2.5 rounded-xs bg-risk" aria-hidden />
                ERP &amp; Ops
              </span>
            </div>
          )}
        </div>

        <div className="overflow-x-auto border-y border-border/70">
          <table className="w-full border-collapse text-[13px]" data-testid="team-table">
            <thead className="sticky top-0 z-10 bg-bg-subtle text-left text-[12px] text-muted">
              <tr className="border-b border-border/70">
                {head("name", "Person", "Name", "min-w-[204px] pl-4")}
                {teams.length > 0 && head("team", "Team", "Team, from the owner's assignment, the directory or the role the work shows")}
                {head("total", "All", "Everything they did in the period except mail received", "text-right")}
                <th scope="col" className="h-9 whitespace-nowrap px-2.5 py-2 font-medium" title="Output mix across Comms, Docs/Planner, and ERP/Ops">
                  Output mix
                </th>
                {has.week && head("week", "Week", "Work done this week so far (Monday on): everything but mail received", "text-right")}
                {savedShown && head("saved", "Saved", "Time the Workbench saved them this week (the work log's estimate)", "text-right")}
                {shown.map((ch) => head(ch.key, ch.head, ch.title, cn("text-right", system === ch.system && "text-fg")))}
                {has.awaiting_reply && head("awaiting_reply", "Replies", "Threads awaiting their reply", "text-right")}
                {has.tasks && head("tasks", "Tasks", "Open Planner tasks assigned to them", "text-right")}
                {has.alerts && head("alerts", "Alerts", "Other open alerts on them, stuck or overdue tasks among them", "text-right")}
                {has.reply && head("reply", "Median", "Median time to reply to a message addressed to them", "text-right")}
                {has.trend && <th scope="col" className="h-9 whitespace-nowrap px-2.5 py-2 font-medium" title="Everything they did per day, last 28 days">28-day trend</th>}
                {has.hours && <th scope="col" className="h-9 whitespace-nowrap px-2.5 py-2 pr-4 font-medium" title="Messages sent per hour of the day, Eastern time, last 28 days">Busiest hours</th>}
              </tr>
            </thead>
            <tbody className="divide-y divide-border/60">
            {rows.map((p) => {
              const c = counts(p, period);
              const mix = outputMix(p, period);
              const pr = formatRoleList(p.roles, p.team);
              return (
                <tr
                  key={p.key}
                  data-testid="team-row"
                  onClick={() => setOpen(p.key)}
                  onPointerEnter={() => readAhead(p.key)}
                  onPointerLeave={readAheadStop}
                  onFocus={() => readAhead(p.key)}
                  className="cursor-pointer transition-colors duration-150 hover:bg-row-on"
                >
                  <td className="py-2.5 pl-4 pr-2">
                    <div className="flex items-center gap-2.5">
                      <span
                        className={cn(
                          "inline-flex size-8 shrink-0 items-center justify-center rounded-full text-[11.5px] font-semibold tracking-tight",
                          AVATAR_MONO,
                        )}
                        aria-hidden
                      >
                        {initialsOf(p.name)}
                      </span>
                      <div className="min-w-0">
                        <div className="flex items-center gap-1.5">
                          <button
                            type="button"
                            data-tap="inline"
                            className="truncate text-left text-[13.5px] font-medium text-fg hover:underline"
                            onClick={(e) => {
                              e.stopPropagation();
                              setOpen(p.key);
                            }}
                          >
                            {displayName(p.name)}
                          </button>
                          {p.self && (
                            <span className="text-[11px] font-medium text-faint">
                              you
                            </span>
                          )}
                        </div>
                        {pr.length > 0 && (
                          <div className="mt-0.5 truncate text-[12px] text-muted" data-testid="team-roles">
                            {pr.join(", ")}
                          </div>
                        )}
                      </div>
                    </div>
                  </td>
                  {teams.length > 0 && (
                    <td className="whitespace-nowrap px-2.5 py-2 text-[12.5px] text-muted">
                      {cleanCsv(p.team) || <span className="text-faint/45" aria-label="none">-</span>}
                    </td>
                  )}
                  <td className="px-2.5 py-2 text-right">
                    <Figure n={total(p, period)} strong />
                  </td>
                  <td className="px-2.5 py-2">
                    <OutputBar mix={mix} maxTotal={maxOutput} />
                  </td>
                  {has.week && (
                    <td
                      className="whitespace-nowrap px-2.5 py-2 text-right"
                      data-testid="team-week"
                      title={lastWeek(p) ? `Last week: ${lastWeek(p)!.done.toLocaleString("en-US")}` : undefined}
                    >
                      <Figure n={thisWeek(p)?.done} />
                    </td>
                  )}
                  {savedShown && (
                    <td className="whitespace-nowrap px-2.5 py-2 text-right num">
                      {thisWeek(p)?.saved_minutes ? minutes(thisWeek(p)!.saved_minutes) : <span className="text-faint/45" aria-label="none">-</span>}
                    </td>
                  )}
                  {shown.map((ch) => (
                    <td key={ch.key} className={cn("px-2.5 py-2 text-right", system === ch.system && "bg-surface-2/50")}>
                      <Figure n={c[ch.key]} />
                    </td>
                  ))}
                  {has.awaiting_reply && (
                    <td className="px-2.5 py-2 text-right">
                      {p.awaiting_reply ? (
                        <span
                          className={cn(
                            "num text-[13px]",
                            p.awaiting_reply > 5 ? "font-semibold text-risk" : "font-medium text-fg",
                          )}
                        >
                          {p.awaiting_reply}
                        </span>
                      ) : (
                        <Figure n={0} />
                      )}
                    </td>
                  )}
                  {has.tasks && (
                    <td className="px-2.5 py-2 text-right" title={planner?.get(p.key)?.overdue ? `${planner.get(p.key)!.overdue} past due` : undefined}>
                      <Figure n={tasksOf(p)} />
                    </td>
                  )}
                  {has.alerts && <td className="px-2.5 py-2 text-right"><Figure n={p.alerts + p.tasks} /></td>}
                  {has.reply && <td className="whitespace-nowrap px-2.5 py-2 text-right num text-fg">{minutes(replyOf(p, period)) || <span className="text-faint/45" aria-label="none">-</span>}</td>}
                  {has.trend && <td className="px-2.5 py-2"><Spark values={p.trend ?? []} /></td>}
                  {has.hours && (
                    <td className="whitespace-nowrap px-2.5 py-2 pr-4" title={busiest(p.hours) ? `Busiest: ${busiest(p.hours)}` : undefined}>
                      <span className="inline-flex flex-col items-start gap-0.5">
                        <Hours hours={p.hours} />
                        <span className="num text-[11px] leading-3 text-muted">{busiest(p.hours)}</span>
                      </span>
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
        {rows.length === 0 && (
          <div className="px-4 py-8 text-center text-[13.5px] text-muted">
            <p>{people.length === 0 ? "No one has activity counted yet." : "Nobody matches these filters."}</p>
            {people.length > 0 && (team || role || system || query.trim()) && (
              <Button
                size="sm"
                variant="secondary"
                className="mt-3"
                onClick={() => {
                  setTeam("");
                  setRole("");
                  setSystem("");
                  setQuery("");
                }}
              >
                Clear filters
              </Button>
            )}
          </div>
        )}
        </div>
      </div>

      {data.scope === "everyone" && accounts.length > 0 && (
        <section
          className="space-y-2 pt-2"
          aria-label="Shared mailboxes and accounts"
          data-testid="team-accounts"
        >
          <div className="border-b border-border pb-2">
            <h2 className="text-[14px] font-semibold text-fg">
              Shared mailboxes and accounts <span className="num font-normal text-muted">{accounts.length}</span>
            </h2>
            <p className="mt-0.5 max-w-[80ch] text-[12.5px] text-muted">
              Named only by their address, with no manager, department or title on record: not counted as people, not ranked.
            </p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-[13px]">
              <thead className="border-b border-border text-left text-[12px] text-muted">
                <tr>
                  <th scope="col" className="px-4 py-2 font-medium">Account</th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">All</th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">Mail sent</th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">Mail received</th>
                  <th scope="col" className="px-4 py-2 text-right font-medium">Teams</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {accounts.map((a) => {
                  const c = counts(a, period);
                  return (
                    <tr
                      key={a.key}
                      className="cursor-pointer transition-colors duration-150 hover:bg-row-on"
                      data-testid="team-account-row"
                      onClick={() => setOpen(a.key)}
                    >
                      <td className="px-4 py-2.5 font-medium text-fg">{a.name}</td>
                      <td className="px-3 py-2.5 text-right"><Figure n={total(a, period)} strong /></td>
                      <td className="px-3 py-2.5 text-right"><Figure n={c.mail_sent} /></td>
                      <td className="px-3 py-2.5 text-right"><Figure n={c.mail_received} /></td>
                      <td className="px-4 py-2.5 text-right"><Figure n={c.teams} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {open && (
        <PersonSheet
          key={open}
          personKey={open}
          fallbackPerson={people.find((p) => p.key === open) ?? accounts.find((a) => a.key === open)}
          onClose={closeSheet}
          onOpen={setOpen}
        />
      )}
    </div>
  );
}

const BUCKETS: { key: TeamItem["bucket"]; label: string }[] = [
  { key: "awaiting_reply", label: "Replies owed to them" },
  { key: "tasks", label: "Open tasks" },
  { key: "alerts", label: "Other alerts" },
];

export function WeekTracker({ weeks, waiting }: { weeks: TeamTrackerWeek[]; waiting: number | { replies: number; tasks: number; alerts: number } }) {
  const saved = weeks.some((w) => w.saved_minutes != null);
  const cell = "px-2.5 py-2 text-right num";
  const parts = typeof waiting === "number" ? null : waiting;
  const total = parts ? parts.replies + parts.tasks + parts.alerts : waiting as number;
  return (
    <div className="space-y-2">
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-[13px]" data-testid="team-weeks">
          <thead className="border-b border-border text-left text-[12px] text-muted">
            <tr>
              <th scope="col" className="py-1.5 pr-2.5 font-medium">Week of</th>
              <th scope="col" className="px-2.5 py-1.5 text-right font-medium" title="Everything they sent or did: mail, Teams, meetings and each system's recorded actions">Done</th>
              <th scope="col" className="px-2.5 py-1.5 text-right font-medium" title="Mail they received from people">Came in</th>
              <th scope="col" className="px-2.5 py-1.5 text-right font-medium" title="Their alerts closed that week">Closed</th>
              <th scope="col" className="px-2.5 py-1.5 text-right font-medium" title="Median time to reply that week, and how many replies it rests on">Median reply</th>
              {saved && <th scope="col" className="py-1.5 pl-2.5 text-right font-medium" title="Time the Workbench saved them: an estimate from the work log">Saved</th>}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {weeks.map((w) => (
              <tr key={w.week} className="transition-colors duration-150 hover:bg-row-on" data-testid="team-week-row">
                <td className="whitespace-nowrap py-2 pr-2.5 text-fg">{weekOf(w.week)}{w.partial && <span className="ml-1.5 text-[11.5px] text-muted">to {weekOf(w.to)}</span>}</td>
                <td className={cn(cell, "font-medium text-fg")}>{w.done.toLocaleString("en-US")}</td>
                <td className={cell}>{w.received.toLocaleString("en-US")}</td>
                <td className={cell}>{w.closed ?? 0}</td>
                <td className={cn(cell, "whitespace-nowrap")}>{w.reply_minutes != null ? <>{minutes(w.reply_minutes)}<span className="ml-1 text-[12px] text-muted">({w.replies})</span></> : <span className="text-faint/45" aria-label="none">-</span>}</td>
                {saved && <td className={cn(cell, "pr-0")}>{w.saved_minutes ? minutes(w.saved_minutes) : <span className="text-faint/45" aria-label="none">-</span>}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-[13px] text-muted" data-testid="team-waiting">{total ? (parts ? `${total.toLocaleString("en-US")} open on them now: ${parts.replies.toLocaleString("en-US")} ${parts.replies === 1 ? "reply" : "replies"} owed to them, ${parts.tasks.toLocaleString("en-US")} ${parts.tasks === 1 ? "task" : "tasks"} and ${parts.alerts.toLocaleString("en-US")} ${parts.alerts === 1 ? "alert" : "alerts"}.` : `${total.toLocaleString("en-US")} waiting on them now (replies, tasks and alerts).`) : "Nothing waiting on them now."}</p>
    </div>
  );
}

function dayLabel(iso: string): string {
  return new Date(`${iso}T12:00:00`).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }).replace("Sept", "Sep");
}

function PersonSheet({
  personKey,
  fallbackPerson,
  onClose,
  onOpen,
}: {
  personKey: string;
  fallbackPerson?: TeamPerson;
  onClose: () => void;
  onOpen: (key: string) => void;
}) {
  const [data, setData] = useState<TeamPersonDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  useEffect(() => {
    const ctl = new AbortController();
    setData(null);
    setError(null);
    getTeamPerson(personKey, ctl.signal).then(setData, (e) => { if (!ctl.signal.aborted) setError(e); });
    return () => ctl.abort();
  }, [personKey, round]);
  const p = data?.person;
  const shownName = p ? displayName(p.name) : fallbackPerson ? displayName(fallbackPerson.name) : "Person";
  const effectiveHours = (p?.hours ?? []).some((n) => n > 0) ? p?.hours : fallbackPerson?.hours;
  const effectiveTrend = (p?.trend ?? []).some((n) => n > 0) ? p?.trend : fallbackPerson?.trend;
  const summaryCountFor = (bucket: TeamItem["bucket"]): number => {
    if (!p) return 0;
    if (bucket === "awaiting_reply") return Math.max(p.awaiting_reply ?? 0, fallbackPerson?.awaiting_reply ?? 0);
    if (bucket === "tasks") return Math.max(p.tasks ?? 0, fallbackPerson?.tasks ?? 0);
    return Math.max(p.alerts ?? 0, fallbackPerson?.alerts ?? 0);
  };
  const roleParts = p ? [cleanCsv(p.team), ...formatRoleList(p.roles, p.team)].filter(Boolean) : [];

  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={shownName}
      className="sm:w-[min(720px,calc(100vw-16px))]"
      bodyClassName="pb-24"
      footer={
        p ? (
          <div className="flex flex-wrap items-center gap-2">
            <Link
              href={`/ask?q=${encodeURIComponent(`What is ${shownName} working on right now?`)}`}
              onClick={onClose}
              className="inline-flex h-8 items-center gap-1.5 rounded-md bg-fg px-3 text-[12.5px] font-medium text-bg transition-opacity duration-150 hover:opacity-90"
            >
              <MessageCircle className="size-3.5" aria-hidden />
              Ask about {shownName}
            </Link>
            <Link
              href={`/meetings?view=follow-ups&person=${encodeURIComponent(p.name ?? shownName)}`}
              onClick={onClose}
              className="inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-[12.5px] font-medium text-muted transition-colors duration-150 hover:bg-hover hover:text-fg"
            >
              <CalendarClock className="size-3.5" aria-hidden />
              Meeting follow-ups
            </Link>
          </div>
        ) : undefined
      }
    >
      {error ? (
        <LoadProblem message={plainError(error)} onRetry={() => setRound((n) => n + 1)} />
      ) : !data || !p ? (
        <Pending label="Reading activity" />
      ) : (
        <div className="space-y-6 pb-16" data-testid="team-person">
          <div className="flex items-center gap-3.5">
            <span
              className={cn(
                "inline-flex size-11 shrink-0 items-center justify-center rounded-full text-[14px] font-semibold tracking-tight",
                AVATAR_MONO,
              )}
              aria-hidden
            >
              {initialsOf(p.name)}
            </span>
            <div>
              <div className="text-[17px] font-semibold tracking-[-0.015em] text-fg">{shownName}</div>
              <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[13px] text-muted">
                {roleParts.length > 0 ? (
                  roleParts.map((part, idx) => (
                    <span key={`${part}-${idx}`} className="inline-flex items-center gap-2">
                      {idx > 0 && <span className="h-3 w-px bg-border" aria-hidden />}
                      <span>{part}</span>
                    </span>
                  ))
                ) : (
                  <span>No team or role on record</span>
                )}
              </div>
            </div>
          </div>

          <section className="border-t border-border pt-5 space-y-3">
            <h3 className="text-[13.5px] font-semibold text-fg" data-testid="team-window">Counts for the last 28 days</h3>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-3.5 text-[13px] sm:grid-cols-4">
              {CHANNELS.filter((c) => (p.d28?.[c.key] ?? 0) > 0).map((c) => (
                <div key={c.key} title={c.title}>
                  <dt className="text-muted">{c.short}</dt>
                  <dd className="num mt-0.5 text-[18px] font-semibold text-fg">{p.d28?.[c.key]}</dd>
                </div>
              ))}
              <div title="Median time to reply, last 28 days">
                <dt className="text-muted">Median reply</dt>
                <dd className="num mt-0.5 text-[18px] font-semibold text-fg">{minutes(p.reply_minutes_28) || "none yet"}</dd>
              </div>
            </dl>
          </section>

          {(p.weeks ?? []).length > 0 && (
            <section className="cv-auto cv-tile border-t border-border pt-5 space-y-3">
              <h3 className="text-[13.5px] font-semibold text-fg">Week by week</h3>
              <WeekTracker weeks={p.weeks ?? []} waiting={{ replies: summaryCountFor("awaiting_reply"), tasks: summaryCountFor("tasks"), alerts: summaryCountFor("alerts") }} />
            </section>
          )}

          <section className="cv-auto cv-tile border-t border-border pt-5 space-y-3">
            <h3 className="text-[13.5px] font-semibold text-fg">Each day, last 28 days</h3>
            <Timeline days={data.timeline ?? []} fallbackTrend={effectiveTrend} />
          </section>

          {BUCKETS.map((b) => {
            const items = (data.items ?? []).filter((it) => it.bucket === b.key);
            const knownCount = Math.max(items.length, summaryCountFor(b.key));
            return (
              <section key={b.key} className="border-t border-border pt-5 space-y-2">
                <h3 className="text-[12.5px] font-medium text-muted">
                  {b.label} <span className="num ml-1">{knownCount}</span>
                </h3>
                {items.length === 0 ? (
                  knownCount > 0 ? (
                    <p className="text-[12.5px] text-muted" data-testid={`team-bucket-scoped-${b.key}`}>
                      Open item details appear when scoped to your view.
                    </p>
                  ) : (
                    <p className="text-[12.5px] text-muted">None open.</p>
                  )
                ) : (
                  <ul className="divide-y divide-border text-[13px]">
                    {items.map((it) => {
                      const metaParts = [
                        it.counterpart,
                        it.channel === "teams" ? "Teams" : it.channel === "mail" ? "Mail" : null,
                        b.key !== "awaiting_reply" && it.waiting_on ? `answered by ${it.waiting_on}` : null,
                      ].filter((v): v is string => Boolean(v));
                      return (
                        <li key={it.id} className="flex items-baseline gap-3 py-2 transition-colors duration-150 hover:bg-row-on" data-testid="team-item">
                          <span className="num w-9 shrink-0 text-right text-muted">{it.age_days == null ? "" : `${it.age_days}d`}</span>
                          <span className="min-w-0 flex-1">
                            <span className="block truncate font-medium text-fg" title={it.title}>{it.title}</span>
                            {metaParts.length > 0 && (
                              <span className="flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[12px] text-muted">
                                {metaParts.map((part, idx) => (
                                  <span key={`${part}-${idx}`} className="truncate">{part}</span>
                                ))}
                              </span>
                            )}
                          </span>
                          {it.record_id && it.record_type && (
                            <Link
                              className="shrink-0 font-medium text-[12.5px] text-fg underline decoration-border-strong underline-offset-2 hover:decoration-fg"
                              href={`/object/${encodeURIComponent(it.record_type)}/${encodeURIComponent(it.record_id)}`}
                              onClick={onClose}
                            >
                              Open
                            </Link>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </section>
            );
          })}
          {data.items_unread && <p className="text-[12.5px] text-risk" data-testid="team-items-unread">Their open items could not be read just now. The counts above stand; open the person again in a moment.</p>}
          {data.withheld > 0 && <p className="text-[12.5px] text-muted" data-testid="team-withheld">{data.withheld} more not shown: marked, or not open to you.</p>}

          <div className="grid gap-6 border-t border-border pt-5 sm:grid-cols-2">
            <section className="min-w-0 space-y-2">
              <h3 className="text-[12.5px] font-medium text-muted">Works most with</h3>
              {(data.partners ?? []).length === 0 ? <p className="text-[12.5px] text-muted">Nobody on record.</p> : (
                <ul className="space-y-1.5 text-[13px]">
                  {(() => {
                    const countOf = (x: { n?: number; count?: number }) => {
                      const raw = x.n ?? x.count ?? 0;
                      return Number.isFinite(raw) ? raw : 0;
                    };
                    const partners = data.partners ?? [];
                    const top = Math.max(1, ...partners.map((x) => countOf(x as { n?: number; count?: number })));
                    return partners.map((x) => {
                      const count = countOf(x as { n?: number; count?: number });
                      const barPx = Math.max(4, Math.round((count / top) * 96));
                      return (
                        <li key={x.name} className="flex min-w-0 items-center gap-2">
                          {x.key ? (
                            <button
                              type="button"
                              data-tap="inline"
                              title={x.name}
                              className="min-w-0 flex-1 max-w-[160px] truncate text-left text-fg hover:underline cursor-pointer"
                              onClick={() => onOpen(x.key!)}
                            >
                              {displayName(x.name)}
                            </button>
                          ) : (
                            <span className="min-w-0 flex-1 max-w-[160px] truncate text-fg" title={x.name}>{displayName(x.name)}</span>
                          )}
                          <span className="h-1.5 shrink-0 rounded-sm bg-accent/75" style={{ width: `${barPx}px` }} />
                          <span className="num shrink-0 text-[12px] text-muted">{count}</span>
                        </li>
                      );
                    });
                  })()}
                </ul>
              )}
            </section>
            <section className="min-w-0 overflow-x-auto space-y-2">
              <h3 className="text-[12.5px] font-medium text-muted">Busiest hours, Eastern</h3>
              <Hours hours={effectiveHours} tall />
              <p className="sr-only">{busiest(effectiveHours) ? `Busiest: ${busiest(effectiveHours)}` : "No messages counted by hour."}</p>
            </section>
          </div>
        </div>
      )}
    </Sheet>
  );
}

function Timeline({ days, fallbackTrend }: { days: { day: string; counts?: Record<string, number> }[]; fallbackTrend?: number[] }) {
  const rows = CHANNELS.filter((c) => days.some((d) => (d.counts?.[c.key] ?? 0) > 0));
  if (rows.length === 0) {
    const trend = fallbackTrend ?? [];
    if (trend.some((n) => n > 0)) {
      const max = Math.max(1, ...trend);
      return (
        <div className="space-y-1.5" data-testid="team-timeline-fallback">
          <div className="flex h-10 items-end gap-1" aria-label="28-day daily activity totals">
            {trend.map((n, i) => (
              <span
                key={i}
                title={`Day ${i + 1}: ${n}`}
                className="flex-1 rounded-[2px]"
                style={{
                  height: `${n > 0 ? Math.max(12, Math.round((n / max) * 100)) : 6}%`,
                  background: n > 0 ? "var(--accent)" : "var(--surface-2)",
                  opacity: n > 0 ? 0.35 + 0.65 * (n / max) : 1,
                }}
              />
            ))}
          </div>
          <p className="text-[12px] text-muted">Daily activity totals across channels.</p>
        </div>
      );
    }
    return <p className="text-[12.5px] text-muted">Nothing counted in these days.</p>;
  }
  return (
    <div className="w-fit max-w-full overflow-x-auto [direction:rtl]" data-testid="team-timeline-scroll">
      <table className="border-collapse text-[12.5px] [direction:ltr]" data-testid="team-timeline">
        <tbody>
          {rows.map((c) => {
            const max = Math.max(1, ...days.map((d) => d.counts?.[c.key] ?? 0));
            return (
              <tr key={c.key}>
                <th scope="row" className="sticky left-0 z-[1] whitespace-nowrap bg-raised pr-3 text-left font-medium text-muted">{c.short}</th>
                {days.map((d) => {
                  const n = d.counts?.[c.key] ?? 0;
                  return (
                    <td key={d.day} className="p-[2px]" title={`${dayLabel(d.day)}: ${n}`}>
                      <div className="size-[13px] rounded-[3px]" style={{ background: n ? GROUPS[c.system].tone : "var(--surface-2)", opacity: n ? 0.30 + 0.70 * (n / max) : 1 }} />
                    </td>
                  );
                })}
              </tr>
            );
          })}
          <tr>
            <td className="sticky left-0 bg-raised" />
            {days.map((d, i) => (
              <td key={d.day} className="pt-1 text-center text-[11px] text-muted num">{i % 7 === 0 || i === days.length - 1 ? new Date(`${d.day}T12:00:00`).getDate() : ""}</td>
            ))}
          </tr>
        </tbody>
      </table>
    </div>
  );
}
