"use client";

import { Check, Clock3, ListChecks, Lock, MapPin, MessageCircle, Minus, Video, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { deltaText, figureText } from "@/components/brief-view";
import { SourceChips } from "@/components/source-chip";
import { Sheet } from "@/components/ui/sheet";
import { LoadProblem } from "@/components/ui/notice";
import { getMeetingBrief } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDate, formatMoney, objectHref, readable, safeHref } from "@/lib/format";
import type { Brief, BriefSection, MeetingInvitee, UpcomingMeeting } from "@/lib/types";
import { cleanMeetingAgenda, clock, dayTitle, durationText, endOf, minutes, relative, startOf, whenOf } from "./calendar";
import { PartyLink } from "./upcoming";

const ROLE: Record<MeetingInvitee["role"], string> = { organizer: "Organizer", required: "Required", optional: "Optional", resource: "Room" };

function Response({ r }: { r: MeetingInvitee["response"] }) {
  if (r === "accepted" || r === "organizer") {
    return (
      <span className="inline-flex items-center gap-1.5 text-[12px] font-medium text-ok">
        <Check className="size-3 shrink-0" strokeWidth={2.2} aria-hidden />
        {r === "organizer" ? "Organizer" : "Accepted"}
      </span>
    );
  }
  if (r === "declined") {
    return (
      <span className="inline-flex items-center gap-1.5 text-[12px] font-medium text-late">
        <X className="size-3 shrink-0" strokeWidth={2.2} aria-hidden />
        Declined
      </span>
    );
  }
  if (r === "tentativelyaccepted") {
    return (
      <span className="inline-flex items-center gap-1.5 text-[12px] font-medium text-risk">
        <Clock3 className="size-3 shrink-0" strokeWidth={2} aria-hidden />
        Tentative
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-[12px] font-medium text-muted">
      <Minus className="size-3 shrink-0" strokeWidth={2} aria-hidden />
      No reply
    </span>
  );
}

function H({ children, count }: { children: React.ReactNode; count?: number }) {
  return (
    <h3 className="mb-2 flex items-baseline gap-2 text-[12.5px] font-semibold text-fg">
      {children}
      {count != null && <span className="num font-normal text-muted">{count}</span>}
    </h3>
  );
}

export function MeetingSheet({ meeting: m, now, onClose }: { meeting: UpcomingMeeting | null; now: Date; onClose: () => void }) {
  const s = m ? startOf(m) : null;
  const e = m ? endOf(m) : null;
  const title = m ? (m.private ? "Private event" : m.object.label) : "Meeting";
  const people = m ? [m.organizer, ...(m.attendees ?? [])].filter((p): p is MeetingInvitee => !!p) : [];
  const accepted = people.filter((p) => p.response === "accepted" || p.response === "organizer").length;
  const agendaLines = m ? cleanMeetingAgenda(m.agenda) : [];
  const canJoin = !!(m && m.join_url && whenOf(m, now) !== "past");
  return (
    <Sheet
      open={!!m}
      onOpenChange={(o) => !o && onClose()}
      title={title}
      className="sm:w-[min(680px,calc(100vw-16px))]"
      footer={
        m && !m.private ? (
          <div className="flex flex-wrap items-center gap-2">
            {canJoin ? (
              <a
                href={safeHref(m.join_url) ?? undefined}
                target="_blank"
                rel="noreferrer"
                className="inline-flex h-8 items-center gap-1.5 rounded-md bg-fg px-3 text-[12.5px] font-medium text-bg transition-[opacity,transform] duration-150 hover:opacity-90 active:scale-[0.98]"
              >
                <Video className="size-3.5" strokeWidth={1.85} aria-hidden />
                Join in Teams
              </a>
            ) : null}
            <Link
              href={safeHref(m.brief_url) ?? "/meetings"}
              className={cn(
                "inline-flex h-8 items-center rounded-md px-3 text-[12.5px] font-medium transition-[background-color,opacity,transform] duration-150 active:scale-[0.98]",
                canJoin ? "text-fg hover:bg-hover" : "bg-fg text-bg hover:opacity-90",
              )}
            >
              Open full prep
            </Link>
            <Link
              href={`/ask?q=${encodeURIComponent(`Prepare me for the meeting "${m.object.label}"`)}&object=${encodeURIComponent(m.object.object_id)}&brief=${encodeURIComponent(m.object.type)}`}
              className="ml-auto inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-[12.5px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg active:scale-[0.98]"
            >
              <MessageCircle className="size-3.5" strokeWidth={1.85} aria-hidden />
              Ask about it
            </Link>
          </div>
        ) : undefined
      }
    >
      {m && (
        <div className="space-y-6 text-[13.5px]" data-testid="meeting-sheet">
          <div className="space-y-1.5">
            <p className="flex items-center gap-2 text-[17px] leading-6 font-semibold tracking-[-0.015em] text-fg">
              {m.private && <Lock className="size-4 text-muted" strokeWidth={1.8} aria-hidden />}
              {title}
            </p>
            <p className="num text-[13px] text-muted">
              {s ? dayTitle(s, now) : "Time not read"}
              {s && !m.all_day && ` · ${clock(s)}${e ? ` - ${clock(e)}` : ""}`}
              {m.all_day && " · all day"}
              {durationText(minutes(m)) && ` · ${durationText(minutes(m))}`}
              {relative(m, now) && <span className={cn("ml-1 font-medium", whenOf(m, now) === "now" ? "text-accent" : "text-live")}> · {relative(m, now)}</span>}
            </p>
            {(m.location || m.online) && (
              <p className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px]">
                {m.location && (
                  <span className="inline-flex items-center gap-1.5 text-fg">
                    <MapPin className="size-3.5 text-muted" strokeWidth={1.8} aria-hidden />
                    {m.location}
                  </span>
                )}
                {m.online &&
                  (m.join_url ? (
                    <a href={safeHref(m.join_url) ?? undefined} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 font-medium text-accent hover:underline">
                      <Video className="size-3.5" strokeWidth={1.8} aria-hidden />
                      Teams meeting
                    </a>
                  ) : (
                    <span className="inline-flex items-center gap-1.5 text-muted">
                      <Video className="size-3.5" strokeWidth={1.8} aria-hidden />
                      Online, no join link captured
                    </span>
                  ))}
              </p>
            )}
            <SourceChips sources={oneFieldEach(m.sources)} max={3} />
          </div>

          {m.private ? (
            <p className="border-t border-border pt-5 text-muted">A private or confidential event: only its time is shown.</p>
          ) : (
            <>
              {(m.parties ?? []).length > 0 && (
                <div className="border-t border-border pt-5">
                  <H>About</H>
                  <p className="flex flex-wrap gap-x-4 gap-y-1">
                    {(m.parties ?? []).map((p) => (
                      <PartyLink key={p.object_id} p={p} />
                    ))}
                  </p>
                </div>
              )}

              <div className="border-t border-border pt-5">
                <H>
                  {people.length > 0 ? (
                    <>
                      {people.length} {people.length === 1 ? "person" : "people"}
                      <span className="font-normal text-muted"> · {accepted} accepted</span>
                    </>
                  ) : (
                    "People"
                  )}
                </H>
                {m.people_withheld ? (
                  <p className="text-muted">Not shown right now. Try again in a few minutes.</p>
                ) : people.length === 0 ? (
                  <p className="text-muted">No attendee on the invitation.</p>
                ) : (
                  <table className="w-full text-left" data-testid="meeting-people">
                    <tbody className="divide-y divide-border">
                      {people.map((p) => (
                        <tr key={`${p.role}-${p.address ?? p.name}`} className="transition-colors duration-150 hover:bg-row-on">
                          <td className="py-2 pr-3">
                            {!p.outside && p.role !== "resource" && p.name ? (
                              <Link
                                href={`/team?person=${encodeURIComponent(p.name)}`}
                                onClick={onClose}
                                className="font-medium text-fg hover:text-accent hover:underline"
                                title={`Open ${p.name} on Team`}
                              >
                                {p.name}
                              </Link>
                            ) : (
                              <span className="font-medium text-fg">{p.name}</span>
                            )}
                            {p.address && <span className="block text-[12px] text-muted">{p.address}</span>}
                          </td>
                          <td className="py-2 pr-3 text-[12.5px] text-muted">
                            {ROLE[p.role]}
                            {p.outside && <span className="ml-1.5 text-fg">· outside</span>}
                          </td>
                          <td className="py-2 text-right">
                            <Response r={p.role === "organizer" ? "organizer" : p.response} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>

              {agendaLines.length > 0 && (
                <div className="border-t border-border pt-5">
                  <H>Agenda</H>
                  <p className="whitespace-pre-line leading-[22px] text-fg" data-testid="meeting-agenda">{agendaLines.join("\n")}</p>
                </div>
              )}

              {(m.open_items ?? []).length > 0 && (
                <div className="border-t border-border pt-5">
                  <H count={(m.open_items ?? []).length}>Open from earlier meetings</H>
                  <ul className="divide-y divide-border" data-testid="meeting-open-items">
                    {(m.open_items ?? []).map((i) => (
                      <li key={i.label} className="flex items-baseline justify-between gap-3 py-2 transition-colors duration-150 hover:bg-row-on">
                        <span className="min-w-0 text-fg">
                          <ListChecks className="mr-1.5 inline size-3.5 text-risk" strokeWidth={1.8} aria-hidden />
                          {i.label.replace(/^Action item: /, "")}
                          {i.detail && <span className="block pl-5 text-[12.5px] text-muted">{i.detail}</span>}
                        </span>
                        <span className="flex shrink-0 items-center gap-2 text-[12.5px] text-muted">
                          {i.date && <span className="num">{formatDate(i.date, { short: true })}</span>}
                          <SourceChips sources={i.sources} max={1} />
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <PrepPack meeting={m} />
            </>
          )}
        </div>
      )}
    </Sheet>
  );
}

function PrepPack({ meeting }: { meeting: UpcomingMeeting }) {
  const [brief, setBrief] = useState<Brief | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  useEffect(() => {
    const ctl = new AbortController();
    setBrief(null);
    setError(null);
    getMeetingBrief(meeting.object.type, meeting.object.object_id, ctl.signal).then(setBrief, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [meeting.object.type, meeting.object.object_id, round]);
  if (error) return <LoadProblem message={`Prep pack not read: ${plainError(error)}`} onRetry={() => setRound((n) => n + 1)} />;
  if (!brief) return <p className="border-t border-border pt-5 text-[12.5px] text-muted" role="status">Reading the prep pack</p>;
  const shown = brief.sections.filter((s) => (s.rows ?? []).length > 0 || (s.figures ?? []).length > 0);
  const quiet = brief.sections.filter((s) => !shown.includes(s) && s.state !== "finance_marked");
  return (
    <div className="space-y-5 border-t border-border pt-5" data-testid="meeting-prep">
      <p className="text-[12px] font-medium text-muted">Prep pack</p>
      {shown.map((s) => (
        <PrepSection key={s.key} s={s} />
      ))}
      {quiet.length > 0 && (
        <p className="text-[12.5px] text-muted">
          Nothing on {quiet.map((s) => s.title.toLowerCase()).join(", ")}
          {quiet.some((s) => s.state === "partial" || s.state === "unread" || s.state === "blocked") ? " (some not fully read)" : ""}.
        </p>
      )}
    </div>
  );
}

export function PrepSection({ s }: { s: BriefSection }) {
  return (
    <div>
      <H count={(s.rows ?? []).length + (s.more ?? 0)}>{s.title}</H>
      {(s.figures ?? []).length > 0 && (
        <dl className="divide-y divide-border" data-testid="prep-figures">
          {(s.figures ?? []).map((f, i) => {
            const delta = deltaText(f);
            const missing = f.value == null;
            return (
              <div key={`${f.label}-${i}`} className="flex items-baseline justify-between gap-3 py-2">
                <dt className="min-w-0 text-[13.5px] text-fg">
                  {f.label}
                  {f.note && <span className="block text-[12.5px] text-muted">{f.note}</span>}
                </dt>
                <dd className={missing ? "text-[12.5px] text-muted" : "num text-[13.5px] font-medium text-fg"}>
                  {figureText(f)}
                  {delta && <span className="block text-right text-[12px] font-normal text-muted">{delta}</span>}
                </dd>
              </div>
            );
          })}
        </dl>
      )}
      {(s.rows ?? []).length > 0 && (
        <ul className="divide-y divide-border">
          {(s.rows ?? []).map((r, i) => {
            const href = r.object ? safeHref(r.object.open_url) ?? objectHref(r.object.type, r.object.object_id) : null;
            return (
              <li key={`${r.label}-${i}`} className="flex items-baseline justify-between gap-3 py-2 transition-colors duration-150 hover:bg-row-on">
                <span className="min-w-0">
                  {href ? (
                    <Link href={href} className="font-medium text-fg hover:text-accent hover:underline">
                      {readable(r.label)}
                    </Link>
                  ) : (
                    <span className={s.key === "talking_points" ? "text-fg" : "font-medium text-fg"}>{readable(r.label)}</span>
                  )}
                  {(r.who || r.detail) && (
                    <span className="block text-[12.5px] text-muted">
                      {r.who}
                      {r.who && r.detail ? " · " : ""}
                      {r.detail && readable(r.detail)}
                    </span>
                  )}
                </span>
                <span className="flex shrink-0 items-center gap-2 text-[12.5px] text-muted">
                  {r.amount && <span className="num font-medium text-fg">{formatMoney(r.amount.amount, r.amount.currency)}</span>}
                  {r.date && <span className="num">{formatDate(r.date, { short: true })}</span>}
                  <SourceChips sources={r.sources} max={1} />
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {(s.more ?? 0) > 0 && <p className="mt-1.5 text-[12.5px] text-muted">{s.more} more in the full prep.</p>}
    </div>
  );
}

function oneFieldEach<T extends { handle: string }>(sources: T[] | null | undefined): T[] {
  const seen = new Set<string>();
  return (sources ?? []).filter((s) => {
    const record = s.handle.split("#")[0];
    if (seen.has(record)) return false;
    seen.add(record);
    return true;
  });
}
