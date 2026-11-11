"use client";

import { CalendarClock, ListChecks, Video } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { getMyActionItems, getUpcomingMeetings } from "@/lib/api";
import { clock, dayTitle, peopleLine, startOf } from "@/components/meetings/calendar";
import { cn } from "@/lib/cn";
import { formatDate, safeHref } from "@/lib/format";
import type { ObjectPage, UpcomingMeeting } from "@/lib/types";
import { SectionTitle } from "./parts";

type ActionItem = {
  id: string;
  text: string;
  status: string;
  due?: string | null;
  owner?: { name?: string } | null;
  meeting?: string | null;
  records?: { object_id?: string; label?: string; type?: string }[];
};

/** The meetings ahead whose parties include this record (the reader's own calendar, as the
 * upcoming list reads it: a meeting the reader may not see never reaches here). */
export function meetingsWith(meetings: UpcomingMeeting[], objectId: string): UpcomingMeeting[] {
  return meetings.filter((m) => !m.private && (m.parties ?? []).some((p) => p.object_id === objectId));
}

/** The reader's open action items that name this record or one of its related records. */
export function itemsAbout(items: ActionItem[], ids: Set<string>): ActionItem[] {
  return items.filter((i) => i.status !== "done" && (i.records ?? []).some((r) => r.object_id && ids.has(r.object_id)));
}

/** A supplier's or customer's next meetings and open follow-ups, beside its orders: what is
 * coming up with them and what is still owed to or by them. Each list reads with the
 * reader's own grant; a list that cannot be read is left out, never guessed. */
export function PartyPulse({ obj }: { obj: ObjectPage }) {
  const [meetings, setMeetings] = useState<UpcomingMeeting[] | null>(null);
  const [items, setItems] = useState<ActionItem[] | null>(null);
  useEffect(() => {
    const ctl = new AbortController();
    getUpcomingMeetings(ctl.signal).then((r) => setMeetings(meetingsWith(r.meetings ?? [], obj.object_id)), () => setMeetings([]));
    getMyActionItems(null, ctl.signal).then(
      (r) => {
        const ids = new Set([obj.object_id, ...(obj.related ?? []).map((x) => x.object_id), ...(obj.customer?.open_list ?? []).map((x) => x.object_id), ...(obj.customer?.recent_orders ?? []).map((x) => x.object_id)]);
        setItems(itemsAbout((r.items ?? []) as ActionItem[], ids));
      },
      () => setItems([]),
    );
    return () => ctl.abort();
  }, [obj]);
  const now = new Date();
  if (!meetings?.length && !items?.length) return null;
  return (
    <div className="grid gap-x-10 gap-y-6 lg:grid-cols-2" data-testid="party-pulse">
      {meetings && meetings.length > 0 && (
        <section data-testid="party-meetings">
          <SectionTitle>Coming up with them</SectionTitle>
          <ul className="divide-y divide-border">
            {meetings.slice(0, 4).map((m) => {
              const s = startOf(m);
              const people = peopleLine(m);
              return (
                <li key={m.object.object_id} className="flex items-start gap-3 py-2 text-[13px]">
                  <CalendarClock className="mt-0.5 size-[15px] shrink-0 text-muted" strokeWidth={1.8} aria-hidden />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{m.object.label}</span>
                    <span className="block truncate text-[12px] text-muted">
                      {s ? (
                        <>
                          <span className={cn(dayTitle(s, now).startsWith("Today") && "text-accent")}>{dayTitle(s, now)}</span> · <span className="num">{clock(s)}</span>
                        </>
                      ) : (
                        "Time not set"
                      )}
                      {people.names.length > 0 && ` · ${people.names.join(", ")}${people.more ? ` +${people.more}` : ""}`}
                    </span>
                  </span>
                  {m.join_url && (
                    <a href={m.join_url} className="inline-flex shrink-0 items-center gap-1 text-[12px] text-accent hover:underline" target="_blank" rel="noreferrer">
                      <Video className="size-3.5" aria-hidden /> Join
                    </a>
                  )}
                  <Link href={safeHref(m.brief_url) ?? "/meetings"} className="shrink-0 text-[12px] font-medium hover:underline">
                    Prep
                  </Link>
                </li>
              );
            })}
          </ul>
        </section>
      )}
      {items && items.length > 0 && (
        <section data-testid="party-items">
          <SectionTitle>Still open from meetings</SectionTitle>
          <ul className="divide-y divide-border">
            {items.slice(0, 5).map((i) => (
              <li key={i.id} className="flex items-start gap-3 py-2 text-[13px]">
                <ListChecks className={cn("mt-0.5 size-[15px] shrink-0", i.status === "overdue" ? "text-late" : "text-muted")} strokeWidth={1.8} aria-hidden />
                <span className="min-w-0 flex-1">
                  <span className="block">{i.text}</span>
                  <span className="block truncate text-[12px] text-muted">
                    {[i.owner?.name, i.status === "overdue" ? "overdue" : i.due ? `due ${formatDate(i.due, { short: true })}` : null, i.meeting].filter(Boolean).join(" · ")}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
