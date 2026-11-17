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
  // checks last run on older data than the records show: their items are held back, so
  // nothing is called "not late" until they re-run on the latest data
  const refreshing = Boolean(na.refreshing || briefing?.refreshing);
  if (late === 0 && refreshing) return { text: "Rechecking against the latest data.", sub: refreshingLine(na.checked_on ?? briefing?.checked_on) };
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

/** Why a list is held back: its checks last ran on older data and are re-running. */
export function refreshingLine(checkedOn?: string | null): string {
  const day = checkedOn ? new Date(checkedOn) : null;
  const when = day && !Number.isNaN(day.getTime())
    ? ` (last checked ${day.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "America/Toronto" }).replace("Sept", "Sep")})`
    : "";
  return `Some checks are re-running on the latest data${when}. Results appear here when they finish.`;
}

/**
 * A page served from the last good read because a fresh one did not come in time: "As of
 * 9:08 PM ET · refreshing". With only ``readAt`` (the reader's page read a moment ago,
 * served as kept): "As of 9:08 PM ET", as other people's decisions on it are as of then.
 */
export function staleLine(staleSince?: string | null, readAt?: string | null): string | null {
  const at = staleSince || readAt;
  const t = at ? new Date(at) : null;
  if (!t || Number.isNaN(t.getTime())) return null;
  const clock = t.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: "America/Toronto" });
  return staleSince ? `As of ${clock} ET · refreshing` : `As of ${clock} ET`;
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
