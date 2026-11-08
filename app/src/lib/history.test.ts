import { describe, expect, it } from "vitest";
import { buildHistory, facets, filterHistory, kindOf } from "./history";
import type { LinkedItem, Source, TimelineEvent } from "./types";

const src = (system: Source["system"], handle: string): Source => ({ system, handle, label: handle });

describe("cross-system history", () => {
  const events: TimelineEvent[] = [
    { at: "2026-10-01T10:00:00Z", system: "BC", title: "Order released" },
    { at: "2026-10-05T10:00:00Z", system: "BC", title: "Shipment posted" },
    { at: "2026-10-09T10:00:00Z", system: "BC", title: "Invoice posted" },
    { at: "2026-10-20T10:00:00Z", system: "Portal", title: "Payment received" },
    { at: "2026-10-21T10:00:00Z", system: "Email", title: "Customer asks for status", source: src("Email", "mail://1") },
    { at: null, system: "BC", title: "Order released" },
  ];
  const linked: LinkedItem[] = [
    { kind: "email", label: "Customer asks for status", at: "2026-10-21T10:00:00Z", source: src("Email", "mail://1") },
    { kind: "teams", label: "#sales thread", at: "2026-10-22T08:00:00Z", source: src("Teams", "teams://1") },
    { kind: "task", label: "Call the buyer", at: "2026-10-23T08:00:00Z", source: src("Portal", "portal://t1") },
  ];

  it("merges events and linked items newest first, dedupes the same source, undated last", () => {
    const rows = buildHistory(events, linked);
    expect(rows.map((r) => r.title)).toEqual(["Call the buyer", "#sales thread", "Customer asks for status", "Payment received", "Invoice posted", "Shipment posted", "Order released", "Order released"]);
    expect(rows[rows.length - 1].at).toBeNull();
  });

  it("kinds: orders, shipments, invoices, payments, mail, Teams, meetings, tasks", () => {
    expect(kindOf("BC", "Order released")).toBe("Orders");
    expect(kindOf("BC", "Shipment posted")).toBe("Shipments");
    expect(kindOf("BC", "Invoice posted")).toBe("Invoices");
    expect(kindOf("Portal", "Payment received")).toBe("Payments");
    expect(kindOf("Email", "anything")).toBe("Mail");
    expect(kindOf("Teams", "Weekly sync meeting")).toBe("Meetings");
    expect(kindOf("Teams", "hello")).toBe("Teams");
    expect(kindOf("BC", "Something odd")).toBe("Other");
  });

  it("filters by system and kind; an empty filter shows everything", () => {
    const rows = buildHistory(events, linked);
    const none = { systems: new Set<never>(), kinds: new Set<never>() };
    expect(filterHistory(rows, none)).toHaveLength(rows.length);
    expect(filterHistory(rows, { systems: new Set(["Email"] as const), kinds: new Set() })).toHaveLength(1);
    expect(filterHistory(rows, { systems: new Set(), kinds: new Set(["Invoices", "Payments"] as const) }).map((r) => r.kind)).toEqual(["Payments", "Invoices"]);
    const f = facets(rows);
    expect(f.kinds).toContain("Tasks");
    expect(f.systems).toEqual([...f.systems].sort());
  });

  it("an empty record has no rows and N=1/10/60 rows keep every row", () => {
    expect(buildHistory([], [])).toEqual([]);
    for (const n of [1, 10, 60]) {
      const many: TimelineEvent[] = Array.from({ length: n }, (_, i) => ({ at: `2026-10-${String((i % 28) + 1).padStart(2, "0")}T00:00:00Z`, system: "BC", title: `Event ${i}` }));
      expect(buildHistory(many, [])).toHaveLength(n);
    }
  });
});
