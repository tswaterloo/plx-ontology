import { plural } from "./format";
import type { Briefing, HomeResponse } from "./types";

/**
 * The headline: what is late and how many need a call today. "A call" = an item with
 * someone on record to chase; with none named, the top three.
 */
export function headline(home: HomeResponse | null, briefing: Briefing | null): { text: string; sub?: string } | null {
  const na = home?.needs_attention;
  if (briefing?.routing_basis === "no_department_on_record" && (briefing.items ?? []).length === 0)
    return { text: "No department is on record for you, so nothing is routed to you yet." };
  if (!na || na.status !== "read") return null;
  const late = na.late_total ?? (na.late_pos ?? []).length;
  if (late === 0) {
    const n = briefing?.count ?? 0;
    return { text: "Nothing is late today.", sub: n > 0 ? `${plural(n, "item")} to look at this week.` : undefined };
  }
  const items = briefing?.items ?? [];
  const named = items.filter((i) => i.next_action?.contact).length;
  const calls = named > 0 ? named : Math.min(3, items.length);
  const first = `${late} purchase ${late === 1 ? "order is" : "orders are"} late.`;
  if (!briefing || calls === 0) return { text: first };
  return { text: `${first} ${calls} ${calls === 1 ? "needs" : "need"} a call today.` };
}
