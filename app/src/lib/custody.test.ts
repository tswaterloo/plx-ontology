import { describe, expect, it } from "vitest";
import { brokenScale, custodyOf, datesIn, dayLabel, dayOf } from "./custody";
import { dodge, niceMax } from "@/components/late-shape";
import type { ObjectPage } from "./types";

const src = (handle: string, system: "BC" | "Email" | "Teams" = "BC") => ({ handle, system, label: handle });

describe("chain of custody", () => {
  it("reads a bare date as its own day and a stamp as its Toronto day", () => {
    expect(dayLabel(dayOf("2026-10-17")!)).toBe("17 Oct");
    // 02:00Z on 1 Nov is still 31 Oct in Toronto
    expect(dayLabel(dayOf("2026-11-01T02:00:00Z")!)).toBe("31 Oct");
    expect(dayOf("0001-01-01")).toBeNull();
  });

  it("finds the dates a sentence names, in order", () => {
    const [a, b] = datesIn("Promise revised from 17 Oct 2026 to 4 Nov", 2026);
    expect([dayLabel(a), dayLabel(b)]).toEqual(["17 Oct", "4 Nov"]);
    expect(datesIn("from 2026-10-17 to 2026-11-04", 2026).map((d) => dayLabel(d))).toEqual(["17 Oct", "4 Nov"]);
  });

  it("puts each event in its system's track, the promise as it moved, and the late gap up to the data's day", () => {
    const obj = {
      object_id: "PurchaseOrder:1", type: "PurchaseOrder", label: "PO 1",
      freshness: { as_of: "2026-10-29T11:00:00Z" },
      trail: {
        kind: "purchase_order", title: "How we know", steps: [], materials: [], gaps: [], hidden_steps: 0,
        trails: [{
          subject: { object_id: "PurchaseOrder:1", type: "PurchaseOrder", label: "PO 1" }, state: "late", days_late: 12, hidden_steps: 0, may_hide_more: false, not_found: [],
          steps: [
            { kind: "due_date", at: "2026-10-17", title: "Needed by", check: "x" },
            { kind: "po_issued", at: "2026-09-26", title: "PO issued", check: "x", source: src("bc://po") },
            { kind: "revision", at: "2026-10-21T14:00:00Z", title: "Promise revised from 17 Oct 2026 to 4 Nov 2026", check: "x", source: src("mail://1", "Email") },
            { kind: "follow_up", at: "2026-10-27T14:00:00Z", title: "We asked", check: "x", source: src("mail://2", "Email") },
          ],
        }],
      },
      timeline: [{ at: "2026-10-22T10:00:00Z", system: "Teams", title: "Purchasing flags impact", source: src("teams://1", "Teams") }],
      linked: [{ at: "2026-10-21T14:00:00Z", kind: "email", label: "Delay notice", source: src("mail://1", "Email") }],
    } as unknown as ObjectPage;
    const c = custodyOf(obj, new Date("2026-11-05T15:00:00Z"))!;
    expect(c.events.map((e) => [e.track, e.mark])).toEqual([["BC", "issued"], ["Email", "promise"], ["Teams", "message"], ["Email", "message"]]);
    expect(c.tracks).toEqual(["BC", "Email", "Teams"]);
    expect(c.promises.map((p) => dayLabel(p.promised))).toEqual(["17 Oct", "4 Nov"]);
    // the data is as of 29 Oct: 12 days late, as the record says (not 19 to the calendar day)
    expect(c.stale).toBe(true);
    expect(c.gap && c.gap[1] - c.gap[0]).toBe(12);
  });

  it("draws a quiet stretch short and says how long it was", () => {
    const { x, breaks } = brokenScale([0, 2, 60, 62], 100);
    expect(breaks).toEqual([{ at: expect.any(Number), days: 58 }]);
    expect(x(60) - x(2)).toBeLessThan(80);
    expect(x(0)).toBe(0);
    expect(x(62)).toBe(100);
  });
});

describe("the late list, drawn", () => {
  it("ends the axis on a week (or a month past 90 days) and stacks dots that collide", () => {
    expect(niceMax(12)).toBe(14);
    expect(niceMax(134)).toBe(150);
    expect(dodge([10, 10, 10, 40]).map((d) => d.level)).toEqual([0, 1, 2, 0]);
    // a stack past its height continues in the column beside it
    expect(dodge([10, 10, 10], 9, 2)).toEqual([{ level: 0, shift: 0 }, { level: 1, shift: 0 }, { level: 0, shift: 1 }]);
  });
});
