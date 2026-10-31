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
    const rows = within(screen.getByTestId("order-ranking")).getAllByRole("row").slice(1);
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining("SO-101"),
      expect.stringContaining("SO-103"),
      "SO-104No margin: line not invoiced",
    ]);
    const b = screen.getByTestId("cost-breakdown");
    expect(b).toHaveTextContent("Why SO-101 lost money");
    expect(b).toHaveTextContent("Booked cost (item ledger)");
    expect(b).toHaveTextContent("16.00 CAD/PCS goods cost; landed cost not established");
    expect(within(b).getAllByRole("link", { name: /BC item ledger entry/ }).length).toBe(2);
    expect(screen.getByTestId("next-action")).toHaveTextContent("a suggestion; nothing was changed");
  });

  it("labels a partial ranking and says how many more orders are not listed", () => {
    render(<ProfitabilityView p={{ ...p, ranking: "partial: worst of the 3 most recent orders; 214 more in the period not read", orders_more: 4 }} />);
    const t = screen.getByTestId("order-ranking");
    expect(t).toHaveTextContent("partial: worst of the 3 most recent orders; 214 more in the period not read");
    expect(screen.getByTestId("orders-more")).toHaveTextContent("and 4 more");
  });

  it("shows no breakdown or action when the answer has none", () => {
    render(<ProfitabilityView p={{ ...p, breakdown: null, next_action: null, orders: [] }} />);
    expect(screen.queryByTestId("cost-breakdown")).toBeNull();
    expect(screen.queryByTestId("next-action")).toBeNull();
    expect(screen.queryByTestId("order-ranking")).toBeNull();
  });
});
