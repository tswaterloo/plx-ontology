"use client";

import { ArrowDownRight, ArrowUpRight, ChevronLeft, ChevronRight, MapPin, MessageCircle, Users, Video } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { figureText, mondayBefore } from "@/components/brief-view";
import { SourceChips } from "@/components/source-chip";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { getUpcomingMeetings, getWeeklyReview } from "@/lib/api";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { formatDate, formatMoney, objectHref, readable, safeHref } from "@/lib/format";
import type { Brief, BriefFigure, BriefSection, UpcomingMeeting } from "@/lib/types";
import { clock, dayTitle, startOf, whenOf } from "./calendar";
import { MeetingSheet } from "./meeting-sheet";

/** The sections the review talks through, in the order the meeting takes them; the rest
 * (flows, money, supplier trends) sit beside them as context. An unknown key is discussed. */
const CONTEXT = new Set(["alert_flow", "cash", "margin", "suppliers", "top_alerts"]);

/** A figure where a rise is bad news (late, missed, held, opened alerts). */
function worseWhenUp(label: string): boolean {
  return /late|missed|hold|opened|over \d+ days|ap due/i.test(label);
}

/** Meetings, Weekly review: the standing review one week at a time. The week's figures
 * against the week before, then what the meeting discusses, with the money and trend
 * context beside it, and the next review on the calendar. */
export function WeeklyReview() {
  const [weeks, setWeeks] = useState(1);
  const [brief, setBrief] = useState<Brief | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const [next, setNext] = useState<UpcomingMeeting | null>(null);
  const [open, setOpen] = useState<UpcomingMeeting | null>(null);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  const monday = mondayBefore(new Date(), weeks);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    setBrief(null);
    getWeeklyReview(monday, ctl.signal).then(setBrief, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [monday, round]);
  useEffect(() => {
    const ctl = new AbortController();
    const now = new Date();
    // the next review on your calendar: a meeting whose title names a weekly review
    getUpcomingMeetings(ctl.signal).then(
      (l) => setNext(l.meetings.find((m) => /\bweekly\b.*\breview\b|\breview\b.*\bweekly\b/i.test(m.object.label) && whenOf(m, now) !== "past") ?? null),
      () => setNext(null),
    );
    return () => ctl.abort();
  }, []);
  const sections = brief?.sections ?? [];
  const figures = sections.find((s) => s.key === "figures");
  const discuss = sections.filter((s) => s.key !== "figures" && !CONTEXT.has(s.key));
  const context = sections.filter((s) => CONTEXT.has(s.key));
  return (
    <div className="space-y-5" data-testid="weekly-review">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3">
        <div className="flex items-center gap-1 text-[13px]" data-testid="week-picker">
          <button type="button" aria-label="Earlier week" className="flex size-7 items-center justify-center rounded-md hover:bg-hover cursor-pointer" onClick={() => setWeeks((w) => w + 1)}>
            <ChevronLeft className="size-4" aria-hidden />
          </button>
          <span className="min-w-[9rem] text-center font-medium tabular-nums">Week of {formatDate(monday, { short: true })}</span>
          <button type="button" aria-label="Later week" className="flex size-7 items-center justify-center rounded-md hover:bg-hover disabled:opacity-40 cursor-pointer" disabled={weeks <= 0} onClick={() => setWeeks((w) => Math.max(0, w - 1))}>
            <ChevronRight className="size-4" aria-hidden />
          </button>
          {brief && <span className="ml-3 text-[12px] text-muted">Read {formatDate(brief.as_of)}{brief.stored_at ? ` · kept ${formatDate(brief.stored_at)}` : ""}</span>}
        </div>
        <div className="flex items-center gap-4 text-[13px]">
          {next && <NextReview m={next} onOpen={setOpen} />}
          {brief?.summary_question && (
            <Link href={`/ask?q=${encodeURIComponent(brief.summary_question)}`} className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-3 font-medium">
              <MessageCircle className="size-[15px]" strokeWidth={1.8} aria-hidden />
              Summarise
            </Link>
          )}
        </div>
      </div>
      {error ? (
        <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
      ) : !brief ? (
        <Pending label="Gathering the week" />
      ) : (
        <>
          {figures && <FigureStrip section={figures} />}
          <div className="grid gap-x-10 gap-y-6 xl:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
            <div className="min-w-0 space-y-6">
              <p className="text-[12px] font-semibold tracking-[0.02em] text-muted uppercase">To discuss</p>
              <Sections list={discuss} numbered />
            </div>
            <div className="min-w-0 space-y-6 xl:border-l xl:border-border xl:pl-10">
              <p className="text-[12px] font-semibold tracking-[0.02em] text-muted uppercase">Context</p>
              <Sections list={context} />
            </div>
          </div>
          <p className="text-[12px] text-muted">{brief.basis}</p>
        </>
      )}
      <MeetingSheet meeting={open} now={new Date()} onClose={() => setOpen(null)} />
    </div>
  );
}

function NextReview({ m, onOpen }: { m: UpcomingMeeting; onOpen: (m: UpcomingMeeting) => void }) {
  const s = startOf(m);
  const people = (m.attendees ?? []).length + (m.organizer ? 1 : 0);
  const items = (m.open_items ?? []).length;
  return (
    <button type="button" onClick={() => onOpen(m)} className="flex items-center gap-3 rounded-md px-2 py-1 text-left hover:bg-hover cursor-pointer" data-testid="next-review">
      <span className="text-muted">Next review</span>
      <span className="num font-medium">{s ? `${dayTitle(s, new Date()).split(" · ")[0].replace(/,.*$/, "")} ${clock(s)}` : "—"}</span>
      {m.location ? (
        <span className="inline-flex items-center gap-1 text-muted">
          <MapPin className="size-3.5" strokeWidth={1.8} aria-hidden />
          {m.location}
        </span>
      ) : m.online ? (
        <span className="inline-flex items-center gap-1 text-muted">
          <Video className="size-3.5" strokeWidth={1.8} aria-hidden />
          Teams
        </span>
      ) : null}
      {people > 0 && (
        <span className="inline-flex items-center gap-1 text-muted">
          <Users className="size-3.5" strokeWidth={1.8} aria-hidden />
          <span className="num">{people}</span>
        </span>
      )}
      {items > 0 && <span className="text-risk"><span className="num">{items}</span> carried</span>}
    </button>
  );
}

function FigureStrip({ section }: { section: BriefSection }) {
  const figures = section.figures ?? [];
  return (
    <div data-testid="weekly-figures">
      <dl className="grid grid-cols-2 border-y border-border sm:grid-cols-4 xl:grid-cols-7">
        {figures.map((f, i) => (
          <Figure key={`${f.label}-${i}`} f={f} />
        ))}
      </dl>
      {section.note && <p className="mt-1.5 text-[12px] text-muted">{readable(section.note)}</p>}
      {section.state === "partial" && <p className="text-[12px] text-muted">A read limit was reached: some figures are at least this many.</p>}
    </div>
  );
}

function Figure({ f }: { f: BriefFigure }) {
  const missing = f.value == null;
  const d = f.delta != null ? Number(f.delta) : null;
  const bad = d != null && d !== 0 && (d > 0) === worseWhenUp(f.label);
  const Arrow = d != null && d > 0 ? ArrowUpRight : ArrowDownRight;
  const prev = f.previous != null ? figureText({ value: f.previous, unit: f.unit, currency: f.currency }) : null;
  return (
    <div className="min-w-0 border-r border-border px-3 py-2.5 last:border-r-0" data-testid="brief-figure">
      <dt className="truncate text-[12px] text-muted" title={f.label}>{f.label}</dt>
      <dd className={cn("num leading-7", missing ? "text-[13px] text-muted" : "text-[19px] font-semibold")}>{figureText(f)}</dd>
      <dd className="flex items-center gap-1 text-[12px]">
        {d != null && d !== 0 && (
          <span className={cn("num inline-flex items-center", bad ? "text-late" : "text-ok")}>
            <Arrow className="size-3.5" strokeWidth={2} aria-hidden />
            {f.delta}
            {f.unit === "percent" ? " pts" : ""}
          </span>
        )}
        {d === 0 && <span className="text-muted">no change</span>}
        {prev && <span className="truncate text-faint">vs {prev}</span>}
      </dd>
      {f.note && !missing && <dd className="mt-0.5 line-clamp-2 text-[11px] leading-4 text-faint">{f.note}</dd>}
    </div>
  );
}

function Sections({ list, numbered = false }: { list: BriefSection[]; numbered?: boolean }) {
  const shown = list.filter((s) => (s.rows ?? []).length > 0 || (s.figures ?? []).length > 0);
  const quiet = list.filter((s) => !shown.includes(s));
  return (
    <>
      {shown.map((s, i) => (
        <section key={s.key} data-testid={`brief-section-${s.key}`}>
          <h3 className="flex items-baseline gap-2 border-b border-border pb-1 text-[13px] font-semibold">
            {numbered && <span className="num text-[12px] font-normal text-faint">{i + 1}</span>}
            {s.title}
            <span className="num text-[12px] font-normal text-muted">{(s.rows ?? []).length + (s.more ?? 0) || ""}</span>
          </h3>
          {s.note && s.state !== "shown" && <p className="mt-1 text-[12px] text-muted">{readable(s.note)}</p>}
          {(s.figures ?? []).length > 0 && s.key !== "late_pos" && (
            <dl className="mt-2 flex flex-wrap gap-x-6 gap-y-1">
              {(s.figures ?? []).map((f) => (
                <div key={f.label}>
                  <dt className="text-[12px] text-muted">{f.label}</dt>
                  <dd className="num text-[15px] font-semibold">
                    {f.unit === "money" && f.value != null && f.currency ? formatMoney(f.value, f.currency) : figureText(f)}
                    {f.previous != null && <span className="ml-1.5 text-[12px] font-normal text-faint">vs {figureText({ value: f.previous, unit: f.unit, currency: f.currency })}</span>}
                  </dd>
                </div>
              ))}
            </dl>
          )}
          <ul className="divide-y divide-border">
            {(s.rows ?? []).map((r, j) => {
              const href = r.object ? safeHref(r.object.open_url) ?? objectHref(r.object.type, r.object.object_id) : null;
              return (
                <li key={`${r.label}-${j}`} className="flex items-baseline justify-between gap-4 py-2 text-[13px]" data-testid="brief-row">
                  <span className="min-w-0">
                    {href ? (
                      <Link href={href} className="font-medium hover:underline">
                        {readable(r.label)}
                      </Link>
                    ) : (
                      <span className="font-medium">{readable(r.label)}</span>
                    )}
                    {(r.who || r.detail) && (
                      <span className="block text-[12px] text-muted">
                        {r.who}
                        {r.who && r.detail ? " · " : ""}
                        {r.detail && readable(r.detail)}
                      </span>
                    )}
                  </span>
                  <span className="flex shrink-0 items-center gap-2 text-[12px] text-muted">
                    {r.amount && <span className="num text-fg">{formatMoney(r.amount.amount, r.amount.currency)}</span>}
                    {r.date && <span className="num">{formatDate(r.date, { short: true })}</span>}
                    <SourceChips sources={r.sources} max={1} />
                  </span>
                </li>
              );
            })}
          </ul>
          {(s.more ?? 0) > 0 && <p className="mt-1 text-[12px] text-muted">{s.more} more not listed.</p>}
        </section>
      ))}
      {quiet.length > 0 && (
        <p className="text-[12px] text-muted">
          Nothing this week on {quiet.map((s) => s.title.toLowerCase()).join(", ")}
          {quiet.some((s) => s.state === "finance_marked") ? " (finance-marked sections show only to finance readers)" : ""}
          {quiet.some((s) => s.state === "unread" || s.state === "blocked" || s.state === "partial") ? "; some could not be read in full" : ""}.
        </p>
      )}
    </>
  );
}
