import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Profitability } from "@/lib/types";
import { ProfitabilityView } from "./cost-breakdown";

const ref = (id: string, label: string) => ({ object_id: id, type: "SalesOrder", label, open_url: `/object/SalesOrder/${id}` });
const cad = (amount: string) => ({ amount, currency: "CAD" });
const ledger = { system: "BC" as const, handle: "bc://itemLedgerEntries/1", label: "BC item ledger entry", open_url: "/source?handle=bc1" };

const p: Profitability = {
  customer: { object_id: "c1", type: "Customer", label: "Northwind", open_url: "/object/Customer/c1" },
  period_start: "2025-10-30",
  period_end: "2026-10-30",
  period: "the last 12 months",
  revenue: cad("190.00"),
  booked_cost: cad("231.00"),
  margin: cad("-41.00"),
  margin_pct: "-21.58",
  orders_in_period: 3,
  orders_counted: 2,
  withheld_orders: 0,
  orders_read: 3,
  orders_in_period_exact: true,
  period_basis: null,
  ranking: "worst first, every order of the period",
  orders_more: 0,
  margin_basis: "on item cost only",
  margin_note: "Labour and overhead aren't booked in Business Central, so this overstates true margin.",
  verdict: "not_profitable",
  orders_excluded: 1,
  orders: [
    { order: ref("o1", "SO-101"), date: null, revenue: cad("90.00"), booked_cost: cad("135.00"), margin: cad("-45.00"), margin_pct: "-50.00", gap: null },
    { order: ref("o3", "SO-103"), date: null, revenue: cad("40.00"), booked_cost: cad("16.00"), margin: cad("24.00"), margin_pct: "60.00", gap: null },
    { order: ref("o4", "SO-104"), date: null, revenue: null, booked_cost: null, margin: null, margin_pct: null, gap: "line not invoiced" },
  ],
  breakdown: {
    order: ref("o1", "SO-101"),
    date: "2026-09-05",
    margin_pct: "-50.00",
    revenue: cad("90.00"),
    booked_cost: cad("135.00"),
    margin: cad("-45.00"),
    lines: [{ label: "item FG-9 (Tube)", quantity: "9", unit: "PCS", revenue: cad("90.00"), booked_cost: cad("135.00"), margin: cad("-45.00"), unit_price: "10", unit_cost: "15", share_of_cost_pct: null, sources: [ledger] }],
    cost_sources: [
      { kind: "item_ledger", label: "item FG-9 (Tube)", detail: "booked item-ledger cost of 1 shipment entry", amount: cad("135.00"), per_unit: null, sources: [ledger] },
      { kind: "material_po", label: "Purchase order PO9002", detail: "item FG-9 (Tube)", amount: null, per_unit: "16.00 CAD/PCS goods cost; landed cost not established", sources: [] },
    ],
    drivers: ["Item FG-9 (Tube) lost 45.00 CAD on SO-101."],
    gaps: [],
  },
  next_action: { text: "Review the price for Northwind on item FG-9.", rule: "item_below_cost_on_repeat_orders", contact: null, contact_source: null, suggestion_only: true },
};

describe("ProfitabilityView", () => {
  it("ranks orders worst first, says why an order has no margin, and breaks down the worst", () => {
    render(<ProfitabilityView p={p} />);
    const table = screen.getByTestId("order-ranking");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining("SO-101"),
      expect.stringContaining("SO-103"),
      expect.stringContaining("1 order has no margin yet"),
    ]);
    // an order without a margin is collapsed with its reason, never an inline row of dashes
    expect(screen.getByTestId("no-margin")).toHaveTextContent("SO-104: line not invoiced");
    const b = screen.getByTestId("cost-breakdown");
    expect(b).toHaveTextContent("Why SO-101 lost $45.00 CAD");
    expect(b).not.toHaveTextContent("made what it made");
    expect(b).toHaveTextContent("Booked cost (item ledger)");
    expect(b).toHaveTextContent("16.00 CAD/PCS goods cost; landed cost not established");
    // one line: no "share of cost" column
    expect(b).not.toHaveTextContent("Share of cost");
    expect(within(b).getAllByRole("button", { name: /Business Central/ }).length).toBe(2);
    expect(screen.getByTestId("next-action")).toHaveTextContent("Nothing is sent or changed from here.");
    // the margin basis is said once, as the footnote the header's asterisk points at
    expect(table).toHaveTextContent("Margin*");
    expect(screen.getByTestId("profitability")).not.toHaveTextContent("Item cost only. Freight");
    expect(screen.getByTestId("profitability").textContent!.match(/Margins are on item cost only/g)).toHaveLength(1);
    // four numbers: coverage beside the figure
    expect(screen.getByTestId("profitability")).toHaveTextContent("Orders with a margin2 of 3");
    expect(screen.getByTestId("margin-basis")).toHaveTextContent(
      "*Margins are on item cost only. Labour and overhead aren't booked in Business Central, so this overstates true margin.");
  });

  it("with no basis note from the server, the ranking keeps its own footnote", () => {
    render(<ProfitabilityView p={{ ...p, margin_note: null }} />);
    expect(screen.getByTestId("profitability")).toHaveTextContent("*Item cost only. Freight, labour and overhead aren't included.");
    expect(screen.queryByTestId("margin-basis")).toBeNull();
  });

  it("labels a partial ranking and says how many more orders are not listed", () => {
    render(<ProfitabilityView p={{ ...p, ranking: "partial: worst of the 3 most recent orders; 214 more in the period not read", orders_more: 4 }} />);
    const t = screen.getByTestId("order-ranking");
    expect(t).toHaveTextContent("Partial: worst of the 3 most recent orders; 214 more in the period not read");
    expect(screen.getByTestId("orders-more")).toHaveTextContent("and 4 more");
  });

  it("never lists a test order in the ranking", () => {
    render(<ProfitabilityView p={{ ...p, orders: [...(p.orders ?? []), { ...p.orders![1], order: ref("t1", "UAT-SO100004") }] }} />);
    expect(screen.getByTestId("order-ranking")).not.toHaveTextContent("UAT-SO100004");
  });

  it("shows no breakdown or action when the answer has none", () => {
    render(<ProfitabilityView p={{ ...p, breakdown: null, next_action: null, orders: [] }} />);
    expect(screen.queryByTestId("cost-breakdown")).toBeNull();
    expect(screen.queryByTestId("next-action")).toBeNull();
    expect(screen.queryByTestId("order-ranking")).toBeNull();
  });
});

describe("MarginDrivers", () => {
  it("lists why the customer loses money with each reason's sources, and says plainly what is not broken out", () => {
    render(
      <ProfitabilityView
        p={{
          ...p,
          drivers: [
            { kind: "losing_orders", established: true, text: "2 of 3 checked orders lost money: −65.00 CAD in all.", amount: cad("-65.00"), sources: [ledger] },
            { kind: "freight", established: false, text: "Freight is not in this margin.", amount: null, where_it_would_live: "the G/L", sources: [] },
          ],
        }}
      />,
    );
    const why = screen.getByTestId("margin-drivers");
    expect(within(why).getByTestId("driver-losing_orders").textContent).toContain("lost money");
    expect(within(within(why).getByTestId("driver-losing_orders")).getAllByTestId("source-chip")).toHaveLength(1);
    expect(within(within(why).getByTestId("driver-freight")).queryAllByTestId("source-chip")).toHaveLength(0);
    expect(within(why).getByTestId("driver-freight").className).toContain("text-muted");
  });
});
