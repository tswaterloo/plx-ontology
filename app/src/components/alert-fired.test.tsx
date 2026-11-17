import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { BriefingItem } from "@/lib/types";
import { AlertFired } from "./alert-fired";

const item = (over: Partial<BriefingItem> = {}) =>
  ({
    rank: 1, id: "i1", event_type: "negative_margin", label: "Negative margin", object_id: "SalesOrder:demo:77120", object_type: "SalesOrder", subject_label: "77120",
    summary: "Order 77120 sells below its item cost", why: "Item cost rose 36% while the price stayed", days: 3, amount: { amount: "264.00", currency: "CAD" }, amount_basis: "order total",
    amount_disagree: true, lines_sum: { amount: "250.50", currency: "CAD" }, counterpart: "Glow Botanicals", orders_affected: 2, yours: true, why_yours: ["you are the account manager"],
    first_seen: "2026-11-04T14:00:00Z", new_since_visit: true, new_this_generation: false, open_url: "/so/77120", acknowledged: false, evidence: "live",
    next_action: { text: "Review the price", rule: "r", suggestion_only: true },
    sources: [{ system: "BC", handle: "bc:si:55120", label: "Sales invoice SI-55120" }], ...over,
  }) as BriefingItem;

describe("an alert opened from My work", () => {
  it("says what fired, the figures with their source, how long it has been open, whose it is and what it touches", () => {
    render(<AlertFired item={item()} now={new Date("2026-11-13T15:00:00Z")} />);
    const box = screen.getByTestId("alert-fired");
    expect(box).toHaveTextContent("AlertLosing money · new since your last visit");
    expect(screen.getByTestId("alert-what")).toHaveTextContent("Order 77120 sells below its item cost");
    expect(screen.getByTestId("alert-what")).toHaveTextContent("Sales invoice SI-55120");
    expect(box).toHaveTextContent("ReasonItem cost rose 36% while the price stayed");
    expect(screen.getByTestId("alert-figures")).toHaveTextContent("$264.00 CAD (order total)The order total and its lines disagree: lines sum to $250.50 CAD");
    expect(screen.getByTestId("alert-open")).toHaveTextContent("9 days · first seen 4 Nov · 3 days past its date");
    expect(screen.getByTestId("alert-owner")).toHaveTextContent("Yours: you are the account manager");
    expect(box).toHaveTextContent("TouchesGlow Botanicals · 2 customer orders");
  });

  it("says in words what is not on record and claims nothing it lacks", () => {
    render(<AlertFired item={item({ first_seen: null, yours: false, why_yours: [], amount: null, lines_sum: null, amount_disagree: false, why: null, counterpart: null, orders_affected: null, sources: [] })} />);
    expect(screen.getByTestId("alert-open")).toHaveTextContent("First seen not on record");
    expect(screen.getByTestId("alert-owner")).toHaveTextContent(/^WhoseNot routed to you$/);
    expect(screen.queryByTestId("alert-figures")).toBeNull();
    expect(screen.getByTestId("alert-fired")).not.toHaveTextContent("Touches");
  });

  it("does not claim who owns an alert when routing is not on record", () => {
    render(<AlertFired item={item({ yours: undefined, why_yours: [] })} />);
    expect(screen.getByTestId("alert-owner")).toHaveTextContent(/^WhoseOwner not on record$/);
  });
});
