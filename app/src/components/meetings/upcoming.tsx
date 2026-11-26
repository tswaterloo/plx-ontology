"use client";

import { Building2, Calendar, ChevronLeft, ChevronRight, ListChecks, Lock, MapPin, Search, Truck, Users, Video, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useRead } from "@/lib/use-read";
import { StoredAge } from "@/components/ui/stored-age";
import { Button } from "@/components/ui/button";
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
  cleanMeetingAgenda,
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
type MeetingFilter = "all" | "external" | "carried";

function useNow(): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(t);
  }, []);
  return now;
}

export function Upcoming() {
  const { data: list, error, retry, at } = useRead<UpcomingMeetings>("/api/brief/meetings", (s) => getUpcomingMeetings(s));
  const [layout, setLayout] = useState<Layout>("agenda");
  const [filter, setFilter] = useState<MeetingFilter>("all");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<UpcomingMeeting | null>(null);
  const now = useNow();

  const meetings = list?.meetings ?? [];
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return meetings.filter((m) => {
      if (filter === "external" && (m.parties ?? []).length === 0 && outsideDomains(m).length === 0) return false;
      if (filter === "carried" && (m.open_items ?? []).length === 0) return false;
      if (!q) return true;
      const hay = [
        m.private ? "private event" : m.object.label,
        m.location ?? "",
        m.organizer?.name ?? "",
        ...(m.attendees ?? []).map((a) => `${a.name} ${a.address ?? ""}`),
        ...(m.parties ?? []).map((p) => p.label),
      ]
        .join(" ")
        .toLowerCase();
      return hay.includes(q);
    });
  }, [meetings, filter, query]);

  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!list) return <Pending label="Reading your calendar" />;
  if (meetings.length === 0) {
    return <EmptyState title="No meetings today or in the next two weeks." line="A customer or supplier page has a Prep button for any meeting." />;
  }

  const hasFilter = filter !== "all" || query.trim().length > 0;
  const externalCount = meetings.filter((m) => (m.parties ?? []).length > 0 || outsideDomains(m).length > 0).length;
  const carriedCount = meetings.filter((m) => (m.open_items ?? []).length > 0).length;

  return (
    <div className="space-y-5" data-testid="upcoming-meetings">
      <StoredAge at={at} />
      <Summary meetings={meetings} now={now} onOpen={setOpen} />

      <div className="flex flex-wrap items-center justify-between gap-2.5" data-testid="meetings-filters">
        <div role="group" aria-label="Filter meetings" className="inline-flex flex-wrap items-center gap-1">
          {(
            [
              ["all", "All", meetings.length],
              ...(externalCount > 0 ? [["external", "External", externalCount] as const] : []),
              ...(carriedCount > 0 ? [["carried", "With action items", carriedCount] as const] : []),
            ] as const
          ).map(([key, label, count]) => {
            const active = filter === key;
            return (
              <button
                key={key}
                type="button"
                aria-pressed={active}
                onClick={() => setFilter(key)}
                className={cn(
                  "inline-flex h-8 items-center gap-1.5 rounded-md px-2.5 text-[12.5px] transition-colors duration-150 active:scale-[0.98] cursor-pointer",
                  active ? "bg-surface-2 font-medium text-fg" : "text-muted hover:bg-hover hover:text-fg",
                )}
              >
                {label}
                <span className="num text-[11.5px] text-muted">{count}</span>
              </button>
            );
          })}
        </div>

        <div className="flex flex-wrap items-center gap-2 max-sm:w-full">
          <label className="relative inline-flex items-center max-sm:flex-1">
            <Search className="pointer-events-none absolute left-2.5 size-3.5 text-muted" strokeWidth={2} aria-hidden />
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape" && query) {
                  e.preventDefault();
                  setQuery("");
                }
              }}
              placeholder="Filter meetings or people"
              aria-label="Filter meetings or people"
              className="h-8 w-56 max-w-full rounded-md border border-border bg-surface pl-8 pr-7 text-[12.5px] text-fg outline-none transition-colors hover:border-border-strong focus:border-border-strong focus-visible:ring-2 focus-visible:ring-ring/40 placeholder:text-muted max-sm:w-full"
            />
            {query && (
              <button
                type="button"
                aria-label="Clear meeting filter"
                onClick={() => setQuery("")}
                className="absolute right-1.5 inline-flex size-5 items-center justify-center rounded-sm text-muted hover:bg-hover hover:text-fg active:scale-[0.95] cursor-pointer"
              >
                <X className="size-3" aria-hidden />
              </button>
            )}
          </label>
          {hasFilter && (
            <button
              type="button"
              onClick={() => {
                setFilter("all");
                setQuery("");
              }}
              className="h-8 rounded-md px-2 text-[12.5px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg active:scale-[0.98] cursor-pointer"
            >
              Clear
            </button>
          )}
          <div className="inline-flex h-8 items-center rounded-md border border-border bg-surface p-0.5 text-[12.5px]" role="tablist" aria-label="Calendar layout">
            {(["agenda", "week"] as const).map((l) => (
              <button
                key={l}
                type="button"
                role="tab"
                aria-selected={layout === l}
                onClick={() => setLayout(l)}
                className={cn(
                  "h-full cursor-pointer rounded-[4px] px-2.5 capitalize font-medium transition-[background-color,color,transform] duration-150 active:scale-[0.98]",
                  layout === l ? "bg-fg text-bg" : "text-muted hover:text-fg",
                )}
              >
                {l}
              </button>
            ))}
          </div>
        </div>
      </div>

      {filtered.length === 0 ? (
        <div className="py-10 text-center text-[13.5px] text-muted">
          <p>No meetings match these filters.</p>
          <Button
            size="sm"
            variant="secondary"
            className="mt-3"
            onClick={() => {
              setFilter("all");
              setQuery("");
            }}
          >
            Clear filters
          </Button>
        </div>
      ) : layout === "agenda" ? (
        <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
          <Agenda meetings={filtered} now={now} onOpen={setOpen} />
          <Rail meetings={filtered} now={now} onOpen={setOpen} />
        </div>
      ) : (
        <Week meetings={filtered} now={now} onOpen={setOpen} />
      )}
      <MeetingSheet meeting={open} now={now} onClose={() => setOpen(null)} />
    </div>
  );
}

function Summary({
  meetings,
  now,
  onOpen,
}: {
  meetings: UpcomingMeeting[];
  now: Date;
  onOpen: (m: UpcomingMeeting) => void;
}) {
  const today = meetings.filter((m) => startOf(m) && dayKey(startOf(m)!) === dayKey(now));
  const left = today.filter((m) => whenOf(m, now) !== "past");
  const outside = meetings.filter((m) => (m.parties ?? []).length > 0 || outsideDomains(m).length > 0);
  const carried = meetings.reduce((n, m) => n + (m.open_items ?? []).length, 0);
  const next = meetings.find((m) => whenOf(m, now) === "now" || whenOf(m, now) === "soon");
  return (
    <dl
      className="grid grid-cols-2 border-y border-border divide-y divide-border sm:grid-cols-4 sm:divide-x sm:divide-y-0"
      data-testid="meetings-summary"
    >
      {today.length > 0 && <Fig label="Today" value={`${left.length} of ${today.length}`} sub="still to come" />}
      <Fig label="Two weeks" value={String(meetings.length)} sub={meetings.length === 1 ? "meeting" : "meetings"} />
      {outside.length > 0 && <Fig label="With customers or suppliers" value={String(outside.length)} />}
      {carried > 0 && <Fig label="Open action items carried" value={String(carried)} tone="risk" />}
      {next && (
        <div className="min-w-0 px-4 py-3 sm:col-span-4 sm:border-t sm:border-border sm:px-4">
          <dt className="text-[12px] font-medium text-muted">Next up</dt>
          <dd className="mt-1 truncate text-[13.5px] font-medium">
            <button
              type="button"
              onClick={() => onOpen(next)}
              className="max-w-full truncate text-left text-fg hover:text-accent hover:underline cursor-pointer"
              title={next.private ? "Private event" : next.object.label}
            >
              <span className={cn("num mr-1.5 font-semibold", whenOf(next, now) === "now" ? "text-accent" : "text-live")}>
                {relative(next, now)}
              </span>
              {next.private ? "Private event" : next.object.label}
            </button>
          </dd>
        </div>
      )}
    </dl>
  );
}

function Fig({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "risk" }) {
  return (
    <div className="px-4 py-3 first:pl-0 tabular-nums">
      <dt className="truncate text-[12px] font-medium text-muted">{label}</dt>
      <dd className="mt-1 flex items-baseline gap-1.5">
        <span className={cn("num text-[22px] leading-6 font-semibold tracking-[-0.02em]", tone === "risk" ? "text-risk" : "text-fg")}>{value}</span>
        {sub && <span className="text-[12.5px] text-muted">{sub}</span>}
      </dd>
    </div>
  );
}

function Agenda({ meetings, now, onOpen }: { meetings: UpcomingMeeting[]; now: Date; onOpen: (m: UpcomingMeeting) => void }) {
  const days = byDay(meetings);
  return (
    <div className="min-w-0 space-y-6" data-testid="meetings-agenda">
      {days.map((day) => {
        const isToday = day.key === dayKey(now);
        const total = day.meetings.reduce((n, m) => n + (minutes(m) ?? 0), 0);
        return (
          <section
            key={day.key}
            aria-label={dayTitle(day.date, now)}
            data-today={isToday || undefined}
          >
            <h3 className="flex items-baseline justify-between border-b border-border pb-2 text-[13.5px] font-semibold text-fg">
              <span className="inline-flex items-center gap-2">
                {isToday && <Calendar className="size-3.5 shrink-0 text-accent" strokeWidth={2} aria-hidden />}
                {dayTitle(day.date, now)}
              </span>
              <span className="num text-[12.5px] font-normal text-muted">
                {day.meetings.length} · {durationText(total) ?? "0 min"}
              </span>
            </h3>
            <ol className="divide-y divide-border/70 border-b border-border">
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

const EDGE: Record<string, string> = { now: "before:bg-accent", soon: "before:bg-live", later: "before:bg-transparent", past: "before:bg-transparent" };

function AgendaRow({ m, now, onOpen }: { m: UpcomingMeeting; now: Date; onOpen: (m: UpcomingMeeting) => void }) {
  const s = startOf(m);
  const e = endOf(m);
  const when = whenOf(m, now);
  const rel = relative(m, now);
  const { names, more } = peopleLine(m);
  const domains = outsideDomains(m);
  const items = m.open_items ?? [];
  const agenda = cleanMeetingAgenda(m.agenda);
  const imminent = when === "now" || when === "soon";
  return (
    <li
      className={cn(
        "group relative grid cursor-pointer grid-cols-[68px_minmax(0,1fr)] gap-x-3 px-4 py-3 text-[13.5px] transition-colors duration-150 hover:bg-row-on sm:grid-cols-[96px_minmax(0,1fr)_auto] sm:gap-x-4",
        "before:absolute before:inset-y-2.5 before:left-0 before:w-[2px]",
        EDGE[when],
        when === "past" && "opacity-75",
      )}
      data-testid="meeting-row"
      onClick={() => onOpen(m)}
    >
      <div className="num text-[12.5px] leading-5">
        <div className={cn("font-medium", when === "past" ? "text-muted" : "text-fg")}>{s ? clock(s) : "-"}</div>
        <div className="text-muted">{m.all_day ? "all day" : e ? clock(e) : null}</div>
        {rel && (
          <div className={cn("mt-0.5 text-[11.5px] font-medium", when === "now" ? "text-accent" : when === "soon" ? "text-live" : "text-muted")}>
            {rel}
          </div>
        )}
      </div>
      <div className="min-w-0 space-y-1">
        <button
          type="button"
          className="text-left text-[14px] font-medium text-fg group-hover:underline cursor-pointer"
          onClick={(ev) => {
            ev.stopPropagation();
            onOpen(m);
          }}
        >
          {m.private ? (
            <span className="inline-flex items-center gap-1.5 text-muted">
              <Lock className="size-3.5" strokeWidth={1.8} aria-hidden />
              Private event
            </span>
          ) : (
            m.object.label
          )}
        </button>
        <p className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[12.5px] text-muted">
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
          <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12.5px]">
            {(m.parties ?? []).map((p) => (
              <PartyLink key={p.object_id} p={p} />
            ))}
            {items.length > 0 && (
              <span className="inline-flex items-center gap-1.5 font-medium text-risk">
                <ListChecks className="size-3.5 shrink-0 text-risk" strokeWidth={2} aria-hidden />
                <span><span className="num">{items.length}</span> open {plural(items.length, "action item").replace(/^\d+ /, "")} from earlier</span>
              </span>
            )}
          </p>
        )}
        {agenda.length > 0 && when !== "past" && <p className="line-clamp-1 max-w-[90ch] text-[12.5px] text-muted">{agenda.join(" · ")}</p>}
      </div>
      <div className="col-start-2 flex items-start gap-1 pt-0.5 sm:col-start-auto" onClick={(ev) => ev.stopPropagation()}>
        {m.join_url && when !== "past" && (
          <a
            href={safeHref(m.join_url) ?? undefined}
            target="_blank"
            rel="noreferrer"
            className={cn(
              "inline-flex h-7 items-center gap-1 rounded-md px-2.5 text-[12.5px] font-medium transition-[background-color,opacity,transform] duration-150 active:scale-[0.98]",
              imminent ? "bg-fg text-bg hover:opacity-90" : "text-accent hover:bg-hover",
            )}
          >
            <Video className="size-3.5" strokeWidth={1.8} aria-hidden />
            Join
          </a>
        )}
        {!m.private && (
          <Link
            href={safeHref(m.brief_url) ?? "/meetings"}
            className="inline-flex h-7 items-center rounded-md px-2 text-[12.5px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg"
            data-testid="prep-link"
          >
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
    <aside className="space-y-6 text-[13.5px] xl:sticky xl:top-[64px] xl:self-start xl:border-l xl:border-border xl:pl-6" data-testid="meetings-rail">
      <div>
        <div className="border-b border-border pb-2">
          <h3 className="text-[12px] font-semibold text-fg">Prepare for</h3>
        </div>
        {prep.length === 0 ? (
          <p className="py-3 text-[12.5px] text-muted">No customer or supplier meeting in the next two days.</p>
        ) : (
          <ul className="divide-y divide-border/70">
            {prep.map((m) => (
              <li key={m.object.object_id} className="py-2.5 transition-colors duration-150 hover:bg-row-on">
                <button type="button" className="text-left font-medium text-fg hover:text-accent hover:underline cursor-pointer" onClick={() => onOpen(m)}>
                  {m.object.label}
                </button>
                <p className="num mt-0.5 text-[12.5px] text-muted">
                  {startOf(m) ? `${dayTitle(startOf(m)!, now).split(" · ")[0]} ${clock(startOf(m)!)}` : null}
                </p>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <div className="border-b border-border pb-2">
          <h3 className="text-[12px] font-semibold text-fg">Still open from earlier meetings</h3>
        </div>
        {unique.length === 0 ? (
          <p className="py-3 text-[12.5px] text-muted">Nothing carried over.</p>
        ) : (
          <ul className="divide-y divide-border/70" data-testid="carried-items">
            {unique.map(({ i, m }) => (
              <li key={i.label} className="py-2.5 transition-colors duration-150 hover:bg-row-on">
                <p className="text-fg">{i.label.replace(/^Action item: /, "")}</p>
                <p className="mt-0.5 text-[12.5px] text-muted">
                  {i.detail}
                  {" · "}
                  <button type="button" className="text-left hover:text-fg hover:underline cursor-pointer" onClick={() => onOpen(m)}>
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
    <div className="min-w-0 space-y-3" data-testid="meetings-week">
      <div className="flex items-center gap-1.5 text-[13.5px]">
        <button
          type="button"
          aria-label="Earlier week"
          className="flex size-8 items-center justify-center rounded-md border border-border bg-surface text-fg transition-[background-color,border-color,transform] duration-150 hover:border-border-strong hover:bg-surface-2 active:scale-[0.98] disabled:pointer-events-none disabled:opacity-40 cursor-pointer"
          disabled={offset <= bounds.min}
          onClick={() => setOffset((o) => o - 1)}
        >
          <ChevronLeft className="size-4" aria-hidden />
        </button>
        <span className="min-w-[11rem] text-center font-semibold text-fg tabular-nums">
          Week of {monday.toLocaleDateString("en-GB", { day: "numeric", month: "short" }).replace("Sept", "Sep")}
        </span>
        <button
          type="button"
          aria-label="Later week"
          className="flex size-8 items-center justify-center rounded-md border border-border bg-surface text-fg transition-[background-color,border-color,transform] duration-150 hover:border-border-strong hover:bg-surface-2 active:scale-[0.98] disabled:pointer-events-none disabled:opacity-40 cursor-pointer"
          disabled={offset >= bounds.max}
          onClick={() => setOffset((o) => o + 1)}
        >
          <ChevronRight className="size-4" aria-hidden />
        </button>
        {offset !== 0 && (
          <button
            type="button"
            onClick={() => setOffset(0)}
            className="ml-1 inline-flex h-8 items-center rounded-md px-2.5 text-[12.5px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg active:scale-[0.98] cursor-pointer"
          >
            This week
          </button>
        )}
      </div>
      <div className="overflow-x-auto rounded-xl border border-border bg-surface">
        <div className="grid min-w-[880px] grid-cols-[56px_repeat(7,minmax(0,1fr))]">
          <div className="border-r border-b border-border" />
          {days.map((d) => {
            const today = dayKey(d) === dayKey(now);
            return (
              <div key={dayKey(d)} className={cn("border-r border-b border-border px-2.5 py-2 text-[12.5px] last:border-r-0", today && "bg-surface-2/60")}>
                <span className={cn("font-medium", today ? "text-fg" : "text-muted")}>{d.toLocaleDateString("en-CA", { weekday: "short" })}</span>{" "}
                <span className={cn("num", today ? "font-semibold text-accent" : "text-fg")}>{d.getDate()}</span>
              </div>
            );
          })}
          {anyAllDay && (
            <>
              <div className="border-r border-b border-border px-1.5 py-1.5 text-right text-[11px] text-muted">All day</div>
              {days.map((d) => (
                <div key={dayKey(d)} className="space-y-0.5 border-r border-b border-border p-1 last:border-r-0" data-testid="week-all-day">
                  {(byKey.get(dayKey(d)) ?? []).filter((m) => m.all_day).map((m) => (
                    <button
                      key={m.object.object_id}
                      type="button"
                      onClick={() => onOpen(m)}
                      className="block w-full truncate rounded-[4px] border-l-[3px] border-accent bg-raised px-1.5 py-0.5 text-left text-[11px] leading-[15px] shadow-[0_0_0_1px_var(--border)] hover:bg-row-on cursor-pointer"
                      title={m.private ? "Private event" : m.object.label}
                      data-testid="week-block"
                    >
                      {m.private ? "Private event" : m.object.label}
                    </button>
                  ))}
                </div>
              ))}
            </>
          )}
          <div className="relative border-r border-border" style={{ height: hours.length * HOUR_PX }}>
            {hours.map((h, i) => (
              <span key={h} className="num absolute right-2 text-[11px] text-muted" style={{ top: i * HOUR_PX - 7 }}>
                {i === 0 ? "" : clock(new Date(2000, 0, 1, h)).replace(":00", "")}
              </span>
            ))}
          </div>
          {days.map((d) => {
            const key = dayKey(d);
            const today = key === dayKey(now);
            const placed = layoutDay(byKey.get(key) ?? []);
            return (
              <div key={key} className={cn("relative border-r border-border last:border-r-0", today && "bg-surface-2/25")} style={{ height: hours.length * HOUR_PX }}>
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
        "absolute overflow-hidden rounded-[4px] border-l-[3px] bg-raised px-1.5 py-0.5 text-left text-[11px] leading-[15px] shadow-[0_0_0_1px_var(--border)] transition-colors hover:bg-row-on cursor-pointer",
        when === "past" ? "border-border-strong text-muted" : outside ? "border-accent" : "border-live",
        height < 34 && "truncate whitespace-nowrap",
      )}
      style={{ top, height, left: `calc(${p.col * width}% + 2px)`, width: `calc(${width}% - 4px)` }}
      title={m.private ? "Private event" : m.object.label}
      data-testid="week-block"
    >
      <span className="num text-muted">{clock(s)}</span> <span className="font-medium text-fg">{m.private ? "Private event" : m.object.label}</span>
      {height > 34 && m.location && <span className="block truncate text-muted">{m.location}</span>}
    </button>
  );
}
