// The chain of custody of one order, as a drawing's data: its dated events in tracks by the
// system that holds them, the needed-by date, today, the late gap and how the supplier's
// promise moved. Built only from what the record page already returns (the trail's steps,
// the activity timeline, linked messages); nothing is fetched or invented here.

import type { ObjectPage, Source, TrailStep } from "./types";

export type Track = "BC" | "Portal" | "Email" | "Teams" | "Other";
export type MarkKind = "issued" | "promise" | "lead" | "message" | "shipped" | "receipt" | "document" | "record";

export interface CustodyEvent {
  id: string;
  day: number; // calendar day number (days since 1970-01-01, Toronto)
  at: string; // the ISO stamp as given
  track: Track;
  mark: MarkKind;
  title: string;
  detail: string | null;
  source: Source | null;
}

export interface Promise_ {
  day: number; // when the promise was made
  promised: number; // the day it promised
  source: Source | null;
}

export interface Custody {
  /** True when "today" is the data's as-of day, earlier than the calendar day. */
  stale: boolean;
  events: CustodyEvent[];
  tracks: Track[];
  due: number | null;
  today: number;
  receipt: number | null;
  /** The late span: from needed-by to the receipt (if it came late) or to today. */
  gap: [number, number] | null;
  promises: Promise_[];
  min: number;
  max: number;
}

const TRACKS: Track[] = ["BC", "Portal", "Email", "Teams", "Other"];
const TZ = "America/Toronto";
const DAY = 86_400_000;

/** The calendar day an ISO date or stamp falls on: a bare date is its own day, a stamp its Toronto day. */
export function dayOf(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const s = iso.trim();
  if (/^0{3}1-01-01/.test(s)) return null;
  const bare = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (bare) return Math.floor(Date.UTC(+bare[1], +bare[2] - 1, +bare[3]) / DAY);
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return null;
  const [y, m, dd] = d.toLocaleDateString("en-CA", { timeZone: TZ }).split("-").map(Number);
  return Math.floor(Date.UTC(y, m - 1, dd) / DAY);
}

/** A day number as "17 Oct" (with the year when it isn't `thisYear`'s). */
export function dayLabel(day: number, thisYear?: number): string {
  const d = new Date(day * DAY);
  const y = d.getUTCFullYear();
  return d
    .toLocaleDateString("en-GB", { day: "numeric", month: "short", year: thisYear !== undefined && y !== thisYear ? "numeric" : undefined, timeZone: "UTC" })
    .replace("Sept", "Sep");
}

export function yearOf(day: number): number {
  return new Date(day * DAY).getUTCFullYear();
}

const MONTHS: Record<string, number> = { jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11 };

/** Every date a sentence names ("2026-10-17", "17 Oct 2026", "4 November"), in order, as day numbers. */
export function datesIn(text: string | null | undefined, year: number): number[] {
  if (!text) return [];
  const out: number[] = [];
  const re = /(\d{4})-(\d{2})-(\d{2})|\b(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)[a-z]*\.?(?: (\d{4}))?/gi;
  for (const m of text.matchAll(re)) {
    if (m[1]) out.push(Math.floor(Date.UTC(+m[1], +m[2] - 1, +m[3]) / DAY));
    else out.push(Math.floor(Date.UTC(m[6] ? +m[6] : year, MONTHS[m[5].slice(0, 3).toLowerCase()], +m[4]) / DAY));
  }
  return out;
}

function trackOf(system: string | null | undefined): Track {
  return system === "BC" || system === "Portal" || system === "Email" || system === "Teams" ? system : "Other";
}

function markOfStep(kind: TrailStep["kind"]): MarkKind | null {
  switch (kind) {
    case "po_issued":
      return "issued";
    case "confirmation":
    case "revision":
      return "promise";
    case "lead":
      return "lead";
    case "follow_up":
    case "communication":
      return "message";
    case "shipped":
      return "shipped";
    case "receipt":
      return "receipt";
    case "document":
      return "document";
    case "record_change":
    case "decision":
      return "record";
    default:
      return null; // due_date, verdict, claim: not events on the line
  }
}

function markOfTitle(title: string, track: Track): MarkKind {
  if (/receiv|receipt/i.test(title)) return "receipt";
  if (/\bship(ped|ment)?\b|dispatch/i.test(title)) return "shipped";
  if (/confirm|promis/i.test(title)) return "promise";
  if (/released|issued|posted|created|placed|opened/i.test(title)) return "issued";
  return track === "Email" || track === "Teams" ? "message" : "record";
}

/** The chain of custody of an order page, or null when it has fewer than two dated events. */
export function custodyOf(obj: ObjectPage, now: Date = new Date()): Custody | null {
  // "today" is the day the data is as of when that is earlier (a record read last week is
  // late by the days up to that read, as its page says), else the calendar day
  const asOf = dayOf(obj.freshness?.as_of);
  const calendar = dayOf(now.toISOString()) ?? Math.floor(now.getTime() / DAY);
  const today = asOf !== null && asOf < calendar ? asOf : calendar;
  const year = yearOf(today);
  const events: CustodyEvent[] = [];
  const seen = new Set<string>();
  const handles = new Set<string>();
  // a trail step is kept per kind; the timeline and linked messages add only what no step already shows
  const push = (e: CustodyEvent, fromTrail = false) => {
    const h = e.source?.handle;
    if (!fromTrail && h && handles.has(h)) return;
    const keys = [h ? `h:${h}:${e.mark}` : null, `t:${e.track}:${e.day}:${e.title.toLowerCase().slice(0, 40)}`].filter(Boolean) as string[];
    if (keys.some((k) => seen.has(k))) return;
    keys.forEach((k) => seen.add(k));
    if (h) handles.add(h);
    events.push(e);
  };
  let due: number | null = null;
  const promises: Promise_[] = [];
  const trails = obj.trail?.trails ?? [];
  const steps: TrailStep[] = [...(trails.length === 1 ? (trails[0].steps ?? []) : []), ...(obj.trail?.steps ?? [])];
  for (const [i, st] of steps.entries()) {
    if (st.kind === "due_date") {
      due = dayOf(st.at) ?? datesIn(st.title, year)[0] ?? due;
      continue;
    }
    const mark = markOfStep(st.kind);
    const day = dayOf(st.at);
    if (!mark || day === null) continue;
    const track = trackOf(st.source?.system);
    push({ id: `s${i}`, day, at: st.at!, track, mark, title: st.title, detail: st.detail ?? null, source: st.source ?? null }, true);
    if (st.kind === "revision" || st.kind === "confirmation") {
      const named = datesIn(`${st.title} ${st.detail ?? ""}`, year);
      // "revised from 17 Oct to 4 Nov": the first promise is where the line starts
      if (st.kind === "revision" && named.length > 1 && promises.length === 0) promises.push({ day, promised: named[0], source: st.source ?? null });
      const promised = named.length ? named[named.length - 1] : null;
      if (promised !== null && promises[promises.length - 1]?.promised !== promised) promises.push({ day, promised, source: st.source ?? null });
    }
  }
  for (const [i, ev] of (obj.timeline ?? []).entries()) {
    const day = dayOf(ev.at);
    if (day === null) continue;
    const track = trackOf(ev.system);
    push({ id: `t${i}`, day, at: ev.at!, track, mark: markOfTitle(ev.title, track), title: ev.title, detail: ev.summary ?? null, source: ev.source ?? null });
  }
  for (const [i, l] of (obj.linked ?? []).entries()) {
    const day = dayOf(l.at);
    if (day === null || (l.kind !== "email" && l.kind !== "teams")) continue;
    const track: Track = l.kind === "email" ? "Email" : "Teams";
    push({ id: `l${i}`, day, at: l.at!, track, mark: "message", title: l.label, detail: null, source: l.source });
  }
  // the order's own date ("Order date: Mar 28, 2026"), when no step says when it was placed
  if (!events.some((e) => e.mark === "issued")) {
    const f = (obj.facts ?? []).find((x) => /^(?:order|document) date\b/i.test(x.text));
    const said = f ? f.text.replace(/^[^:]*:\s*/, "") : null;
    const parsed = said ? new Date(said) : null;
    const day = parsed && !Number.isNaN(parsed.getTime()) ? Math.floor(Date.UTC(parsed.getFullYear(), parsed.getMonth(), parsed.getDate()) / DAY) : said ? (datesIn(said, year)[0] ?? null) : null;
    if (day !== null) push({ id: "ordered", day, at: new Date(day * DAY).toISOString().slice(0, 10), track: "BC", mark: "issued", title: "Order placed", detail: null, source: f?.sources?.[0] ?? null });
  }
  if (due === null) {
    const lineDue = (obj.late_lines ?? []).map((l) => dayOf(l.due_date)).filter((d): d is number => d !== null);
    if (lineDue.length) due = Math.min(...lineDue);
  }
  // one dated event is enough when the needed-by date makes the story (the late gap)
  if (events.length === 0 || (events.length < 2 && due === null)) return null;
  events.sort((a, b) => a.day - b.day || a.at.localeCompare(b.at));
  const receipt = events.filter((e) => e.mark === "receipt").map((e) => e.day).pop() ?? null;
  // a receipt closes the gap only when the order is no longer late (a part receipt doesn't)
  const stillLate = trails[0]?.state === "late" || obj.status?.state === "late";
  const end = receipt !== null && !stillLate ? receipt : today;
  const gap: [number, number] | null = due !== null && end > due ? [due, end] : null;
  promises.sort((a, b) => a.day - b.day);
  const days = [...events.map((e) => e.day), ...promises.map((p) => p.promised), today, ...(due !== null ? [due] : [])];
  const tracks = TRACKS.filter((l) => l !== "Other" || events.some((e) => e.track === "Other")).filter(
    (l) => l === "BC" || l === "Email" || events.some((e) => e.track === l),
  );
  return { stale: today < calendar, events, tracks, due, today, receipt, gap, promises, min: Math.min(...days) - 2, max: Math.max(...days) + 2 };
}

/**
 * A piecewise scale over day numbers: a quiet stretch longer than three weeks is drawn as a
 * short broken segment (its length printed), so the busy weeks get the room.
 */
export function brokenScale(stops: number[], width: number, quiet = 21, shown = 9): { x: (day: number) => number; breaks: { at: number; days: number }[] } {
  const pts = [...new Set(stops)].sort((a, b) => a - b);
  const seg: { a: number; b: number; len: number }[] = [];
  for (let i = 1; i < pts.length; i++) {
    const d = pts[i] - pts[i - 1];
    seg.push({ a: pts[i - 1], b: pts[i], len: d > quiet ? shown : d });
  }
  const total = seg.reduce((n, s) => n + s.len, 0) || 1;
  const k = width / total;
  const breaks: { at: number; days: number }[] = [];
  let acc = 0;
  const starts = seg.map((s) => {
    const st = acc;
    if (s.b - s.a > quiet) breaks.push({ at: (acc + s.len / 2) * k, days: s.b - s.a });
    acc += s.len;
    return st;
  });
  const x = (day: number) => {
    if (pts.length < 2) return width / 2;
    if (day <= pts[0]) return 0;
    if (day >= pts[pts.length - 1]) return width;
    const i = seg.findIndex((s) => day >= s.a && day <= s.b);
    const s = seg[i];
    return (starts[i] + ((day - s.a) / (s.b - s.a || 1)) * s.len) * k;
  };
  return { x, breaks };
}
