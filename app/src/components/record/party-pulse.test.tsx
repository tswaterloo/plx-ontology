import { describe, expect, it } from "vitest";
import type { UpcomingMeeting } from "@/lib/types";
import { itemsAbout, meetingsWith } from "./party-pulse";

const meeting = (id: string, parties: string[], over: Partial<UpcomingMeeting> = {}): UpcomingMeeting =>
  ({ object: { object_id: id, type: "CalendarEvent", label: id, open_url: "/" }, start: "2026-11-12T15:00:00Z", parties: parties.map((p) => ({ object_id: p, type: "Supplier", label: p, open_url: "/" })), brief_url: "/", sources: [], ...over }) as UpcomingMeeting;

describe("a party's pulse", () => {
  it("lists only meetings that name the party, never a private one", () => {
    const list = [meeting("a", ["Supplier:demo:maple-leaf"]), meeting("b", ["Supplier:demo:northwind"]), meeting("c", ["Supplier:demo:maple-leaf"], { private: true }), meeting("d", [])];
    expect(meetingsWith(list, "Supplier:demo:maple-leaf").map((m) => m.object.object_id)).toEqual(["a"]);
  });

  it("lists open action items that name the party or its records, never done ones", () => {
    const items = [
      { id: "1", text: "chase", status: "open", records: [{ object_id: "PurchaseOrder:demo:90412" }] },
      { id: "2", text: "done", status: "done", records: [{ object_id: "PurchaseOrder:demo:90412" }] },
      { id: "3", text: "other", status: "overdue", records: [{ object_id: "PurchaseOrder:demo:90388" }] },
      { id: "4", text: "none", status: "open" },
    ];
    expect(itemsAbout(items, new Set(["Supplier:demo:maple-leaf", "PurchaseOrder:demo:90412"])).map((i) => i.id)).toEqual(["1"]);
  });
});
