import { describe, expect, it } from "vitest";
import { headline, staleLine } from "./home";
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
  it("says when the shown page was read, in Eastern time, and that a fresh one is coming", () => {
    const line = staleLine("2026-11-07T02:08:00Z", new Date("2026-11-07T02:38:00Z"));
    expect(line).toBe("Showing the page as of 9:08 p.m. ET (30 min ago). A fresh one is on its way.");
  });
  it("shows nothing for a fresh page or an unreadable time", () => {
    expect(staleLine(null)).toBeNull();
    expect(staleLine("nonsense")).toBeNull();
  });
});
