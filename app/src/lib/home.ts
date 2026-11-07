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
    ? ` (last checked ${day.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "America/Toronto" })})`
    : "";
  return `Some checks are re-running on the latest data${when}. Results appear here when they finish.`;
}

/** A page served from the last good read because a fresh one did not come in time. */
export function staleLine(staleSince?: string | null, now: Date = new Date()): string | null {
  const t = staleSince ? new Date(staleSince) : null;
  if (!t || Number.isNaN(t.getTime())) return null;
  const mins = Math.max(0, Math.round((now.getTime() - t.getTime()) / 60000));
  const age = mins < 1 ? "just now" : mins < 60 ? `${mins} min ago` : `${Math.round(mins / 60)} h ago`;
  const clock = t.toLocaleTimeString("en-CA", { hour: "numeric", minute: "2-digit", timeZone: "America/Toronto" });
  return `Showing the page as of ${clock} ET (${age}). A fresh one is on its way.`;
}
