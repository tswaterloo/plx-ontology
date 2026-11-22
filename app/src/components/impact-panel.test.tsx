import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ImpactCard, buildChange, impactApplies } from "./impact-panel";

afterEach(() => vi.unstubAllGlobals());
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("the Impact panel on a record page", () => {
  it("asks only for the kinds of record that have a walk, and for a valid change", () => {
    expect(impactApplies("PurchaseOrder")).toBe(true);
    expect(impactApplies("SalesOrder")).toBe(true);
    expect(impactApplies("Email")).toBe(false);
    expect(buildChange("delay", 0, "5")).toBeNull();
    expect(buildChange("delay", 400, "5")).toBeNull();
    expect(buildChange("delay", 7, "5")).toEqual({ kind: "delay", days: 7 });
    expect(buildChange("price_change", 7, "0")).toBeNull();
  });

  it("draws nothing for a record with no walk and reads nothing until asked", () => {
    const f = vi.fn();
    vi.stubGlobal("fetch", f);
    const { container } = render(<ImpactCard type="Email" id="Email:1" />);
    expect(container).toBeEmptyDOMElement();
    render(<ImpactCard type="PurchaseOrder" id="PurchaseOrder:1" />);
    expect(f).not.toHaveBeenCalled();
  });

  it("a delay on a PO reads what-if for that record with the days, and shows what could not be established", async () => {
    const f = vi.fn(() => Promise.resolve(json({ object_id: "PurchaseOrder:1", mode: "what_if", change: "delayed 7 days", orders: [], orders_total: 0, customers: [], money: [], causes: [], unknown: ["no customer order is linked to this PO"], partial: false, applies: true })));
    vi.stubGlobal("fetch", f);
    render(<ImpactCard type="PurchaseOrder" id="PurchaseOrder:1" />);
    fireEvent.click(screen.getByRole("button", { name: "Show impact" }));
    await waitFor(() => expect(screen.getByText(/no customer order is linked/)).toBeInTheDocument());
    const url = String((f.mock.calls[0] as unknown[])[0]);
    expect(url).toContain("/api/object-impact/what_if/PurchaseOrder/");
    expect(url).toContain("change=delay");
    expect(url).toContain("days=7");
  });
});
