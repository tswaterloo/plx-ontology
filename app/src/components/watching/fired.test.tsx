import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { AlertKinds, Briefing, BriefingItem } from "@/lib/types";
import type { WatchAlert } from "@/lib/types";
import { AlertFeed, FiredKind, firedKinds } from "./fired";

const item = (over: Partial<BriefingItem>) =>
  ({ rank: 1, id: "i1", event_type: "late_po", label: "Late PO", object_id: "PurchaseOrder:demo:90388", object_type: "PurchaseOrder", subject_label: "90388", summary: "No supplier confirmation", days: 5, new_since_visit: false, new_this_generation: false, open_url: "/po/90388", next_action: { text: "Ask the supplier to confirm a ship date", rule: "no_confirmation", contact: "dispatch@northwind.example", suggestion_only: true }, yours: true, first_seen: "2026-11-04T14:00:00Z", ...over }) as BriefingItem;

describe("what fired: each item with its next step", () => {
  it("puts the next step, whom to ask, whether it is yours and since when under the item", () => {
    render(<FiredKind f={{ kind: "late_po", label: "Late PO", on: true, count: 1, fresh: 0, top: [item({})], rest: [] }} />);
    const next = screen.getByTestId("fired-next");
    expect(next).toHaveTextContent("Ask the supplier to confirm a ship date · ask dispatch@northwind.example · yours · since 4 Nov");
  });

  it("offers to show a kind's items, never \"65 more\" when none is listed above", () => {
    const { rerender } = render(<FiredKind f={{ kind: "money", label: "Order or item losing money", on: true, count: 65, fresh: 0, top: [], rest: [{ type: "price_below_cost", label: "Customer price below cost", more: 65 }] }} />);
    expect(screen.getByRole("button", { name: "Show all 65: customer price below cost" })).toBeInTheDocument();
    rerender(<FiredKind f={{ kind: "money", label: "Order or item losing money", on: true, count: 66, fresh: 0, top: [item({})], rest: [{ type: "price_below_cost", label: "Customer price below cost", more: 65 }] }} />);
    expect(screen.getByRole("button", { name: "65 more: customer price below cost" })).toBeInTheDocument();
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

describe("a reply waiting: sender, subject and how long, in plain words", () => {
  const reply = (summary: string) =>
    item({ event_type: "needs_my_reply", object_type: "Email", object_id: "Email:x", subject_label: "Pricing for Q1", open_url: "/object/Email/x", days: 18, summary, next_action: undefined, yours: false, first_seen: null });
  it("says who sent it and which orders it names, never the date sentence or the recheck note", () => {
    render(<FiredKind f={{ kind: "reply", label: "Needs your reply", on: true, count: 3, fresh: 0, rest: [], top: [
      reply("A message of Wed 21 Oct from Acme Foods waits on your reply, 18 business days (2 SalesOrder named); asked 2 times"),
      { ...reply("SO 77120, SO 77121: a message of Wed 21 Oct waits on your reply, 18 business days; 1 past the requested date (not rechecked since the rules changed; last checked 2026-11-16 6:09 PM ET)"), id: "i2" },
      { ...reply("a Teams message of Sat 24 Oct waits on your reply, 16 business days (not rechecked since the rules changed; last checked 2026-11-16 6:09 PM ET)"), id: "i3" },
    ] }} />);
    const rows = screen.getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("Pricing for Q1From Acme Foods · asked 2 times18 d");
    expect(rows[1]).toHaveTextContent("Pricing for Q1about SO 77120, SO 77121 · 1 past the requested date18 d");
    expect(rows[2]).toHaveTextContent(/^Pricing for Q118 d$/);
    for (const t of ["waits on your reply", "Wed 21 Oct", "not rechecked", "business days"]) expect(document.body.textContent).not.toContain(t);
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

describe("an alert type whose check is not current", () => {
  const reg = { kinds: [{ kind: "late_po", label: "Late purchase orders" }], types: [{ type: "po_late", kind: "late_po", default: "on" }, { type: "lot_on_hold", kind: "late_po", default: "on" }] } as unknown as AlertKinds;
  const group = (over: object) => ({ event_type: "po_late", label: "Late purchase order", count: 0, in_top: 0, new_since_visit: 0, acknowledged: 0, ...over });
  it("says unavailable since its last check, with no old items, beside a current type's as-of", () => {
    const briefing = { status: "read", count: 1, items: [item({ event_type: "lot_on_hold", alert_kind: "late_po" })], groups: [
      group({ unavailable: true, unavailable_since: "2026-11-18T19:07:00Z" }),
      group({ event_type: "lot_on_hold", label: "Batch on quality hold", count: 1, in_top: 1, as_of: "2026-11-18T20:37:00Z" }),
    ] } as unknown as Briefing;
    const fired = firedKinds(reg, briefing);
    expect(fired).toHaveLength(1);
    render(<FiredKind f={fired[0]} />);
    expect(screen.getByTestId("unavailable-po_late")).toHaveTextContent(/^Late purchase order: unavailable since 18 Nov/);
    expect(screen.getByTestId("fired-as-of")).toHaveTextContent(/^as of 18 Nov/);
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });
  it("lists a kind whose only type is unavailable, never as nothing fired", () => {
    const briefing = { status: "read", count: 0, items: [], groups: [group({ unavailable: true, unavailable_since: null })] } as unknown as Briefing;
    const fired = firedKinds(reg, briefing);
    render(<FiredKind f={fired[0]} />);
    expect(screen.getByTestId("unavailable-po_late")).toHaveTextContent("Late purchase order: unavailable. Shown again after its next check.");
  });
});
