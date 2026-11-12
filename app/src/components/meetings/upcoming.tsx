"use client";

import { Building2, ChevronLeft, ChevronRight, ListChecks, Lock, MapPin, Truck, Users, Video } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { EmptyState } from "@/components/ui/empty-state";
import { Pending } from "@/components/ui/skeleton";
import { LoadProblem } from "@/components/ui/notice";
import { getUpcomingMeetings } from "@/lib/api";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { objectHref, plural, safeHref } from "@/lib/format";
import type { ObjectRef, UpcomingMeeting, UpcomingMeetings } from "@/lib/types";
import {
  addDays,
  byDay,
  clock,
  dayKey,
  dayTitle,
  durationText,
  endOf,
  hourSpan,
  layoutDay,
  minutes,
  mondayOf,
  outsideDomains,
  peopleLine,
  relative,
  startOf,
  type Placed,
  weekBounds,
  whenOf,
} from "./calendar";
import { MeetingSheet } from "./meeting-sheet";

type Layout = "agenda" | "week";

/** The current minute, ticking once a minute so "in 40 min" and the now line stay true. */
function useNow(): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(t);
  }, []);
  return now;
}

/** Meetings, Upcoming: your calendar for two weeks as an agenda by day or a week grid; a
 * meeting opens its detail (people, agenda, related records, carried action items, prep). */
export function Upcoming() {
  const [list, setList] = useState<UpcomingMeetings | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const [layout, setLayout] = useState<Layout>("agenda");
  const [open, setOpen] = useState<UpcomingMeeting | null>(null);
  const now = useNow();
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    getUpcomingMeetings(ctl.signal).then(setList, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [round]);
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!list) return <Pending label="Reading your calendar" />;
  if (list.meetings.length === 0)
    return <EmptyState title="No meetings today or in the next two weeks." line="A customer or supplier page has a Prep button for any meeting." />;
  return (
    <div className="space-y-4" data-testid="upcoming-meetings">
      <Summary meetings={list.meetings} now={now} layout={layout} onLayout={setLayout} />
      {layout === "agenda" ? (
        <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_300px]">
          <Agenda meetings={list.meetings} now={now} onOpen={setOpen} />
          <Rail meetings={list.meetings} now={now} onOpen={setOpen} />
        </div>
      ) : (
        <Week meetings={list.meetings} now={now} onOpen={setOpen} />
      )}
      <MeetingSheet meeting={open} now={now} onClose={() => setOpen(null)} />
    </div>
  );
}

function Summary({ meetings, now, layout, onLayout }: { meetings: UpcomingMeeting[]; now: Date; layout: Layout; onLayout: (l: Layout) => void }) {
  const today = meetings.filter((m) => startOf(m) && dayKey(startOf(m)!) === dayKey(now));
  const left = today.filter((m) => whenOf(m, now) !== "past");
  const outside = meetings.filter((m) => (m.parties ?? []).length > 0 || outsideDomains(m).length > 0);
  const carried = meetings.reduce((n, m) => n + (m.open_items ?? []).length, 0);
  const next = meetings.find((m) => whenOf(m, now) === "now" || whenOf(m, now) === "soon");
  return (
    <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3 border-b border-border pb-3">
      <dl className="flex flex-wrap gap-x-7 gap-y-2 text-[12px]" data-testid="meetings-summary">
        <Fig label="Today" value={`${left.length} of ${today.length}`} sub="still to come" />
        <Fig label="Two weeks" value={String(meetings.length)} sub={meetings.length === 1 ? "meeting" : "meetings"} />
        <Fig label="With customers or suppliers" value={String(outside.length)} />
        <Fig label="Open action items carried" value={String(carried)} tone={carried ? "risk" : undefined} />
        {next && (
          <div className="min-w-0">
            <dt className="text-muted">Next</dt>
            <dd className="max-w-[28ch] truncate text-[13px] font-medium">
              <span className={cn("num mr-1.5", whenOf(next, now) === "now" ? "text-accent" : "text-live")}>{relative(next, now)}</span>
              {next.object.label}
            </dd>
          </div>
        )}
      </dl>
      <div className="flex items-center gap-4 text-[13px]" role="tablist" aria-label="Calendar layout">
        {(["agenda", "week"] as const).map((l) => (
          <button
            key={l}
            type="button"
            role="tab"
            aria-selected={layout === l}
            onClick={() => onLayout(l)}
            className={cn("relative h-7 cursor-pointer capitalize", layout === l ? "font-medium text-fg after:absolute after:inset-x-0 after:-bottom-[13px] after:h-[2px] after:bg-accent" : "text-muted hover:text-fg")}
          >
            {l}
          </button>
        ))}
      </div>
    </div>
  );
}

function Fig({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "risk" }) {
  return (
    <div>
      <dt className="text-muted">{label}</dt>
      <dd className="text-[13px]">
        <span className={cn("num text-[15px] font-semibold", tone === "risk" && "text-risk")}>{value}</span>
        {sub && <span className="ml-1 text-muted">{sub}</span>}
      </dd>
    </div>
  );
}

function Agenda({ meetings, now, onOpen }: { meetings: UpcomingMeeting[]; now: Date; onOpen: (m: UpcomingMeeting) => void }) {
  const days = byDay(meetings);
  return (
    <div className="min-w-0 space-y-5" data-testid="meetings-agenda">
      {days.map((day) => {
        const isToday = day.key === dayKey(now);
        const total = day.meetings.reduce((n, m) => n + (minutes(m) ?? 0), 0);
        return (
          <section key={day.key} aria-label={dayTitle(day.date, now)} data-today={isToday || undefined}>
            <h3 className={cn("sticky top-[52px] z-10 flex items-baseline justify-between border-b bg-bg py-1.5 text-[13px] font-semibold", isToday ? "border-accent text-accent" : "border-border")}>
              <span>{dayTitle(day.date, now)}</span>
              <span className="num text-[12px] font-normal text-muted">
                {day.meetings.length} · {durationText(total) ?? "0 min"}
              </span>
            </h3>
            <ol>
              {day.meetings.map((m) => (
                <AgendaRow key={m.object.object_id} m={m} now={now} onOpen={onOpen} />
              ))}
            </ol>
          </section>
        );
      })}
    </div>
  );
}

const EDGE: Record<string, string> = { now: "before:bg-accent", soon: "before:bg-live", later: "before:bg-border-strong", past: "before:bg-border" };

function AgendaRow({ m, now, onOpen }: { m: UpcomingMeeting; now: Date; onOpen: (m: UpcomingMeeting) => void }) {
  const s = startOf(m);
  const e = endOf(m);
  const when = whenOf(m, now);
  const rel = relative(m, now);
  const { names, more } = peopleLine(m);
  const domains = outsideDomains(m);
  const items = m.open_items ?? [];
  const agenda = m.agenda?.split(/\n+/).map((l) => l.trim()).filter(Boolean) ?? [];
  return (
    <li
      className={cn(
        "group relative grid cursor-pointer grid-cols-[92px_minmax(0,1fr)_auto] gap-x-4 border-b border-border py-2.5 pl-3 text-[13px] hover:bg-hover",
        "before:absolute before:inset-y-2 before:left-0 before:w-[3px]",
        EDGE[when],
        when === "past" && "text-muted",
      )}
      data-testid="meeting-row"
      onClick={() => onOpen(m)}
    >
      <div className="num text-[12px] leading-5">
        <div className={cn("font-medium", when === "past" ? "text-muted" : "text-fg")}>{s ? clock(s) : "—"}</div>
        <div className="text-faint">{m.all_day ? "all day" : e ? clock(e) : null}</div>
        {rel && <div className={cn(when === "now" ? "text-accent" : when === "soon" ? "text-live" : "text-faint")}>{rel}</div>}
      </div>
      <div className="min-w-0 space-y-0.5">
        <button type="button" className="text-left font-medium group-hover:underline cursor-pointer" onClick={(ev) => { ev.stopPropagation(); onOpen(m); }}>
          {m.private ? (
            <span className="inline-flex items-center gap-1.5 text-muted">
              <Lock className="size-3.5" strokeWidth={1.8} aria-hidden />
              Private event
            </span>
          ) : (
            m.object.label
          )}
        </button>
        <p className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[12px] text-muted">
          {names.length > 0 && (
            <span className="inline-flex min-w-0 items-center gap-1">
              <Users className="size-3.5 shrink-0" strokeWidth={1.8} aria-hidden />
              <span className="truncate">
                {names.join(", ")}
                {more > 0 && <span className="num"> +{more}</span>}
              </span>
            </span>
          )}
          {m.location && (
            <span className="inline-flex items-center gap-1">
              <MapPin className="size-3.5" strokeWidth={1.8} aria-hidden />
              {m.location}
            </span>
          )}
          {m.online && (
            <span className="inline-flex items-center gap-1">
              <Video className="size-3.5" strokeWidth={1.8} aria-hidden />
              Teams
            </span>
          )}
          {domains.length > 0 && <span>with {domains.join(", ")}</span>}
          {m.people_withheld && <span>people not shown: the exclusion list is not loaded</span>}
        </p>
        {((m.parties ?? []).length > 0 || items.length > 0) && (
          <p className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[12px]">
            {(m.parties ?? []).map((p) => (
              <PartyLink key={p.object_id} p={p} />
            ))}
            {items.length > 0 && (
              <span className="inline-flex items-center gap-1 text-risk">
                <ListChecks className="size-3.5" strokeWidth={1.8} aria-hidden />
                <span className="num">{items.length}</span> open {plural(items.length, "action item").replace(/^\d+ /, "")} from earlier
              </span>
            )}
          </p>
        )}
        {agenda.length > 0 && when !== "past" && <p className="line-clamp-1 max-w-[90ch] text-[12px] text-faint">{agenda.join(" · ")}</p>}
      </div>
      <div className="flex items-start gap-1 pt-0.5" onClick={(ev) => ev.stopPropagation()}>
        {m.join_url && when !== "past" && (
          <a href={safeHref(m.join_url) ?? undefined} target="_blank" rel="noreferrer" className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-[12px] font-medium text-accent hover:bg-hover">
            <Video className="size-3.5" strokeWidth={1.8} aria-hidden />
            Join
          </a>
        )}
        {!m.private && (
          <Link href={safeHref(m.brief_url) ?? "/meetings"} className="inline-flex h-7 items-center rounded-md px-2 text-[12px] font-medium text-fg hover:bg-hover" data-testid="prep-link">
            Prep
          </Link>
        )}
      </div>
    </li>
  );
}

export function PartyLink({ p }: { p: ObjectRef }) {
  const Icon = p.type === "Supplier" ? Truck : Building2;
  return (
    <Link href={safeHref(p.open_url) ?? objectHref(p.type, p.object_id)} className="inline-flex items-center gap-1 font-medium text-accent hover:underline" onClick={(e) => e.stopPropagation()}>
      <Icon className="size-3.5" strokeWidth={1.8} aria-hidden />
      {p.label}
    </Link>
  );
}

/** Beside the agenda: what needs you before it starts. Outside meetings in the next two days
 * and every carried action item, each naming its meeting. */
function Rail({ meetings, now, onOpen }: { meetings: UpcomingMeeting[]; now: Date; onOpen: (m: UpcomingMeeting) => void }) {
  const soon = addDays(now, 2).getTime();
  const prep = meetings.filter((m) => {
    const s = startOf(m);
    return s && s.getTime() <= soon && whenOf(m, now) !== "past" && (m.parties ?? []).length > 0;
  });
  const items = meetings.flatMap((m) => (m.open_items ?? []).map((i) => ({ i, m })));
  const seen = new Set<string>();
  const unique = items.filter(({ i }) => (seen.has(i.label) ? false : (seen.add(i.label), true)));
  return (
    <aside className="space-y-6 text-[13px] xl:sticky xl:top-[64px] xl:self-start" data-testid="meetings-rail">
      <div>
        <h3 className="mb-1.5 text-[12px] font-semibold tracking-[0.02em] text-muted uppercase">Prepare for</h3>
        {prep.length === 0 ? (
          <p className="text-[12px] text-muted">No customer or supplier meeting in the next two days.</p>
        ) : (
          <ul className="divide-y divide-border">
            {prep.map((m) => (
              <li key={m.object.object_id} className="py-1.5">
                <button type="button" className="text-left font-medium hover:underline cursor-pointer" onClick={() => onOpen(m)}>
                  {m.object.label}
                </button>
                <p className="num text-[12px] text-muted">
                  {startOf(m) ? `${dayTitle(startOf(m)!, now).split(" · ")[0]} ${clock(startOf(m)!)}` : null}
                </p>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <h3 className="mb-1.5 text-[12px] font-semibold tracking-[0.02em] text-muted uppercase">Still open from earlier meetings</h3>
        {unique.length === 0 ? (
          <p className="text-[12px] text-muted">Nothing carried over.</p>
        ) : (
          <ul className="divide-y divide-border" data-testid="carried-items">
            {unique.map(({ i, m }) => (
              <li key={i.label} className="py-1.5">
                <p>{i.label.replace(/^Action item: /, "")}</p>
                <p className="text-[12px] text-muted">
                  {i.detail}
                  {" · "}
                  <button type="button" className="text-left hover:underline cursor-pointer" onClick={() => onOpen(m)}>
                    {m.object.label}
                  </button>
                </p>
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}

const HOUR_PX = 48;

/** One week as a grid: a column per day, a block per meeting at its time (side by side when
 * they overlap), all-day events in a row above, the hours widened to fit every meeting,
 * today's column and the current time drawn in blue. */
export function Week({ meetings, now, onOpen }: { meetings: UpcomingMeeting[]; now: Date; onOpen: (m: UpcomingMeeting) => void }) {
  const [offset, setOffset] = useState(0);
  const bounds = useMemo(() => weekBounds(meetings, now), [meetings, now]);
  const monday = useMemo(() => addDays(mondayOf(now), 7 * offset), [now, offset]);
  const days = Array.from({ length: 7 }, (_, i) => addDays(monday, i));
  const byKey = new Map(byDay(meetings).map((d) => [d.key, d.meetings]));
  const inWeek = days.flatMap((d) => byKey.get(dayKey(d)) ?? []);
  const { first, last } = hourSpan(inWeek);
  const hours = Array.from({ length: last - first }, (_, i) => first + i);
  const nowTop = ((now.getHours() - first) * 60 + now.getMinutes()) * (HOUR_PX / 60);
  const anyAllDay = inWeek.some((m) => m.all_day);
  return (
    <div className="min-w-0 space-y-2" data-testid="meetings-week">
      <div className="flex items-center gap-1 text-[13px]">
        <button type="button" aria-label="Earlier week" className="flex size-7 items-center justify-center rounded-md hover:bg-hover disabled:opacity-40 cursor-pointer" disabled={offset <= bounds.min} onClick={() => setOffset((o) => o - 1)}>
          <ChevronLeft className="size-4" aria-hidden />
        </button>
        <span className="min-w-[11rem] text-center font-medium">
          Week of {monday.toLocaleDateString("en-CA", { month: "short", day: "numeric" })}
        </span>
        <button type="button" aria-label="Later week" className="flex size-7 items-center justify-center rounded-md hover:bg-hover disabled:opacity-40 cursor-pointer" disabled={offset >= bounds.max} onClick={() => setOffset((o) => o + 1)}>
          <ChevronRight className="size-4" aria-hidden />
        </button>
      </div>
      <div className="overflow-x-auto">
        <div className="grid min-w-[880px] grid-cols-[52px_repeat(7,minmax(0,1fr))] border-t border-l border-border">
          <div className="border-r border-b border-border" />
          {days.map((d) => {
            const today = dayKey(d) === dayKey(now);
            return (
              <div key={dayKey(d)} className={cn("border-r border-b border-border px-2 py-1.5 text-[12px]", today && "bg-accent-soft")}>
                <span className={cn("font-medium", today ? "text-accent" : "text-muted")}>{d.toLocaleDateString("en-CA", { weekday: "short" })}</span>{" "}
                <span className={cn("num", today ? "font-semibold text-accent" : "text-fg")}>{d.getDate()}</span>
              </div>
            );
          })}
          {anyAllDay && (
            <>
              <div className="border-r border-b border-border px-1 py-1 text-right text-[11px] text-faint">All day</div>
              {days.map((d) => (
                <div key={dayKey(d)} className="space-y-0.5 border-r border-b border-border p-1" data-testid="week-all-day">
                  {(byKey.get(dayKey(d)) ?? []).filter((m) => m.all_day).map((m) => (
                    <button key={m.object.object_id} type="button" onClick={() => onOpen(m)} className="block w-full truncate rounded-[3px] border-l-[3px] border-accent bg-raised px-1.5 py-0.5 text-left text-[11px] leading-[15px] shadow-[0_0_0_1px_var(--border)] hover:bg-hover cursor-pointer" title={m.private ? "Private event" : m.object.label} data-testid="week-block">
                      {m.private ? "Private event" : m.object.label}
                    </button>
                  ))}
                </div>
              ))}
            </>
          )}
          <div className="relative border-r border-border" style={{ height: hours.length * HOUR_PX }}>
            {hours.map((h, i) => (
              <span key={h} className="num absolute right-1.5 text-[11px] text-faint" style={{ top: i * HOUR_PX - 7 }}>
                {i === 0 ? "" : clock(new Date(2000, 0, 1, h)).replace(":00", "")}
              </span>
            ))}
          </div>
          {days.map((d) => {
            const key = dayKey(d);
            const today = key === dayKey(now);
            const placed = layoutDay(byKey.get(key) ?? []);
            return (
              <div key={key} className={cn("relative border-r border-border", today && "bg-[color-mix(in_srgb,var(--accent-soft)_45%,transparent)]")} style={{ height: hours.length * HOUR_PX }}>
                {hours.map((h, i) => (
                  <div key={h} className="absolute inset-x-0 border-t border-border/70" style={{ top: i * HOUR_PX }} />
                ))}
                {today && nowTop > 0 && nowTop < hours.length * HOUR_PX && (
                  <div className="absolute inset-x-0 z-10 h-[2px] bg-accent" style={{ top: nowTop }} aria-label="Now" />
                )}
                {placed.map((p) => (
                  <WeekBlock key={p.m.object.object_id} p={p} first={first} last={last} now={now} onOpen={onOpen} />
                ))}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function WeekBlock({ p, first, last, now, onOpen }: { p: Placed; first: number; last: number; now: Date; onOpen: (m: UpcomingMeeting) => void }) {
  const m = p.m;
  const s = startOf(m)!;
  const mins = minutes(m) ?? 30;
  const top = ((s.getHours() - first) * 60 + s.getMinutes()) * (HOUR_PX / 60);
  const height = Math.max(20, Math.min(mins * (HOUR_PX / 60), (last - first) * HOUR_PX - top) - 2);
  const when = whenOf(m, now);
  const outside = (m.parties ?? []).length > 0;
  const width = 100 / p.cols;
  return (
    <button
      type="button"
      onClick={() => onOpen(m)}
      className={cn(
        "absolute overflow-hidden rounded-[3px] border-l-[3px] bg-raised px-1.5 py-0.5 text-left text-[11px] leading-[15px] shadow-[0_0_0_1px_var(--border)] hover:bg-hover cursor-pointer",
        when === "past" ? "border-border-strong text-muted" : outside ? "border-accent" : "border-live",
        height < 34 && "truncate whitespace-nowrap",
      )}
      style={{ top, height, left: `calc(${p.col * width}% + 2px)`, width: `calc(${width}% - 4px)` }}
      title={m.private ? "Private event" : m.object.label}
      data-testid="week-block"
    >
      <span className="num text-muted">{clock(s)}</span> <span className="font-medium">{m.private ? "Private event" : m.object.label}</span>
      {height > 34 && m.location && <span className="block truncate text-muted">{m.location}</span>}
    </button>
  );
}
