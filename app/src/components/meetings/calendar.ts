import type { UpcomingMeeting } from "@/lib/types";

/** A local calendar day key, YYYY-MM-DD, in the browser's own zone. */
export function dayKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function startOf(m: UpcomingMeeting): Date | null {
  const d = m.start ? new Date(m.start) : null;
  return d && !Number.isNaN(d.getTime()) ? d : null;
}

export function endOf(m: UpcomingMeeting): Date | null {
  const d = m.end ? new Date(m.end) : null;
  return d && !Number.isNaN(d.getTime()) ? d : null;
}

export interface Day {
  key: string;
  date: Date;
  meetings: UpcomingMeeting[];
}

/** Meetings grouped by local day, soonest first; a meeting without a readable start is dropped. */
export function byDay(meetings: UpcomingMeeting[]): Day[] {
  const days = new Map<string, Day>();
  for (const m of meetings) {
    const s = startOf(m);
    if (!s) continue;
    const key = dayKey(s);
    const day = days.get(key) ?? { key, date: new Date(s.getFullYear(), s.getMonth(), s.getDate()), meetings: [] };
    day.meetings.push(m);
    days.set(key, day);
  }
  const out = [...days.values()].sort((a, b) => a.key.localeCompare(b.key));
  for (const d of out) d.meetings.sort((a, b) => (startOf(a)?.getTime() ?? 0) - (startOf(b)?.getTime() ?? 0));
  return out;
}

/** The Monday of the week holding `d` (local). */
export function mondayOf(d: Date): Date {
  const out = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  out.setDate(out.getDate() - ((out.getDay() + 6) % 7));
  return out;
}

export function addDays(d: Date, n: number): Date {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
}

const TIME = new Intl.DateTimeFormat("en-CA", { hour: "numeric", minute: "2-digit", hour12: true });

/** "9:30 a.m." as "9:30 AM"; the locale's dotted meridiem is set plainly. */
export function clock(d: Date): string {
  return TIME.format(d).replace(/a\.m\./i, "AM").replace(/p\.m\./i, "PM");
}

/** Minutes between start and end; null when either is unreadable or the end comes first. */
export function minutes(m: UpcomingMeeting): number | null {
  const s = startOf(m);
  const e = endOf(m);
  if (!s || !e || e <= s) return null;
  return Math.round((e.getTime() - s.getTime()) / 60000);
}

export function durationText(mins: number | null): string | null {
  if (mins == null) return null;
  if (mins < 60) return `${mins} min`;
  const h = Math.floor(mins / 60);
  const r = mins % 60;
  return r ? `${h} h ${r}` : `${h} h`;
}

export type When = "past" | "now" | "soon" | "later";

/** Where a meeting sits against `now`: over, running, starting within the hour, later. */
export function whenOf(m: UpcomingMeeting, now: Date): When {
  const s = startOf(m);
  const e = endOf(m) ?? (s ? new Date(s.getTime() + 30 * 60000) : null);
  if (!s || !e) return "later";
  if (e <= now) return "past";
  if (s <= now) return "now";
  return s.getTime() - now.getTime() <= 60 * 60000 ? "soon" : "later";
}

/** "in 40 min", "in 3 h", "now", "ended"; days are said by the day header. */
export function relative(m: UpcomingMeeting, now: Date): string | null {
  const s = startOf(m);
  if (!s) return null;
  const w = whenOf(m, now);
  if (w === "past") return "ended";
  if (w === "now") return "now";
  const mins = Math.round((s.getTime() - now.getTime()) / 60000);
  if (mins < 60) return `in ${mins} min`;
  if (mins < 24 * 60) return `in ${Math.round(mins / 60)} h`;
  return null;
}

export function dayTitle(d: Date, now: Date): string {
  const today = dayKey(now);
  const key = dayKey(d);
  const name = d.toLocaleDateString("en-CA", { weekday: "long", month: "short", day: "numeric" });
  if (key === today) return `Today · ${name}`;
  if (key === dayKey(addDays(now, 1))) return `Tomorrow · ${name}`;
  return name;
}

/** People on the invitation (organizer first), as the list shows them: a few names then a count. */
export function peopleLine(m: UpcomingMeeting, shown = 3): { names: string[]; more: number } {
  const all = [m.organizer, ...(m.attendees ?? [])].filter((p): p is NonNullable<typeof p> => !!p && p.role !== "resource");
  return { names: all.slice(0, shown).map((p) => p.name), more: Math.max(0, all.length - shown) };
}

/** The outside organisations on the invitation, by their address domain. */
export function outsideDomains(m: UpcomingMeeting): string[] {
  const out = new Set<string>();
  for (const p of [m.organizer, ...(m.attendees ?? [])]) {
    if (p?.outside && p.address) out.add(p.address.split("@")[1]?.toLowerCase() ?? "");
  }
  out.delete("");
  return [...out];
}
