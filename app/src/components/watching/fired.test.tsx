import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { BriefingItem } from "@/lib/types";
import { FiredKind } from "./fired";

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
});
