import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Brief } from "@/lib/types";
import { BriefView, deltaText, figureText, mondayBefore } from "./brief-view";

const brief = (over: Partial<Brief> = {}): Brief => ({
  kind: "weekly",
  title: "Weekly review, week of 2099-01-05",
  as_of: "2099-01-12",
  basis: "Counted from the records you may read.",
  sections: [
    { key: "figures", title: "This week", state: "shown", rows: [], more: 0, figures: [
      { label: "Orders booked", value: "12", unit: "records", previous: "9", delta: "+3", sources: [] },
      { label: "Invoiced (CAD)", value: "1200.50", unit: "money", currency: "CAD", sources: [] },
      { label: "Shipped on time", value: null, unit: "percent", sources: [] },
    ] },
    { key: "cash", title: "Cash position", state: "finance_marked", rows: [], more: 0, figures: [] },
    { key: "late_pos", title: "Late purchase orders", state: "shown", more: 4, figures: [], rows: [
      { label: "Late purchase order", detail: "PO 1 is 3 days late", sources: [] },
    ] },
  ],
  ...over,
});

describe("brief view", () => {
  it("shows each figure in its unit, a finance-marked section without values, and visible counts only", () => {
    render(<BriefView brief={brief()} eyebrow="Weekly review" />);
    expect(screen.getByText("12")).toBeTruthy();
    expect(screen.getByText("+3 vs 9 the week before")).toBeTruthy();
    expect(screen.getByText("Not readable")).toBeTruthy();
    expect(screen.getByText(/Finance-marked/)).toBeTruthy();
    expect(screen.getByText("4 more not listed.")).toBeTruthy();
  });

  it("words percents, money and the week picker's Mondays", () => {
    expect(figureText({ value: "92.5", unit: "percent", currency: null })).toBe("92.5%");
    expect(deltaText({ delta: "-2.5", previous: "95.0", unit: "percent", currency: null })).toBe("-2.5 pts vs 95.0% the week before");
    expect(deltaText({ delta: null, previous: "1", unit: "records", currency: null })).toBeNull();
    expect(mondayBefore(new Date(2026, 10, 8), 1)).toBe("2026-10-26");
    expect(mondayBefore(new Date(2026, 10, 9), 0)).toBe("2026-11-09");
  });

  it("lists lead-tier parties apart, linking to their own brief", () => {
    render(<BriefView brief={brief({ kind: "meeting", parties: [], possible_parties: [{ object_id: "Supplier:x:1", type: "Supplier", label: "Lead Co", open_url: "/object/Supplier/x" }] })} eyebrow="Meeting prep" />);
    expect(screen.getByText(/matched by email domain only/)).toBeTruthy();
    expect(screen.getByText("Lead Co").getAttribute("href")).toBe("/meetings/brief/Supplier/Supplier%3Ax%3A1");
  });
});
