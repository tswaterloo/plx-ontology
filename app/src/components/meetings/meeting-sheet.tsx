"use client";

import { Check, CircleHelp, ListChecks, Lock, MapPin, MessageCircle, Minus, Video, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { deltaText, figureText } from "@/components/brief-view";
import { SourceChips } from "@/components/source-chip";
import { Sheet } from "@/components/ui/sheet";
import { getMeetingBrief } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDate, formatMoney, objectHref, readable, safeHref } from "@/lib/format";
import type { Brief, BriefSection, MeetingInvitee, UpcomingMeeting } from "@/lib/types";
import { clock, dayTitle, durationText, endOf, minutes, relative, startOf, whenOf } from "./calendar";
import { PartyLink } from "./upcoming";

const ROLE: Record<MeetingInvitee["role"], string> = { organizer: "Organizer", required: "Required", optional: "Optional", resource: "Room" };

function Response({ r }: { r: MeetingInvitee["response"] }) {
  if (r === "accepted" || r === "organizer")
    return (
      <span className="inline-flex items-center gap-1 text-ok">
        <Check className="size-3.5" strokeWidth={2} aria-hidden />
        {r === "organizer" ? "Organizer" : "Accepted"}
      </span>
    );
  if (r === "declined")
    return (
      <span className="inline-flex items-center gap-1 text-late">
        <X className="size-3.5" strokeWidth={2} aria-hidden />
        Declined
      </span>
    );
  if (r === "tentativelyaccepted")
    return (
      <span className="inline-flex items-center gap-1 text-risk">
        <CircleHelp className="size-3.5" strokeWidth={1.8} aria-hidden />
        Tentative
      </span>
    );
  return (
    <span className="inline-flex items-center gap-1 text-faint">
      <Minus className="size-3.5" strokeWidth={1.8} aria-hidden />
      No reply
    </span>
  );
}

function H({ children, count }: { children: React.ReactNode; count?: number }) {
  return (
    <h3 className="mb-1.5 flex items-baseline gap-2 text-[12px] font-semibold tracking-[0.02em] text-muted uppercase">
      {children}
      {count != null && <span className="num font-normal normal-case">{count}</span>}
    </h3>
  );
}

/** One meeting in full: when and where, who (role and reply), the agenda, what it is about,
 * the action items still open from earlier meetings of the series, and its prep pack. */
export function MeetingSheet({ meeting: m, now, onClose }: { meeting: UpcomingMeeting | null; now: Date; onClose: () => void }) {
  const s = m ? startOf(m) : null;
  const e = m ? endOf(m) : null;
  const title = m ? (m.private ? "Private event" : m.object.label) : "Meeting";
  const people = m ? [m.organizer, ...(m.attendees ?? [])].filter((p): p is MeetingInvitee => !!p) : [];
  const accepted = people.filter((p) => p.response === "accepted" || p.response === "organizer").length;
  return (
    <Sheet
      open={!!m}
      onOpenChange={(o) => !o && onClose()}
      title={title}
      className="sm:w-[min(680px,calc(100vw-16px))]"
      footer={
        m && !m.private ? (
          <div className="flex flex-wrap items-center gap-2">
            <Link href={safeHref(m.brief_url) ?? "/meetings"} className="inline-flex h-8 items-center rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-3 text-[13px] font-medium hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)]">
              Open full prep
            </Link>
            {m.join_url && whenOf(m, now) !== "past" && (
              <a href={safeHref(m.join_url) ?? undefined} target="_blank" rel="noreferrer" className="inline-flex h-8 items-center gap-1.5 rounded-lg px-3 text-[13px] font-medium text-accent hover:bg-hover">
                <Video className="size-[15px]" strokeWidth={1.8} aria-hidden />
                Join in Teams
              </a>
            )}
            <Link
              href={`/ask?q=${encodeURIComponent(`Prepare me for the meeting "${m.object.label}"`)}&object=${encodeURIComponent(m.object.object_id)}`}
              className="ml-auto inline-flex h-8 items-center gap-1.5 rounded-lg px-3 text-[13px] text-muted hover:bg-hover hover:text-fg"
            >
              <MessageCircle className="size-[15px]" strokeWidth={1.8} aria-hidden />
              Ask about it
            </Link>
          </div>
        ) : undefined
      }
    >
      {m && (
        <div className="space-y-6 text-[13px]" data-testid="meeting-sheet">
          <div className="space-y-1.5">
            <p className="flex items-center gap-2 text-[17px] leading-6 font-semibold tracking-[-0.015em]">
              {m.private && <Lock className="size-4 text-muted" strokeWidth={1.8} aria-hidden />}
              {title}
            </p>
            <p className="num text-muted">
              {s ? dayTitle(s, now) : "Time not read"}
              {s && !m.all_day && ` · ${clock(s)}${e ? ` – ${clock(e)}` : ""}`}
              {m.all_day && " · all day"}
              {durationText(minutes(m)) && ` · ${durationText(minutes(m))}`}
              {relative(m, now) && <span className={cn("ml-1", whenOf(m, now) === "now" ? "text-accent" : "text-live")}> · {relative(m, now)}</span>}
            </p>
            {(m.location || m.online) && (
              <p className="flex flex-wrap items-center gap-x-4 gap-y-1">
                {m.location && (
                  <span className="inline-flex items-center gap-1.5">
                    <MapPin className="size-3.5 text-muted" strokeWidth={1.8} aria-hidden />
                    {m.location}
                  </span>
                )}
                {m.online &&
                  (m.join_url ? (
                    <a href={safeHref(m.join_url) ?? undefined} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 text-accent hover:underline">
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
            <p className="text-muted">A private or confidential event: only its time is shown.</p>
          ) : (
            <>
              {(m.parties ?? []).length > 0 && (
                <div>
                  <H>About</H>
                  <p className="flex flex-wrap gap-x-4 gap-y-1">
                    {(m.parties ?? []).map((p) => (
                      <PartyLink key={p.object_id} p={p} />
                    ))}
                  </p>
                </div>
              )}

              <div>
                <H count={people.length}>People{people.length > 0 && <span className="font-normal normal-case">· {accepted} accepted</span>}</H>
                {m.people_withheld ? (
                  <p className="text-muted">Not shown: the people exclusion list is not loaded.</p>
                ) : people.length === 0 ? (
                  <p className="text-muted">No attendee on the invitation.</p>
                ) : (
                  <table className="w-full text-left" data-testid="meeting-people">
                    <tbody className="divide-y divide-border">
                      {people.map((p) => (
                        <tr key={`${p.role}-${p.address ?? p.name}`}>
                          <td className="py-1.5 pr-3">
                            <span className="font-medium">{p.name}</span>
                            {p.address && <span className="block text-[12px] text-muted">{p.address}</span>}
                          </td>
                          <td className="py-1.5 pr-3 text-[12px] text-muted">
                            {ROLE[p.role]}
                            {p.outside && <span className="ml-1.5 text-fg">· outside</span>}
                          </td>
                          <td className="py-1.5 text-right text-[12px]">
                            <Response r={p.role === "organizer" ? "organizer" : p.response} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>

              {m.agenda && (
                <div>
                  <H>Agenda</H>
                  <p className="whitespace-pre-line leading-[21px]">{m.agenda}</p>
                </div>
              )}

              {(m.open_items ?? []).length > 0 && (
                <div>
                  <H count={(m.open_items ?? []).length}>Open from earlier meetings</H>
                  <ul className="divide-y divide-border" data-testid="meeting-open-items">
                    {(m.open_items ?? []).map((i) => (
                      <li key={i.label} className="flex items-baseline justify-between gap-3 py-1.5">
                        <span className="min-w-0">
                          <ListChecks className="mr-1.5 inline size-3.5 text-risk" strokeWidth={1.8} aria-hidden />
                          {i.label.replace(/^Action item: /, "")}
                          {i.detail && <span className="block pl-5 text-[12px] text-muted">{i.detail}</span>}
                        </span>
                        <span className="flex shrink-0 items-center gap-2 text-[12px] text-muted">
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

/** The meeting's prep brief, read when the sheet opens: each section with rows, then one line
 * naming the sections with nothing in them. */
function PrepPack({ meeting }: { meeting: UpcomingMeeting }) {
  const [brief, setBrief] = useState<Brief | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    const ctl = new AbortController();
    setBrief(null);
    setError(null);
    getMeetingBrief(meeting.object.type, meeting.object.object_id, ctl.signal).then(setBrief, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [meeting.object.type, meeting.object.object_id]);
  if (error) return <p className="text-[12px] text-muted">Prep pack not read: {plainError(error)}</p>;
  if (!brief) return <p className="text-[12px] text-muted" role="status">Reading the prep pack</p>;
  const shown = brief.sections.filter((s) => (s.rows ?? []).length > 0 || (s.figures ?? []).length > 0);
  const quiet = brief.sections.filter((s) => !shown.includes(s) && s.state !== "finance_marked");
  return (
    <div className="space-y-5 border-t border-border pt-5" data-testid="meeting-prep">
      <p className="text-[12px] font-semibold tracking-[0.02em] text-muted uppercase">Prep pack</p>
      {shown.map((s) => (
        <PrepSection key={s.key} s={s} />
      ))}
      {quiet.length > 0 && (
        <p className="text-[12px] text-muted">
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
              <div key={`${f.label}-${i}`} className="flex items-baseline justify-between gap-3 py-1.5">
                <dt className="min-w-0 text-[13px]">
                  {f.label}
                  {f.note && <span className="block text-[12px] text-muted">{f.note}</span>}
                </dt>
                <dd className={missing ? "text-[12px] text-muted" : "num text-[13px] font-medium"}>
                  {figureText(f)}
                  {delta && <span className="block text-right text-[12px] font-normal text-muted">{delta}</span>}
                </dd>
              </div>
            );
          })}
        </dl>
      )}
      <ul className="divide-y divide-border">
        {(s.rows ?? []).map((r, i) => {
          const href = r.object ? safeHref(r.object.open_url) ?? objectHref(r.object.type, r.object.object_id) : null;
          return (
            <li key={`${r.label}-${i}`} className="flex items-baseline justify-between gap-3 py-1.5">
              <span className="min-w-0">
                {href ? (
                  <Link href={href} className="font-medium hover:underline">
                    {readable(r.label)}
                  </Link>
                ) : (
                  <span className={s.key === "talking_points" ? "" : "font-medium"}>{readable(r.label)}</span>
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
      {(s.more ?? 0) > 0 && <p className="mt-1 text-[12px] text-muted">{s.more} more in the full prep.</p>}
    </div>
  );
}

/** The event's sources once per record: its start, body and attendees are fields of one invite. */
function oneFieldEach<T extends { handle: string }>(sources: T[] | null | undefined): T[] {
  const seen = new Set<string>();
  return (sources ?? []).filter((s) => {
    const record = s.handle.split("#")[0];
    if (seen.has(record)) return false;
    seen.add(record);
    return true;
  });
}
