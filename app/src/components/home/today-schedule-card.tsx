"use client";

import { ArrowUpRight, Calendar, ChevronRight, Lock, MapPin, RefreshCw, Video } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import {
  addDays,
  byDay,
  clock,
  dayKey,
  relative,
  startOf,
  whenOf,
} from "@/components/meetings/calendar";
import { getUpcomingMeetings } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { safeHref } from "@/lib/format";
import type { MeetingInvitee, UpcomingMeeting, UpcomingMeetings } from "@/lib/types";
import { useRead } from "@/lib/use-read";

export interface ScheduleSummary {
  todayCount: number;
  nextTime: string | null;
  loading: boolean;
}

// Calibrated muted avatar tones (non-purple, WCAG legible in both light and dark themes).
const AVATAR_TONES = [
  "bg-sky-500/15 text-sky-800 border-sky-500/25 dark:bg-sky-400/15 dark:text-sky-300 dark:border-sky-400/25",
  "bg-teal-500/15 text-teal-800 border-teal-500/25 dark:bg-teal-400/15 dark:text-teal-300 dark:border-teal-400/25",
  "bg-amber-500/15 text-amber-800 border-amber-500/25 dark:bg-amber-400/15 dark:text-amber-300 dark:border-amber-400/25",
  "bg-emerald-500/15 text-emerald-800 border-emerald-500/25 dark:bg-emerald-400/15 dark:text-emerald-300 dark:border-emerald-400/25",
  "bg-rose-500/15 text-rose-800 border-rose-500/25 dark:bg-rose-400/15 dark:text-rose-300 dark:border-rose-400/25",
  "bg-stone-500/15 text-stone-700 border-stone-500/25 dark:bg-stone-400/15 dark:text-stone-300 dark:border-stone-400/25",
];

function toneForName(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) {
    hash = (hash * 31 + name.charCodeAt(i)) | 0;
  }
  return AVATAR_TONES[Math.abs(hash) % AVATAR_TONES.length];
}

function initialOf(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return "?";
  return trimmed[0].toUpperCase();
}

function inviteesOf(meeting: UpcomingMeeting): MeetingInvitee[] {
  const seen = new Set<string>();
  const out: MeetingInvitee[] = [];
  for (const person of [meeting.organizer, ...(meeting.attendees ?? [])]) {
    if (!person || person.role === "resource" || !person.name) continue;
    const key = (person.address ?? person.name).toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(person);
  }
  return out;
}

function shortHeaderDate(d: Date): string {
  return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
}

function shortDayLabel(d: Date, now: Date): string {
  if (dayKey(d) === dayKey(addDays(now, 1))) return "Tomorrow";
  return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
}

function AttendeeStack({ meeting }: { meeting: UpcomingMeeting }) {
  const people = inviteesOf(meeting);
  if (people.length === 0) return null;
  const shown = people.slice(0, 3);
  const extra = people.length - shown.length;
  return (
    <div className="flex shrink-0 items-center gap-1.5" aria-label={`${people.length} attendees`}>
      <div className="flex -space-x-1.5">
        {shown.map((p) => (
          <span
            key={p.address ?? p.name}
            title={p.name}
            className={cn(
              "inline-flex size-6 items-center justify-center rounded-full border text-[11px] font-semibold leading-none",
              toneForName(p.name),
            )}
          >
            {initialOf(p.name)}
          </span>
        ))}
      </div>
      {extra > 0 && (
        <span
          title={`${extra} more`}
          className="num text-[12px] font-medium text-muted"
        >
          +{extra}
        </span>
      )}
    </div>
  );
}

function ScheduleRow({
  meeting,
  now,
  dayPrefix,
}: {
  meeting: UpcomingMeeting;
  now: Date;
  dayPrefix?: string;
}) {
  const start = startOf(meeting);
  const when = whenOf(meeting, now);
  const rel = relative(meeting, now);
  const fallbackTitle =
    (meeting as UpcomingMeeting & { title?: string | null; subject?: string | null }).title ||
    (meeting as UpcomingMeeting & { title?: string | null; subject?: string | null }).subject ||
    "Untitled meeting";
  const title = meeting.private ? "Private event" : (meeting.object?.label || fallbackTitle);
  const briefHref = !meeting.private ? (safeHref(meeting.brief_url) ?? "/meetings") : null;
  const joinHref = meeting.join_url && when !== "past" ? safeHref(meeting.join_url) : null;
  const parties = (meeting.parties ?? []).map((p) => p.label).filter(Boolean);
  const contextLine = parties.length > 0 ? parties.join(", ") : meeting.location ?? null;
  const imminent = when === "now" || when === "soon";
  const hasAttendees = !meeting.private && inviteesOf(meeting).length > 0;

  return (
    <li
      data-testid="today-schedule-row"
      className={cn(
        "group relative flex items-start gap-3 rounded-lg px-2 py-3 transition-colors duration-150",
        when === "now" ? "bg-accent-soft" : "hover:bg-hover",
        when === "past" && "opacity-60",
      )}
    >
      <div className="num w-[68px] shrink-0 pt-0.5">
        {dayPrefix && (
          <div className="text-[11px] font-medium leading-3.5 text-faint">{dayPrefix}</div>
        )}
        <div
          className={cn(
            "text-[12.5px] leading-4",
            when === "now" ? "font-semibold text-accent" : imminent ? "font-semibold text-fg" : "font-medium text-muted",
          )}
        >
          {meeting.all_day ? "All day" : start ? clock(start) : "-"}
        </div>
        {!dayPrefix && imminent && rel && (
          <div className={cn("mt-0.5 text-[11px] font-semibold leading-3.5", when === "now" ? "text-accent" : "text-live")}>
            {rel}
          </div>
        )}
      </div>

      <div className="min-w-0 flex-1">
        {briefHref ? (
          <Link
            href={briefHref}
            className="line-clamp-2 text-[13.5px] font-semibold leading-[19px] text-fg transition-colors duration-150 group-hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 rounded-xs"
            title={title}
          >
            {title}
          </Link>
        ) : (
          <p className="flex items-center gap-1.5 text-[13.5px] font-medium leading-[19px] text-muted">
            <Lock className="size-3.5 shrink-0" strokeWidth={1.8} aria-hidden />
            <span className="line-clamp-2">{title}</span>
          </p>
        )}

        {contextLine && (
          <p className="mt-0.5 flex items-center gap-1 truncate text-[12px] leading-4 text-muted">
            {parties.length === 0 && <MapPin className="size-3 shrink-0 text-faint" strokeWidth={1.8} aria-hidden />}
            <span className="truncate">{contextLine}</span>
          </p>
        )}

        {(hasAttendees || joinHref) && (
          <div className="mt-2 flex items-center justify-between gap-2">
            <div className="min-w-0">
              {hasAttendees && <AttendeeStack meeting={meeting} />}
            </div>
            {joinHref && (
              <a
                href={joinHref}
                target="_blank"
                rel="noreferrer"
                data-testid="today-schedule-join"
                className={cn(
                  "liquid-glass-pill inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md px-2.5 text-[12px] font-medium transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
                  imminent ? "text-accent" : "text-fg hover:text-accent",
                )}
              >
                <Video className="size-3.5" strokeWidth={1.85} aria-hidden />
                Join
              </a>
            )}
          </div>
        )}
      </div>
    </li>
  );
}

const EMPTY_MEETINGS: UpcomingMeeting[] = [];

export function TodayScheduleCard({
  cardRef,
  highlighted = false,
  sticky = true,
  onSummaryChange,
}: {
  cardRef?: RefObject<HTMLElement | null>;
  highlighted?: boolean;
  sticky?: boolean;
  onSummaryChange?: (summary: ScheduleSummary) => void;
}) {
  const { data, error, retry } = useRead<UpcomingMeetings>(
    "/api/brief/meetings",
    (signal) => getUpcomingMeetings(signal),
  );
  const [refreshing, setRefreshing] = useState(false);
  const [now, setNow] = useState(() => new Date());
  const onSummaryRef = useRef(onSummaryChange);
  onSummaryRef.current = onSummaryChange;

  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    if (data || error) setRefreshing(false);
  }, [data, error]);

  const meetings = data?.meetings ?? EMPTY_MEETINGS;
  const todayKey = dayKey(now);

  const { todayMeetings, upNext } = useMemo(() => {
    const days = byDay(meetings);
    const today = days.find((d) => d.key === todayKey)?.meetings ?? EMPTY_MEETINGS;
    const future: { day: Date; meeting: UpcomingMeeting }[] = [];
    for (const d of days) {
      if (d.key <= todayKey) continue;
      for (const m of d.meetings) {
        future.push({ day: d.date, meeting: m });
      }
    }
    const maxNext = today.length === 0 ? 4 : today.length < 3 ? 3 - today.length : 0;
    return {
      todayMeetings: today,
      upNext: future.slice(0, maxNext),
    };
  }, [meetings, todayKey]);

  const loading = !data && !error;

  const nextToday = todayMeetings.find((m) => whenOf(m, now) !== "past");
  const nextStart = nextToday ? startOf(nextToday) : null;
  const todayCount = todayMeetings.length;
  const nextTime = nextToday?.all_day ? "All day" : nextStart ? clock(nextStart) : null;

  useEffect(() => {
    onSummaryRef.current?.({ todayCount, nextTime, loading });
  }, [todayCount, nextTime, loading]);

  return (
    <section
      ref={cardRef}
      tabIndex={-1}
      aria-label="Today's schedule"
      data-testid="today-schedule-card"
      className={cn(
        "rounded-xl border border-border/75 bg-bg p-4 outline-none transition-colors duration-150 dark:bg-[#14161b]",
        sticky && "lg:sticky lg:top-6",
        highlighted && "border-accent",
      )}
    >
      <header className="flex items-center justify-between gap-2 border-b border-border/70 pb-3">
        <div className="flex min-w-0 items-baseline gap-2">
          <h2 className="font-display text-[15px] font-semibold tracking-[-0.015em] text-fg">Today</h2>
          <span className="truncate text-[12.5px] font-medium text-muted">
            {shortHeaderDate(now)}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            disabled={loading || refreshing}
            onClick={() => {
              setRefreshing(true);
              retry();
            }}
            aria-label="Refresh schedule"
            title="Refresh schedule"
            data-testid="today-schedule-refresh"
            className="inline-flex size-7 items-center justify-center rounded-md text-muted transition-colors duration-150 hover:bg-hover hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 disabled:opacity-50 cursor-pointer"
          >
            <RefreshCw className={cn("size-3.5", refreshing && "animate-spin")} strokeWidth={1.9} aria-hidden />
          </button>
          <Link
            href="/meetings"
            data-testid="today-schedule-full-link"
            className="inline-flex h-7 items-center gap-0.5 rounded-md px-2 text-[12px] font-medium text-accent transition-colors duration-150 hover:bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
          >
            Open schedule
            <ChevronRight className="size-3.5" strokeWidth={2} aria-hidden />
          </Link>
        </div>
      </header>

      {error ? (
        <div className="px-2 py-5 text-[12.5px] text-muted" data-testid="today-schedule-error">
          <p>{plainError(error)}</p>
          <button
            type="button"
            onClick={() => {
              setRefreshing(true);
              retry();
            }}
            className="mt-2.5 inline-flex h-7 items-center rounded-md border border-border-strong bg-bg px-2.5 text-[12px] font-medium text-fg transition-colors duration-150 hover:bg-hover cursor-pointer"
          >
            Try again
          </button>
        </div>
      ) : loading ? (
        <div className="space-y-2.5 pt-3" data-testid="today-schedule-loading">
          <div className="skeleton h-12 w-full" />
          <div className="skeleton h-12 w-5/6" />
          <div className="skeleton h-12 w-4/6" />
        </div>
      ) : meetings.length === 0 ? (
        <div className="px-2 py-7 text-center" data-testid="today-schedule-empty">
          <Calendar className="mx-auto size-5 text-muted" strokeWidth={1.75} aria-hidden />
          <p className="mt-2.5 text-[13.5px] font-medium text-fg">No meetings scheduled</p>
          <p className="mt-1 text-[12.5px] text-muted">
            Upcoming calendar events and prep briefs appear here once your calendar syncs.
          </p>
          <Link
            href="/meetings"
            className="mt-3 inline-flex h-7 items-center gap-1 rounded-md border border-border-strong bg-surface px-2.5 text-[12px] font-medium text-fg transition-colors duration-150 hover:bg-hover"
          >
            Open Meetings
            <ArrowUpRight className="size-3.5 text-muted" aria-hidden />
          </Link>
        </div>
      ) : (
        <>
          {todayMeetings.length > 0 ? (
            <ol className="divide-y divide-border/50 pt-1" data-testid="today-schedule-list">
              {todayMeetings.map((m, idx) => (
                <ScheduleRow key={m.object?.object_id ?? `${m.start ?? ""}-${idx}`} meeting={m} now={now} />
              ))}
            </ol>
          ) : (
            <div className="px-1 py-4 text-[12.5px] text-muted" data-testid="today-schedule-clear">
              No meetings left on your calendar today.
            </div>
          )}

          {upNext.length > 0 && (
            <div className="mt-1 border-t border-border/70 pt-3" data-testid="today-schedule-up-next">
              <div className="px-2 pb-1 text-[12px] font-medium text-muted">
                Up next
              </div>
              <ol className="divide-y divide-border/50">
                {upNext.map(({ day, meeting }, idx) => (
                  <ScheduleRow
                    key={meeting.object?.object_id ?? `${meeting.start ?? ""}-${idx}`}
                    meeting={meeting}
                    now={now}
                    dayPrefix={shortDayLabel(day, now)}
                  />
                ))}
              </ol>
            </div>
          )}

          <footer className="mt-2 flex items-center justify-between gap-2 border-t border-border/70 px-1 pt-2.5 text-[12px]">
            <Link href="/meetings?view=weekly" className="font-medium text-muted transition-colors duration-150 hover:text-fg">
              Weekly review
            </Link>
            <Link href="/meetings?view=follow-ups" className="font-medium text-muted transition-colors duration-150 hover:text-fg">
              Follow-ups
            </Link>
          </footer>
        </>
      )}
    </section>
  );
}
