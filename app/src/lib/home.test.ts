import { describe, expect, it } from "vitest";
import { headline, staleLine, yourMorning } from "./home";
import type { Briefing, BriefingItem, HomeResponse } from "./types";

const home = (late_total: number | null, status: "read" | "unavailable" = "read") =>
  ({ greeting_name: "V", examples: [], recent_questions: [], needs_attention: { status, late_total, late_pos: [] } }) as unknown as HomeResponse;
const it_ = (contact: string | null) => ({ next_action: { text: "x", rule: "r", contact, suggestion_only: true } }) as unknown as BriefingItem;
const brief = (items: BriefingItem[], extra: Partial<Briefing> = {}) => ({ status: "read", count: items.length, items, routing_basis: "employee", ...extra }) as Briefing;

describe("Home headline", () => {
  it("says what is late and how many need a call: those with someone to chase, else the top three", () => {
    expect(headline(home(12), brief([it_("Bob"), it_(null), it_("Ann")]))?.text).toBe("12 POs late. 2 to call today.");
    expect(headline(home(12), brief([it_(null), it_(null), it_(null), it_(null)]))?.text).toBe("12 POs late. 3 to call today.");
    expect(headline(home(1), null)?.text).toBe("1 PO late.");
  });

  it("says plainly when nothing is late, when nothing is routed, and claims nothing it could not read", () => {
    expect(headline(home(0), brief([it_(null)]))).toEqual({ text: "No late POs.", sub: "1 other item open." });
    expect(headline(home(5), brief([], { routing_basis: "no_department_on_record" }))?.text).toBe(
      "No department on record for you, so nothing is routed to you.",
    );
    expect(headline(home(5, "unavailable"), null)).toBeNull();
    expect(headline(null, null)).toBeNull();
  });

  it("never says nothing is late while the checks are re-running on newer data", () => {
    const na = { status: "read", late_total: 0, late_pos: [], refreshing: true, checked_on: "2026-11-01T08:14:36+00:00" };
    const h = headline({ greeting_name: "V", examples: [], recent_questions: [], needs_attention: na } as unknown as HomeResponse, brief([]));
    expect(h?.text).toBe("Rechecking against the latest data.");
    expect(h?.sub).toMatch(/re-running on the latest data \(last checked 1 Nov\)/);
    expect(headline(home(0), brief([], { refreshing: true }))?.text).toBe("Rechecking against the latest data.");
  });
});

describe("staleLine", () => {
  it("says when the shown page was read, in Eastern time, and that it is refreshing", () => {
    expect(staleLine("2026-11-07T02:08:00Z")).toBe("As of 9:08 PM ET · refreshing");
  });
  it("says when a kept page was read, without refreshing, when it is current", () => {
    expect(staleLine(null, "2026-11-07T02:08:00Z")).toBe("As of 9:08 PM ET");
    expect(staleLine("2026-11-07T02:08:00Z", "2026-11-07T03:00:00Z")).toBe("As of 9:08 PM ET · refreshing");
  });
  it("shows nothing for a fresh page or an unreadable time", () => {
    expect(staleLine(null)).toBeNull();
    expect(staleLine("nonsense")).toBeNull();
  });
});

describe("Your morning", () => {
  const mine = { ...it_(null), yours: true, why_yours: ["Mentioned in your mail"] } as unknown as BriefingItem;
  const b = brief([mine, it_(null)], { yours: 1, today: "2 new late POs." });
  it("opens Home from 5 AM local with the reader's own items, ranked first by the server", () => {
    const at = (h: number) => new Date(2026, 10, 7, h, 10);
    expect(yourMorning(b, at(4))).toBeNull();
    const m = yourMorning(b, at(5));
    expect(m?.items).toEqual([mine]);
    expect(m?.line).toBe("1 item about your own orders, tasks and contacts.");
  });
  it("falls back to the day's line when nothing is theirs, and claims nothing unread", () => {
    expect(yourMorning(brief([it_(null)], { yours: 0, today: "2 new late POs." }), new Date(2026, 10, 7, 8))?.line).toBe("2 new late POs.");
    expect(yourMorning(brief([], { status: "unavailable" } as Partial<Briefing>), new Date(2026, 10, 7, 8))).toBeNull();
    expect(yourMorning(null, new Date(2026, 10, 7, 8))).toBeNull();
  });
});
