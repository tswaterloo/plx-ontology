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

const AVATAR_TONES = [
  "bg-surface-2 text-fg/85",
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

function refreshedLabel(at: number | null, now: Date): string | null {
  if (!at) return null;
  const mins = Math.max(0, Math.floor((now.getTime() - at) / 60_000));
  if (mins < 1) return "Refreshed just now";
  if (mins === 1) return "Refreshed 1 min ago";
  if (mins < 60) return `Refreshed ${mins} min ago`;
  const hrs = Math.floor(mins / 60);
  return hrs === 1 ? "Refreshed 1 hr ago" : `Refreshed ${hrs} hrs ago`;
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
              "inline-flex size-6 items-center justify-center rounded-full border border-border-strong text-[10.5px] font-semibold leading-none",
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
          className="num text-[11.5px] font-medium text-muted"
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

  return (
    <li
      data-testid="today-schedule-row"
      className={cn(
        "group relative flex items-center gap-3 px-1.5 py-2.5 transition-colors hover:bg-hover",
        when === "past" && "opacity-65",
      )}
    >
      {when === "now" && (
        <span className="absolute inset-y-2 left-0 w-[2px] bg-accent" aria-hidden />
      )}

      <div className="num w-[68px] shrink-0">
        {dayPrefix && (
          <div className="text-[10.5px] font-medium leading-3.5 text-faint">{dayPrefix}</div>
        )}
        <div className={cn("text-[12.5px] leading-4", imminent ? "font-semibold text-fg" : "font-medium text-muted")}>
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
            className="block truncate text-[13.5px] font-semibold leading-5 text-fg transition-colors group-hover:text-accent"
            title={title}
          >
            {title}
          </Link>
        ) : (
          <p className="flex items-center gap-1.5 truncate text-[13.5px] font-medium leading-5 text-muted">
            <Lock className="size-3.5 shrink-0" strokeWidth={1.8} aria-hidden />
            <span className="truncate">{title}</span>
          </p>
        )}

        {contextLine && (
          <p className="mt-0.5 flex items-center gap-1 truncate text-[12px] leading-4 text-muted">
            {parties.length === 0 && <MapPin className="size-3 shrink-0 text-faint" strokeWidth={1.8} aria-hidden />}
            <span className="truncate">{contextLine}</span>
          </p>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-2">
        {joinHref && (
          <a
            href={joinHref}
            target="_blank"
            rel="noreferrer"
            data-testid="today-schedule-join"
            className={cn(
              "liquid-glass-pill inline-flex h-7 shrink-0 items-center gap-1 rounded-md px-2.5 text-[12px] font-medium transition-transform active:scale-[0.98]",
              imminent ? "text-accent" : "text-fg hover:text-accent",
            )}
          >
            <Video className="size-3.5" strokeWidth={1.85} aria-hidden />
            Join
          </a>
        )}
        {!meeting.private && <AttendeeStack meeting={meeting} />}
      </div>
    </li>
  );
}

const EMPTY_MEETINGS: UpcomingMeeting[] = [];

export function TodayScheduleCard({
  cardRef,
  highlighted = false,
  onSummaryChange,
}: {
  cardRef?: RefObject<HTMLElement | null>;
  highlighted?: boolean;
  onSummaryChange?: (summary: ScheduleSummary) => void;
}) {
  const { data, error, retry, at } = useRead<UpcomingMeetings>(
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
  const freshness = refreshedLabel(at, now);

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
        "outline-none transition-colors lg:sticky lg:top-6 lg:border-l lg:border-border lg:pl-6",
        highlighted && "border-accent",
      )}
    >
      <header className="flex items-center justify-between gap-2 border-b border-border pb-2.5">
        <div className="flex min-w-0 items-baseline gap-2">
          <h2 className="font-display text-[14.5px] font-semibold tracking-[-0.015em] text-fg">Today</h2>
          <span className="truncate text-[12.5px] font-medium text-muted">
            {shortHeaderDate(now)}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {freshness && (
            <span className="hidden text-[11.5px] text-faint xl:inline" title={freshness}>
              {freshness}
            </span>
          )}
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
            className="inline-flex size-7 items-center justify-center rounded-md text-muted transition-colors hover:bg-hover hover:text-fg disabled:opacity-50 cursor-pointer"
          >
            <RefreshCw className={cn("size-3.5", refreshing && "animate-spin")} strokeWidth={1.9} aria-hidden />
          </button>
          <Link
            href="/meetings"
            data-testid="today-schedule-full-link"
            className="inline-flex h-7 items-center gap-0.5 rounded-md px-2 text-[12px] font-medium text-accent transition-colors hover:bg-hover"
          >
            Full schedule
            <ChevronRight className="size-3.5" strokeWidth={2} aria-hidden />
          </Link>
        </div>
      </header>

      {error ? (
        <div className="px-4 py-5 text-[12.5px] text-muted" data-testid="today-schedule-error">
          <p>{plainError(error)}</p>
          <button
            type="button"
            onClick={() => {
              setRefreshing(true);
              retry();
            }}
            className="mt-2 inline-flex h-7 items-center rounded-md border border-border-strong bg-bg px-2.5 text-[12px] font-medium text-fg hover:bg-hover cursor-pointer"
          >
            Try again
          </button>
        </div>
      ) : loading ? (
        <div className="space-y-3 p-4" data-testid="today-schedule-loading">
          <div className="skeleton h-10 w-full" />
          <div className="skeleton h-10 w-5/6" />
          <div className="skeleton h-10 w-4/6" />
        </div>
      ) : meetings.length === 0 ? (
        <div className="px-4 py-8 text-center" data-testid="today-schedule-empty">
          <Calendar className="mx-auto size-5 text-muted" strokeWidth={1.75} aria-hidden />
          <p className="mt-2.5 text-[13.5px] font-medium text-fg">No meetings scheduled</p>
          <p className="mt-1 text-[12.5px] text-muted">
            Upcoming calendar events and prep briefs appear here once your calendar syncs.
          </p>
          <Link
            href="/meetings"
            className="mt-3 inline-flex h-7 items-center gap-1 rounded-md border border-border-strong bg-bg px-2.5 text-[12px] font-medium text-fg hover:bg-hover"
          >
            Open Meetings
            <ArrowUpRight className="size-3.5 text-muted" aria-hidden />
          </Link>
        </div>
      ) : (
        <>
          {todayMeetings.length > 0 ? (
            <ol className="divide-y divide-border/60" data-testid="today-schedule-list">
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
            <div className="border-t border-border/80" data-testid="today-schedule-up-next">
              <div className="px-1 pt-3 pb-1 text-[11px] font-semibold uppercase tracking-[0.04em] text-faint">
                Up next
              </div>
              <ol className="divide-y divide-border/60">
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

          <footer className="flex items-center justify-between gap-2 border-t border-border/80 px-1 py-2.5 text-[12px]">
            <Link href="/meetings?view=weekly" className="font-medium text-muted transition-colors hover:text-fg">
              Weekly review
            </Link>
            <Link href="/meetings?view=follow-ups" className="font-medium text-muted transition-colors hover:text-fg">
              Follow-ups
            </Link>
          </footer>
        </>
      )}
    </section>
  );
}
