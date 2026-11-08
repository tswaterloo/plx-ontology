import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import * as api from "@/lib/api";
import type { Briefing, BriefingItem } from "@/lib/types";
import { BriefingCard, BriefingRow, itemFacts } from "./briefing";

const item = (over: Partial<BriefingItem>): BriefingItem => ({
  rank: 1,
  id: "pe-0000000000000001",
  event_type: "po_late",
  label: "Late purchase order",
  evidence: "live",
  object_id: "PurchaseOrder:x:1",
  object_type: "PurchaseOrder",
  subject_label: "PO 100001",
  summary: "Past its requested-by date 2099-01-15",
  why: "Open past the requested-by date.",
  next_action: { text: "Ask the supplier for a confirmed delivery date.", rule: "late_ask_supplier", suggestion_only: true },
  acknowledged: false,
  amount_disagree: false,
  new_since_visit: false,
  new_this_generation: false,
  yours: false,
  why_yours: [],
  open_url: "/object/PurchaseOrder/PurchaseOrder%3Ax%3A1",
  sources: [],
  ...over,
});

const briefing: Briefing = {
  status: "read",
  count: 2,
  refreshing: false,
  counts_exact: true,
  since_visit: false,
  acknowledged: 0,
  carried: 0,
  new_since_visit: 1,
  other_teams: 3,
  hidden: 2,
  rank_basis: "Ranked by dollars at risk.",
  routing_basis: "employee",
  departments: ["PROC"],
  complete: true,
  more: false,
  items: [
    item({ amount: { amount: "9000.00", currency: "USD" }, amount_basis: "total_cost", days: 5, new_since_visit: true }),
    item({ rank: 2, id: "pe-0000000000000002", subject_label: "PO 100002" }),
  ],
  groups: [{ event_type: "po_late", label: "Late purchase order", count: 2, in_top: 2, new_since_visit: 1, acknowledged: 0 }],
  checks: [],
  hidden_by_type: { po_late: 2 },
  today: "In the latest check: 1 PO went late.",
  digest_preview: "Hello Synth,\n\n2 items need your attention.\n\n(Preview only: this digest is not sent.)",
  digest_sent: false,
  yours: 0,
  involvement: "no_identity",
};

describe("BriefingCard", () => {
  it("states the count, the rank basis and every top item with its next action and open link", () => {
    render(<BriefingCard briefing={briefing} />);
    expect(screen.getByTestId("briefing-today").textContent).toBe("Today: In the latest check: 1 PO went late.");
    expect(screen.getByTestId("briefing-heading").textContent).toContain("2 items need attention");
    // the rank basis sits behind "How this is ranked", not printed on the page
    expect(screen.queryByText("Ranked by dollars at risk.")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /How this is ranked/ }));
    expect(screen.getByText("Ranked by dollars at risk.")).toBeTruthy();
    const items = screen.getAllByTestId("briefing-item");
    expect(items).toHaveLength(2);
    expect(within(items[0]).getByText(/9,000.00/)).toBeTruthy();
    // the next step lives on the record page: never a "Suggested next step" box per row
    expect(items[0].textContent).not.toContain("Ask the supplier for a confirmed delivery date.");
    expect(within(items[0]).queryByTestId("next-action")).toBeNull();
    expect(within(items[0]).getByText("Late PO")).toBeTruthy();
    expect(within(items[0]).getByRole("link", { name: /^Open/ }).getAttribute("href")).toBe(
      "/object/PurchaseOrder/PurchaseOrder%3Ax%3A1?focus=chase",
    );
    // hidden items are a count only, per type
    expect(screen.getByTestId("briefing-counts").textContent).toContain(
      "Late purchase order: 2 more for your team are in places you can't open.",
    );
    expect(screen.getByTestId("briefing-counts").textContent).toContain("3 more items you may see are routed to other teams.");
  });

  it("says 'Chase {supplier}' only when there is someone on record to chase", () => {
    const withContact = item({ next_action: { text: "Ask Bob.", rule: "r", contact: "Bob <bob@vendor.example>", suggestion_only: true } });
    const po = { object_id: "PurchaseOrder:x:1", label: "100001", supplier: "Acme Supply", days_late: 5, open_url: "/o/1", reason: "" };
    const { rerender } = render(<ol><BriefingRow item={withContact} latePo={po} /></ol>);
    expect(screen.getByRole("link", { name: /Chase Acme Supply/ })).toBeTruthy();
    expect(screen.getByTestId("briefing-item")).toHaveTextContent("PO 100001Acme Supply");
    rerender(<ol><BriefingRow item={item({})} latePo={po} /></ol>);
    expect(screen.queryByRole("link", { name: /Chase/ })).toBeNull();
    expect(screen.getByRole("link", { name: /^Open/ })).toBeTruthy();
  });

  it("shows the top items, then every other one in its type's group; +N more opens the list", async () => {
    const many: Briefing = {
      ...briefing,
      count: 40,
      items: Array.from({ length: 7 }, (_, n) => item({ rank: n + 1, id: `pe-top${n}`, subject_label: `PO ${n}` })),
      groups: [
        { event_type: "po_late", label: "Late purchase order", count: 9, in_top: 7, new_since_visit: 0, acknowledged: 1 },
        { event_type: "lot_near_expiry", label: "Lot near expiry", count: 31, in_top: 0, new_since_visit: 0, acknowledged: 0 },
      ],
    };
    const loadItems = vi.fn(async (eventType: string, offset: number) => ({
      event_type: eventType as BriefingItem["event_type"],
      total: 31,
      offset,
      more: offset === 0,
      items: Array.from({ length: offset === 0 ? 25 : 6 }, (_, n) =>
        item({ rank: 8 + offset + n, id: `pe-lot${offset + n}`, event_type: "lot_near_expiry", subject_label: `Lot ${offset + n}` }),
      ),
    }));
    render(<BriefingCard briefing={many} loadItems={loadItems} />);
    expect(screen.getAllByTestId("briefing-item")).toHaveLength(7);
    const groups = screen.getAllByTestId("briefing-group");
    expect(groups).toHaveLength(2);
    expect(within(groups[0]).getByText("Show 2 more")).toBeTruthy();
    expect(within(groups[0]).getByText("1 acknowledged")).toBeTruthy();
    fireEvent.click(within(groups[1]).getByRole("button", { name: "Show 31 more" }));
    await waitFor(() => expect(within(groups[1]).getAllByTestId("briefing-row")).toHaveLength(25));
    fireEvent.click(within(groups[1]).getByRole("button", { name: "Show 6 more" }));
    await waitFor(() => expect(within(groups[1]).getAllByTestId("briefing-row")).toHaveLength(31));
    expect(loadItems).toHaveBeenCalledWith("lot_near_expiry", 25, "all");
  });

  it("never states a suspect total as a figure", () => {
    const suspect = item({
      amount: { amount: "4410000.21", currency: "CAD" },
      amount_basis: "total_cost",
      amount_suspect: "the total is over 1,000 times this vendor's usual order in CAD",
    });
    expect(itemFacts(suspect)).toEqual(["Check the total: the total is over 1,000 times this vendor's usual order in CAD"]);
    render(<BriefingCard briefing={{ ...briefing, items: [suspect] }} />);
    expect(screen.queryByText(/4,410,000/)).toBeNull();
    // a data problem is violet "Check the total", not a business problem
    const badge = within(screen.getByTestId("briefing-item")).getByText("Check the total");
    expect(badge.dataset.status).toBe("suspect");
  });

  it("names every check this run did not make whole, and per event type which were never found live", () => {
    const b: Briefing = {
      ...briefing,
      checks: [{ check: "vendor_no_reply", label: "vendor replies", status: "not_checked", words: "vendor replies: not checked this run" }],
      types: [
        { event_type: "vendor_no_reply", label: "Vendor has not replied", status: "not_checked", words: "Vendor has not replied: not checked this run", found_live: null },
        { event_type: "lot_on_hold", label: "Batch on quality hold", status: "complete", words: "Batch on quality hold: checked", found_live: false },
        { event_type: "lot_near_expiry", label: "Lot near expiry", status: "complete", words: "Lot near expiry: checked", found_live: true, first_found: "2026-10-30" },
      ],
    };
    render(<BriefingCard briefing={b} />);
    expect(screen.getByTestId("briefing-counts").textContent).toContain("Vendor replies: not checked this run.");
    fireEvent.click(screen.getByText(/What was checked/));
    const text = screen.getByTestId("briefing-checks").textContent;
    expect(text).toContain("Batch on quality hold: checked · none found in the live data yet");
    expect(text).toContain("Lot near expiry: checked · first found 2026-10-30");
    expect(text).not.toContain("Vendor has not replied: not checked this run · none found");
  });

  it("states a total its own lines disagree with neutrally, both figures and no blame", () => {
    const disagree = item({
      amount: { amount: "345.50", currency: "CAD" },
      amount_basis: "total_cost",
      amount_disagree: true,
      lines_sum: { amount: "47525.00", currency: "CAD" },
    });
    const [words] = itemFacts(disagree);
    // every amount says its currency
    expect(words).toMatch(/^order total .*345.50 CAD and its lines disagree \(lines sum to .*47,525.00 CAD\)$/);
    expect(words).not.toContain("suspect");
    expect(itemFacts({ ...disagree, lines_sum: null })[0]).toMatch(/and its lines disagree$/);
  });

  it("says a Portal unit-price total for what it is, never an order total", () => {
    const [words] = itemFacts(item({ amount: { amount: "22.60", currency: "CAD" }, amount_basis: "unit_price_sum" }));
    expect(words).toMatch(/22.60 CAD Portal total; doesn't match its lines$/);
    expect(words).not.toContain("order total");
    expect(itemFacts(item({ amount: { amount: "50.00", currency: "CAD" }, amount_basis: "portal_lines_unpriced" }))[0]).toMatch(
      /50.00 CAD Portal total; lines not priced$/,
    );
    expect(itemFacts(item({ amount: { amount: "9.00", currency: "CAD" }, amount_basis: "not_checked_against_lines" }))[0]).toMatch(
      /9.00 CAD total not checked against its lines$/,
    );
  });

  it("marks acknowledged items, which stay listed", () => {
    render(<BriefingCard briefing={{ ...briefing, items: [item({ acknowledged: true })] }} />);
    expect(within(screen.getByTestId("briefing-item")).getByText("Acknowledged")).toBeTruthy();
  });

  it("since your last visit reads the briefing of what is new", async () => {
    const loadView = vi.fn(async () => ({ ...briefing, since_visit: true, count: 1, items: [briefing.items![0]] }));
    render(<BriefingCard briefing={briefing} loadView={loadView} />);
    fireEvent.click(screen.getByRole("tab", { name: /New since your last visit 1/ }));
    await waitFor(() => expect(screen.getAllByTestId("briefing-item")).toHaveLength(1));
    expect(loadView).toHaveBeenCalledWith("since_visit");
    expect(screen.getByTestId("briefing-heading").textContent).toContain("1 item needs attention since your last visit");
  });

  it("shows the digest preview as a preview, never as sent", () => {
    render(<BriefingCard briefing={briefing} />);
    fireEvent.click(screen.getByText(/Daily digest preview/));
    expect(screen.getByTestId("digest-preview").textContent).toContain("2 items need your attention.");
    expect(screen.getByText("Preview only: this digest is not sent.")).toBeTruthy();
  });

  it("an unread briefing says so and lists nothing", () => {
    render(<BriefingCard briefing={{ ...briefing, status: "not_configured", items: [] }} />);
    expect(screen.getByTestId("briefing-unavailable")).toBeTruthy();
    expect(screen.queryAllByTestId("briefing-item")).toHaveLength(0);
  });

  it("ranks facts in words", () => {
    expect(itemFacts(item({ amount: { amount: "120.50", currency: "CAD" }, amount_basis: "loss", orders_affected: 2, days: 1 }))).toEqual([
      expect.stringMatching(/120.50.*lost$/),
      "2 customer orders",
      "1 day",
    ]);
  });
});

describe("shortSummary", () => {
  it("says each summary short and in one style, with its stop", async () => {
    const { shortSummary } = await import("./briefing");
    expect(shortSummary("An open purchase order needed by 2026-07-14 has no supplier confirmation")).toBe("Needed by 14 Jul; no supplier date.");
    expect(shortSummary("An open purchase order has no due date recorded")).toBe("No due date on record.");
  });
});

describe("a watcher item", () => {
  const flagged = item({
    id: "aw-0000000000000001",
    event_type: "watcher_attention",
    label: "Flagged from new changes",
    summary: "The customer asked to add 2 test cases to the shipment.",
    why: "The shipment leaves Friday, so it must be settled first.",
  });

  it("states its reason and can be dismissed, then undone", async () => {
    const set = vi
      .spyOn(api, "setWatcherItemState")
      .mockResolvedValueOnce({ item_id: flagged.id, state: "dismissed", at: "2099-01-01T00:00:00Z" })
      .mockResolvedValueOnce({ item_id: flagged.id, state: "open", at: "2099-01-01T00:00:01Z" });
    render(<ul><BriefingRow item={flagged} /></ul>);
    expect(screen.getByTestId("watcher-why").textContent).toBe("The shipment leaves Friday, so it must be settled first.");
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(screen.getByTestId("briefing-item").textContent).toContain("dismissed"));
    expect(set).toHaveBeenCalledWith(flagged.id, "dismissed");
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Dismiss" })).toBeTruthy());
    expect(set).toHaveBeenLastCalledWith(flagged.id, "open");
    set.mockRestore();
  });

  it("snoozes a week and says until when; a failure is said, never hidden", async () => {
    const set = vi
      .spyOn(api, "setWatcherItemState")
      .mockRejectedValueOnce(new Error("503"))
      .mockResolvedValueOnce({ item_id: flagged.id, state: "snoozed", until: "2099-01-08", at: "2099-01-01T00:00:00Z" });
    render(<ul><BriefingRow item={flagged} /></ul>);
    fireEvent.click(screen.getByRole("button", { name: "Snooze a week" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Couldn't save"));
    fireEvent.click(screen.getByRole("button", { name: "Snooze a week" }));
    await waitFor(() => expect(screen.getByTestId("briefing-item").textContent).toContain("snoozed until"));
    set.mockRestore();
  });

  it("offers no dismiss or snooze on a rule-based item", () => {
    render(<ul><BriefingRow item={item({})} /></ul>);
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
  });
});
