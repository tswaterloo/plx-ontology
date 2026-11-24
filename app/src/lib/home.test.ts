import { describe, expect, it } from "vitest";
import { headline, keyOwner, morningNotice, refreshingLine, staleLine, yourMorning } from "./home";
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

  it("says nothing is late only as of the last late-PO check while it re-runs, with its time", () => {
    const na = { status: "read", late_total: 0, late_pos: [], refreshing: true, checked_on: "2026-11-01T08:14:36+00:00" };
    const h = headline({ greeting_name: "V", examples: [], recent_questions: [], needs_attention: na } as unknown as HomeResponse, brief([]));
    expect(h?.text).toBe("No late POs at the last check.");
    expect(h?.sub).toBe("Checked 1 Nov, 3:14 AM ET. The check runs again on the newest data; a late PO it finds shows here.");
    // owner walk 11-17: another rule re-running never holds the late line (it read as loading for days)
    expect(headline(home(0), brief([], { refreshing: true }))?.text).toBe("No late POs.");
    // an unknown time is left out, never invented
    expect(headline({ greeting_name: "V", examples: [], recent_questions: [], needs_attention: { ...na, checked_on: null } } as unknown as HomeResponse, brief([]))?.sub).toBe(
      "The check runs again on the newest data; a late PO it finds shows here.",
    );
  });

  it("an empty list while checks re-run says nothing needed action as of their last runs", () => {
    expect(refreshingLine("2026-11-17T06:01:50Z")).toBe("Nothing needs action as of the last checks (17 Nov, 1:01 AM ET). Some run again on the newest data; what they find shows here.");
    expect(refreshingLine(null)).toBe("Nothing needs action as of the last checks. Some run again on the newest data; what they find shows here.");
  });
});

describe("staleLine", () => {
  const read = "2026-11-07T02:08:00Z";
  const at = (min: number) => new Date(read).getTime() + min * 60_000;
  it("says only that a recent stale page is refreshing: the sidebar's Data as of is the one clock", () => {
    expect(staleLine(read, null, at(4))).toBe("Refreshing");
  });
  it("names the read's time, in Eastern time, once it is over an hour old", () => {
    expect(staleLine(read, null, at(61))).toBe("Showing the read from 9:08 PM ET · refreshing");
  });
  it("says when a kept page was read, without refreshing, when it is current", () => {
    expect(staleLine(null, read)).toBe("As of 9:08 PM ET");
    expect(staleLine(read, "2026-11-07T03:00:00Z", at(90))).toBe("Showing the read from 9:08 PM ET · refreshing");
  });
  it("shows nothing for a fresh page or an unreadable time", () => {
    expect(staleLine(null)).toBeNull();
    expect(staleLine("nonsense")).toBeNull();
  });
});

describe("Today list", () => {
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

describe("morningNotice (the desktop app's once-a-morning notice)", () => {
  const mine = { ...it_(null), yours: true, subject_label: "PO 4411", label: "Late" } as unknown as BriefingItem;
  const b = brief([mine], { yours: 1 });
  const store = () => {
    const m = new Map<string, string>();
    return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v) };
  };
  it("tells the reader's own morning once a day from 8 AM, opening Today", () => {
    const s = store();
    expect(morningNotice(b, s, new Date(2026, 10, 16, 7, 59))).toBeNull();
    const n = morningNotice(b, s, new Date(2026, 10, 16, 8, 1));
    expect(n).toEqual({ id: "morning:2026-11-16", title: "Your morning", body: "1 item about your own orders, tasks and contacts. First: PO 4411 · Late", href: "/" });
    expect(morningNotice(b, s, new Date(2026, 10, 16, 9))).toBeNull();
    expect(morningNotice(b, s, new Date(2026, 10, 17, 8, 5))?.id).toBe("morning:2026-11-17");
  });
  it("says nothing when nothing is theirs, the briefing is unread, or storage fails", () => {
    expect(morningNotice(brief([it_(null)], { yours: 0, today: "x" }), store(), new Date(2026, 10, 16, 9))).toBeNull();
    expect(morningNotice(null, store(), new Date(2026, 10, 16, 9))).toBeNull();
    const broken = { getItem: () => { throw new Error("off"); }, setItem: () => undefined };
    expect(morningNotice(b, broken, new Date(2026, 10, 16, 9))).toBeNull();
  });
});

describe("keyOwner (WEB-015: Enter on an unrelated link is that link's)", () => {
  const el = (html: string, sel: string) => {
    document.body.innerHTML = html;
    return document.querySelector(sel);
  };
  it("leaves links, buttons, fields and dialogs their own keys; rows and the page are Today's", () => {
    expect(keyOwner(el('<nav><a href="/team" id="x">Team</a></nav>', "#x"))).toBe("own");
    expect(keyOwner(el('<button id="x">Go</button>', "#x"))).toBe("own");
    expect(keyOwner(el('<input id="x">', "#x"))).toBe("own");
    expect(keyOwner(el('<div role="dialog"><span id="x">in a dialog</span></div>', "#x"))).toBe("own");
    expect(keyOwner(el('<a data-row-link href="/po/1" id="x"><span id="y">row</span></a>', "#y"))).toBe("row");
    expect(keyOwner(el('<div id="x">page</div>', "#x"))).toBe("free");
    expect(keyOwner(document.body)).toBe("free");
    expect(keyOwner(null)).toBe("free");
  });
});
