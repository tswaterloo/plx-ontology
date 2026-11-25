"use client";

import { ArrowDownRight, ArrowUpRight, ChevronLeft, ChevronRight, MapPin, MessageCircle, Users, Video } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { figureText, mondayBefore, signedDelta } from "@/components/brief-view";
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

/** A review still being built is asked for again this often until it is filled in. */
const READING_AGAIN_MS = 10_000;

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
    let again: ReturnType<typeof setTimeout> | undefined;
    setError(null);
    setBrief(null);
    getWeeklyReview(monday, ctl.signal).then(
      (b) => {
        setBrief(b);
        // still being built (nothing of it shown yet): ask again until it is filled in
        if (b.reading && !ctl.signal.aborted) again = setTimeout(() => setRound((n) => n + 1), READING_AGAIN_MS);
      },
      (e) => {
        if (!ctl.signal.aborted) setError(e);
      },
    );
    return () => {
      ctl.abort();
      if (again) clearTimeout(again);
    };
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
  const splitQuiet = (list: BriefSection[]) => {
    const shown = list.filter((s) => (s.rows ?? []).length > 0 || (s.figures ?? []).length > 0 || (s.state === "unread" && !!s.note));
    const left = list.filter((s) => !shown.includes(s));
    const unread = left.filter((s) => s.state === "unread" || s.state === "blocked" || s.state === "partial");
    const quiet = left.filter((s) => !unread.includes(s));
    return { shown, unread, quiet };
  };
  const discussSplit = splitQuiet(discuss);
  const contextSplit = splitQuiet(context);
  const allQuiet = [...discussSplit.quiet, ...contextSplit.quiet];
  const allUnread = [...discussSplit.unread, ...contextSplit.unread];
  return (
    <div className="space-y-6" data-testid="weekly-review">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3">
        <div className="flex flex-wrap items-center gap-1.5 text-[13px]" data-testid="week-picker">
          <button type="button" aria-label="Earlier week" className="flex size-8 items-center justify-center rounded-lg border border-border bg-bg transition-colors hover:border-border-strong hover:bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 active:scale-[0.98] cursor-pointer" onClick={() => setWeeks((w) => w + 1)}>
            <ChevronLeft className="size-4" aria-hidden />
          </button>
          <span className="min-w-[9.5rem] text-center font-semibold tabular-nums">Week of {formatDate(monday, { short: true })}</span>
          <button type="button" aria-label="Later week" className="flex size-8 items-center justify-center rounded-lg border border-border bg-bg transition-colors hover:border-border-strong hover:bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 active:scale-[0.98] disabled:pointer-events-none disabled:opacity-40 cursor-pointer" disabled={weeks <= 0} onClick={() => setWeeks((w) => Math.max(0, w - 1))}>
            <ChevronRight className="size-4" aria-hidden />
          </button>
          {weeks !== 0 && (
            <button
              type="button"
              onClick={() => setWeeks(0)}
              className="ml-1 inline-flex h-8 items-center rounded-lg border border-border bg-bg px-2.5 text-[12px] font-medium text-muted transition-colors hover:border-border-strong hover:bg-hover hover:text-fg cursor-pointer"
            >
              This week
            </button>
          )}
          {brief && <span className="ml-2 text-[12px] whitespace-nowrap text-muted">Read {formatDate(brief.as_of)}{brief.stored_at ? ` · kept ${formatDate(brief.stored_at)}` : ""}</span>}
        </div>
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2 text-[13px]">
          {next && <NextReview m={next} onOpen={setOpen} />}
          {brief?.summary_question && (
            <Link href={`/ask?q=${encodeURIComponent(brief.summary_question)}`} className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-bg px-3 font-medium text-fg shadow-sm hover:border-border-strong hover:bg-hover">
              <MessageCircle className="size-[15px] text-muted" strokeWidth={1.8} aria-hidden />
              Summarise
            </Link>
          )}
        </div>
      </div>
      {error ? (
        <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
      ) : !brief ? (
        <Pending label="Reading the week's meetings" />
      ) : brief.reading ? (
        <div data-testid="weekly-building">
          <Pending label={brief.ready_about ? `Building the week's review for you, ready about ${clock(new Date(brief.ready_about))}` : "Building the week's review for you"} />
          <p className="mt-2 text-[12px] text-muted">{brief.basis}</p>
        </div>
      ) : (
        <>
          {figures && <FigureStrip section={figures} />}
          <div className="grid gap-6 xl:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
            <div className="min-w-0 space-y-4">
              <p className="text-[11px] font-semibold tracking-[0.05em] text-faint uppercase">To discuss</p>
              <Sections shown={discussSplit.shown} />
            </div>
            <div className="min-w-0 space-y-4">
              <p className="text-[11px] font-semibold tracking-[0.05em] text-faint uppercase">Context</p>
              <Sections shown={contextSplit.shown} />
            </div>
          </div>
          {(allQuiet.length > 0 || allUnread.length > 0) && (
            <div className="space-y-1 border-t border-border/70 pt-3">
              {allQuiet.length > 0 && (
                <p className="text-[12px] text-muted" title={allQuiet.map((s) => s.title).join(" · ")} data-testid="weekly-quiet">
                  {allQuiet.length === 1
                    ? `Nothing this week on ${allQuiet[0].title.charAt(0).toLowerCase()}${allQuiet[0].title.slice(1)}`
                    : `${allQuiet.length} other topics had nothing this week (${allQuiet.map((s) => s.title.toLowerCase()).join(", ")})`}
                  .
                </p>
              )}
              {allUnread.length > 0 && (
                <p className="text-[12px] text-muted" data-testid="weekly-unread">
                  Not read in full, so not known: {allUnread.map((s) => s.title.charAt(0).toLowerCase() + s.title.slice(1)).join(", ")}.
                </p>
              )}
            </div>
          )}
          <p className="text-[12px] text-faint">{brief.basis}</p>
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
    <button type="button" onClick={() => onOpen(m)} className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 rounded-lg px-2.5 py-1 text-left whitespace-nowrap hover:bg-hover cursor-pointer" data-testid="next-review">
      <span className="text-muted">Next review</span>
      <span className="font-medium tabular-nums">{s ? `${dayTitle(s, new Date()).split(" · ")[0].replace(/,.*$/, "")} ${clock(s)}` : "TBD"}</span>
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
          <span className="tabular-nums">{people}</span>
        </span>
      )}
      {items > 0 && <span className="text-risk"><span className="tabular-nums">{items}</span> carried</span>}
    </button>
  );
}

type TileModel = { kind: "single"; f: BriefFigure } | { kind: "invoiced_pair"; items: BriefFigure[] };

/** Combine Invoiced (CAD) and Invoiced (USD) into one dual-currency card so a 7-metric week becomes a balanced 6-card 3x2 grid with zero empty slots. */
function pairFigures(figures: BriefFigure[]): TileModel[] {
  const invoiced = figures.filter((f) => /^Invoiced\s*\([A-Z]{3}\)$/i.test(f.label));
  if (invoiced.length < 2) return figures.map((f) => ({ kind: "single", f }));
  const out: TileModel[] = [];
  let inserted = false;
  for (const f of figures) {
    if (/^Invoiced\s*\([A-Z]{3}\)$/i.test(f.label)) {
      if (!inserted) {
        out.push({ kind: "invoiced_pair", items: invoiced });
        inserted = true;
      }
    } else {
      out.push({ kind: "single", f });
    }
  }
  return out;
}

function FigureStrip({ section }: { section: BriefSection }) {
  const figures = section.figures ?? [];
  const tiles = pairFigures(figures);
  const cols = tiles.length <= 3 ? "sm:grid-cols-3" : tiles.length === 4 ? "sm:grid-cols-2 lg:grid-cols-4" : "sm:grid-cols-2 lg:grid-cols-3";
  return (
    <div data-testid="weekly-figures">
      <dl className={cn("grid grid-cols-1 gap-3", cols)}>
        {tiles.map((t, i) =>
          t.kind === "invoiced_pair" ? (
            <InvoicedPairTile key={`inv-${i}`} items={t.items} />
          ) : (
            <Figure key={`${t.f.label}-${i}`} f={t.f} />
          ),
        )}
      </dl>
      {section.note && <p className="mt-2 text-[12px] text-muted">{readable(section.note)}</p>}
      {section.state === "partial" && <p className="mt-1 text-[12px] text-muted">A read limit was reached: some figures are at least this many.</p>}
    </div>
  );
}

function DeltaPill({ f }: { f: BriefFigure }) {
  const d = f.delta != null ? Number(f.delta) : null;
  if (d == null) return null;
  if (d === 0) return <span className="rounded-sm border border-neutral-line bg-neutral-soft px-1.5 py-0.5 text-[11px] font-medium text-muted">no change</span>;
  const bad = (d > 0) === worseWhenUp(f.label);
  const Arrow = d > 0 ? ArrowUpRight : ArrowDownRight;
  return (
    <span className={cn("inline-flex items-center rounded-sm border px-1.5 py-0.5 text-[11.5px] font-medium whitespace-nowrap tabular-nums", bad ? "border-late-line bg-late-soft text-late" : "border-ok-line bg-ok-soft text-ok")}>
      <Arrow className="mr-0.5 size-3 shrink-0" strokeWidth={2.2} aria-hidden />
      {signedDelta(f)}
    </span>
  );
}

function InvoicedPairTile({ items }: { items: BriefFigure[] }) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-bg p-4" data-testid="brief-figure">
      <dt className="text-[12px] font-medium text-muted">Invoiced</dt>
      <div className="mt-2 space-y-2.5">
        {items.map((f) => {
          const text = figureText(f);
          const moneyMatch = f.value != null && f.unit === "money" ? /^(.*?)\s+([A-Z]{3})$/.exec(text) : null;
          const prev = f.previous != null ? figureText({ value: f.previous, unit: f.unit, currency: f.currency }) : null;
          return (
            <div key={f.label} className="space-y-1 border-t border-border/60 pt-2 first:border-0 first:pt-0">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <dd className="text-[18px] leading-6 font-semibold tracking-[-0.02em] whitespace-nowrap tabular-nums text-fg">
                  {moneyMatch ? (
                    <>
                      {moneyMatch[1]}
                      <span className="ml-1 text-[12px] font-medium text-muted">{moneyMatch[2]}</span>
                    </>
                  ) : (
                    text
                  )}
                </dd>
                <DeltaPill f={f} />
              </div>
              {prev && <p className="text-[11.5px] text-faint tabular-nums">vs {prev}</p>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Figure({ f }: { f: BriefFigure }) {
  const missing = f.value == null;
  const prev = f.previous != null ? figureText({ value: f.previous, unit: f.unit, currency: f.currency }) : null;
  const text = figureText(f);
  const moneyMatch = !missing && f.unit === "money" ? /^(.*?)\s+([A-Z]{3})$/.exec(text) : null;
  return (
    <div className="flex min-w-0 flex-col justify-between rounded-md border border-border bg-bg p-4" data-testid="brief-figure">
      <div>
        <dt className="truncate text-[12px] font-medium text-muted" title={f.label}>{f.label}</dt>
        <dd className={cn("mt-1.5 leading-7 whitespace-nowrap tabular-nums", missing ? "text-[13px] text-muted" : "text-[22px] font-semibold tracking-[-0.02em] text-fg")}>
          {moneyMatch ? (
            <>
              {moneyMatch[1]}
              <span className="ml-1 text-[12px] font-medium text-muted">{moneyMatch[2]}</span>
            </>
          ) : (
            text
          )}
        </dd>
        <dd className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px] leading-4">
          <DeltaPill f={f} />
          {prev && <span className="whitespace-nowrap tabular-nums text-faint">vs {prev}</span>}
        </dd>
      </div>
      {f.note && !missing && <dd className="mt-2 line-clamp-2 text-[11.5px] leading-4 text-muted">{f.note}</dd>}
    </div>
  );
}

function Sections({ shown }: { shown: BriefSection[] }) {
  if (shown.length === 0) {
    return (
      <div className="rounded-md border border-border bg-surface/50 px-4 py-3 text-[13px] text-muted">
        Nothing flagged here this week.
      </div>
    );
  }
  return (
    <div className="space-y-4">
      {shown.map((s) => {
        const count = (s.rows ?? []).length + (s.more ?? 0);
        return (
        <section key={s.key} className="rounded-md border border-border bg-bg p-4" data-testid={`brief-section-${s.key}`}>
          <h3 className="flex items-center justify-between gap-2 border-b border-border/70 pb-2.5 text-[13.5px] font-semibold text-fg">
            <span>{s.title}</span>
            {count > 0 && (
              <span className="rounded-md bg-surface-2 px-1.5 py-0.5 text-[11px] font-medium leading-none text-muted tabular-nums" data-testid="section-count">
                {count}
              </span>
            )}
          </h3>
          {s.note && s.state !== "shown" && <p className="mt-2 text-[12.5px] text-muted">{readable(s.note)}</p>}
          {(s.figures ?? []).length > 0 && s.key !== "late_pos" && (
            <dl className="mt-3 grid grid-cols-2 gap-3 sm:flex sm:flex-wrap sm:gap-x-6 sm:gap-y-2">
              {(s.figures ?? []).map((f) => (
                <div key={f.label}>
                  <dt className="text-[12px] text-muted">{f.label}</dt>
                  <dd className="mt-0.5 text-[16px] font-semibold tabular-nums text-fg">
                    {f.unit === "money" && f.value != null && f.currency ? formatMoney(f.value, f.currency) : figureText(f)}
                    {f.previous != null && <span className="ml-1.5 text-[12px] font-normal text-faint">vs {figureText({ value: f.previous, unit: f.unit, currency: f.currency })}</span>}
                  </dd>
                </div>
              ))}
            </dl>
          )}
          {(s.rows ?? []).length > 0 && (
            <ul className="mt-1 divide-y divide-border/70">
              {(s.rows ?? []).map((r, j) => {
                const href = r.object ? safeHref(r.object.open_url) ?? objectHref(r.object.type, r.object.object_id) : null;
                return (
                  <li key={`${r.label}-${j}`} className="-mx-2 flex items-baseline justify-between gap-4 rounded-lg px-2 py-2.5 text-[13px] hover:bg-hover/60" data-testid="brief-row">
                    <span className="min-w-0">
                      {href ? (
                        <Link href={href} className="font-medium text-fg hover:text-accent hover:underline">
                          {readable(r.label)}
                        </Link>
                      ) : (
                        <span className="font-medium text-fg">{readable(r.label)}</span>
                      )}
                      {(r.who || r.detail) && (
                        <span className="mt-0.5 block text-[12px] text-muted">
                          {r.who}
                          {r.who && r.detail ? " · " : ""}
                          {r.detail && readable(r.detail)}
                        </span>
                      )}
                    </span>
                    <span className="flex shrink-0 items-center gap-2.5 text-[12px] text-muted">
                      {r.amount && <span className="font-medium tabular-nums text-fg">{formatMoney(r.amount.amount, r.amount.currency)}</span>}
                      {r.date && <span className="tabular-nums">{formatDate(r.date, { short: true })}</span>}
                      <SourceChips sources={r.sources} max={1} />
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
          {(s.more ?? 0) > 0 && <p className="mt-2 text-[12px] text-muted">{s.more} more not listed.</p>}
        </section>
        );
      })}
    </div>
  );
}
