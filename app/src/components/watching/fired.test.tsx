import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { BriefingItem } from "@/lib/types";
import type { WatchAlert } from "@/lib/types";
import { AlertFeed, FiredKind } from "./fired";

const item = (over: Partial<BriefingItem>) =>
  ({ rank: 1, id: "i1", event_type: "late_po", label: "Late PO", object_id: "PurchaseOrder:demo:90388", object_type: "PurchaseOrder", subject_label: "90388", summary: "No supplier confirmation", days: 5, new_since_visit: false, new_this_generation: false, open_url: "/po/90388", next_action: { text: "Ask the supplier to confirm a ship date", rule: "no_confirmation", contact: "dispatch@northwind.example", suggestion_only: true }, yours: true, first_seen: "2026-11-04T14:00:00Z", ...over }) as BriefingItem;

describe("what fired: each item with its next step", () => {
  it("puts the next step, whom to ask, whether it is yours and since when under the item", () => {
    render(<FiredKind f={{ kind: "late_po", label: "Late PO", on: true, count: 1, fresh: 0, top: [item({})], rest: [] }} />);
    const next = screen.getByTestId("fired-next");
    expect(next).toHaveTextContent("Ask the supplier to confirm a ship date · ask dispatch@northwind.example · yours · since 4 Nov");
  });

  it("adds no line when the item carries none of these", () => {
    render(<FiredKind f={{ kind: "late_po", label: "Late PO", on: true, count: 1, fresh: 0, top: [item({ next_action: undefined, yours: false, first_seen: null })], rest: [] }} />);
    expect(screen.queryByTestId("fired-next")).toBeNull();
  });

  it("says a late order's date as a day, not an ISO stamp", () => {
    render(<FiredKind f={{ kind: "late_po", label: "Late PO", on: true, count: 1, fresh: 0, top: [item({ summary: "An open purchase order needed by 2026-10-24 has no supplier confirmation" })], rest: [] }} />);
    expect(screen.getByText("Needed by 24 Oct; no supplier date.")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("2026-10-24");
    expect(document.body.innerHTML).not.toContain("2026-10-24");
  });
});

describe("alerts from rules and follows", () => {
  it("shows what changed, from what to what, the record it changed on and how many records a rule counted", () => {
    const a = {
      alert_id: "w1", at: "2026-11-12T15:00:00Z", kind: "linked_changed", origin: "follow", read: false, sentence: "PO 90412 changed",
      about: { object_id: "SalesOrder:demo:77120", type: "SalesOrder", label: "SO 77120" },
      record: { object_id: "PurchaseOrder:demo:90412", type: "PurchaseOrder", label: "PO 90412" },
      changes: [
        { property: "expected_date", before: "2026-10-17", after: "2026-11-04T12:00:00Z" },
        { property: "status", before: "Open", after: null },
        { property: "qty", before: "100", after: "80" },
        { property: "price", before: "1.59", after: "2.17" },
      ],
      counted: 3,
    } as WatchAlert;
    render(<AlertFeed alerts={[a]} />);
    const changes = screen.getByTestId("alert-changes");
    expect(changes).toHaveTextContent("expected date 17 Oct → 4 Nov");
    expect(changes).toHaveTextContent("status Open → cleared");
    expect(changes).toHaveTextContent("Show 1 more");
    expect(changes).not.toHaveTextContent("price");
    fireEvent.click(screen.getByRole("button", { name: "Show 1 more" }));
    expect(changes).toHaveTextContent("price 1.59 → 2.17");
    expect(changes.textContent).not.toContain("2026-");
    expect(screen.getByText(/Following SO 77120 · on PO 90412 · 3 records/)).toBeInTheDocument();
  });
});
