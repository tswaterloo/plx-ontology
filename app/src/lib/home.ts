import { plural } from "./format";
import type { Briefing, HomeResponse } from "./types";

/**
 * The headline: what is late and how many need a call today. "A call" = an item with
 * someone on record to chase; with none named, the top three.
 */
export function headline(home: HomeResponse | null, briefing: Briefing | null): { text: string; sub?: string } | null {
  const na = home?.needs_attention;
  if (briefing?.routing_basis === "no_department_on_record" && (briefing.items ?? []).length === 0)
    return { text: "No department on record for you, so nothing is routed to you." };
  if (!na || na.status !== "read") return null;
  const late = na.late_total ?? (na.late_pos ?? []).length;
  // the late-PO check last ran on older data or code than the records show and is
  // re-running: what it found then is shown (marked), so "none late" is said as of that check
  if (late === 0 && na.refreshing) return { text: "No late POs at the last check.", sub: lateCheckLine(na.checked_on) };
  if (late === 0) {
    const n = briefing?.count ?? 0;
    return { text: "No late POs.", sub: n > 0 ? `${plural(n, "other item")} open.` : undefined };
  }
  const items = briefing?.items ?? [];
  const named = items.filter((i) => i.next_action?.contact).length;
  const calls = named > 0 ? named : Math.min(3, items.length);
  const first = `${late} ${late === 1 ? "PO" : "POs"} late.`;
  if (!briefing || calls === 0) return { text: first };
  return { text: `${first} ${calls} to call today.` };
}

/** When a check last ran, in Eastern time ("17 Nov, 2:13 PM ET"); null when unknown. */
function checkedAt(checkedOn?: string | null): string | null {
  const day = checkedOn ? new Date(checkedOn) : null;
  if (!day || Number.isNaN(day.getTime())) return null;
  const date = day.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "America/Toronto" }).replace("Sept", "Sep");
  const clock = day.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: "America/Toronto" });
  return `${date}, ${clock} ET`;
}

/** Under "No late POs at the last check": when it ran and that a new run follows. */
export function lateCheckLine(checkedOn?: string | null): string {
  const at = checkedAt(checkedOn);
  return `${at ? `Checked ${at}. ` : ""}The check runs again on the newest data; a late PO it finds shows here.`;
}

/** An empty list while some checks re-run on newer data: nothing needed action as of their
 * last runs (what they found then is listed, marked), never a list still loading. */
export function refreshingLine(checkedOn?: string | null): string {
  const at = checkedAt(checkedOn);
  return `Nothing needs action as of the last checks${at ? ` (${at})` : ""}. Some run again on the newest data; what they find shows here.`;
}

/** A stale page younger than this says only that it is refreshing: the sidebar's "Data as of"
 * is the one time on the screen (two clocks a few minutes apart read as a contradiction). */
export const STALE_CLOCK_AFTER_MS = 60 * 60 * 1000;

/**
 * A page served from the last good read because a fresh one did not come in time:
 * "Refreshing" (the sidebar's "Data as of" says how new the data is); a read over an hour old
 * also says when: "Showing the read from 9:08 PM ET · refreshing". With only ``readAt`` (the
 * reader's page read a moment ago, served as kept): "As of 9:08 PM ET", as other people's
 * decisions on it are as of then.
 */
export function staleLine(staleSince?: string | null, readAt?: string | null, now: number = Date.now()): string | null {
  const at = staleSince || readAt;
  const t = at ? new Date(at) : null;
  if (!t || Number.isNaN(t.getTime())) return null;
  const clock = t.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: "America/Toronto" });
  if (!staleSince) return `As of ${clock} ET`;
  return now - t.getTime() < STALE_CLOCK_AFTER_MS ? "Refreshing" : `Showing the read from ${clock} ET · refreshing`;
}

/** The local hour from which Home opens with its "Today" list. */
export const MORNING_HOUR = 5;

/**
 * The "Today" list at the top of Home: from 5 AM local, the reader's own items of their
 * briefing (records they are named on, that their mail mentions, or whose supplier or
 * customer writes to them; the server ranks them first, read as the reader) and the
 * day's line. Null before 5 AM local or without a read briefing.
 */
export function yourMorning(
  briefing: Briefing | null,
  now: Date = new Date(),
  limit = 3,
): { line: string; items: NonNullable<Briefing["items"]>; yours: number } | null {
  if (!briefing || briefing.status !== "read" || now.getHours() < MORNING_HOUR) return null;
  const items = (briefing.items ?? []).filter((i) => i.yours).slice(0, limit);
  const yours = briefing.yours ?? 0;
  const line = yours > 0
    ? `${plural(yours, "item")} about your own orders, tasks and contacts.`
    : briefing.today || "Nothing of yours needs action this morning.";
  return { line, items, yours };
}

export const MORNING_NOTICE_HOUR = 8;
const MORNINGED = "plx.morning";

/**
 * The desktop app's one notice a day of the reader's morning (from 8 AM on the computer's
 * clock): what of their own the briefing holds and the top item, opening Today. Nothing when
 * the briefing is not read, nothing of theirs is in it, or it was already told today in
 * `store` (this app: once a day, never again on a restart).
 */
export function morningNotice(
  briefing: Briefing | null,
  store: Pick<Storage, "getItem" | "setItem"> | null,
  now: Date = new Date(),
): { id: string; title: string; body: string; href: string } | null {
  if (now.getHours() < MORNING_NOTICE_HOUR) return null;
  const day = now.toLocaleDateString("en-CA");
  try {
    if (store?.getItem(MORNINGED) === day) return null;
  } catch {
    return null;   // storage unreadable: say nothing rather than every minute
  }
  const morning = yourMorning(briefing, now, 1);
  if (!morning || morning.yours === 0) return null;
  try {
    store?.setItem(MORNINGED, day);
  } catch {
    return null;
  }
  const first = morning.items[0];
  const top = first ? [first.subject_label, first.label].filter(Boolean).join(" · ") : "";
  return { id: `morning:${day}`, title: "Your morning", body: top ? `${morning.line} First: ${top}` : morning.line, href: "/" };
}

/**
 * Who owns a key press on Today: "own" when the focus is a link, button, field, menu or open
 * dialog (it keeps its own Enter and arrows), "row" when it is a queue row's link (j/k and the
 * arrows still move the selection; Enter opens that row), "free" otherwise.
 */
export function keyOwner(t: EventTarget | null): "own" | "row" | "free" {
  if (!(t instanceof Element)) return "free";
  if (t.closest("[data-row-link]")) return "row";
  return t.closest("a,button,input,textarea,select,[contenteditable=''],[contenteditable='true'],[role='dialog'],[role='menu'],[role='listbox']") ? "own" : "free";
}
