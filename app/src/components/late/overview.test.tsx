import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { LatePo } from "@/lib/types";
import { LateOverview, nextStep } from "./overview";

const po = (n: number, days: number, confirmation: LatePo["confirmation"], amount?: string, currency = "CAD"): LatePo => ({
  object_id: `PurchaseOrder:demo:${n}`,
  label: String(n),
  supplier: `Supplier ${n}`,
  days_late: days,
  reason: "late",
  confirmation,
  open_url: `/po/${n}`,
  sources: [],
  ...(amount ? { amount: { amount, currency } } : {}),
});

describe("late overview: act on first", () => {
  it("lists the longest late first, each with the step its confirmation calls for, at most six", () => {
    const items = [po(1, 3, "confirmed", "100.00"), po(2, 40, "none_found", "5.00"), po(3, 40, "none_found", "900.00"), po(4, 12, "needs_check"), po(5, 1, null), po(6, 2, "found_unverified"), po(7, 0, null)];
    render(<LateOverview items={items} total={items.length} picked={null} onSupplier={() => undefined} />);
    const rows = within(screen.getByTestId("late-first")).getAllByRole("row");
    expect(rows).toHaveLength(6);
    expect(rows.map((r) => r.textContent?.match(/PO\s*\d+/)?.[0].replace(/\s+/, " "))).toEqual(["PO 3", "PO 2", "PO 4", "PO 1", "PO 6", "PO 5"]);
    expect(rows[0]).toHaveTextContent("40 d");
    expect(rows[0]).toHaveTextContent("ask the supplier for one");
    expect(rows[2]).toHaveTextContent("check which order it is");
  });

  it("never ranks across currencies by the bare number", () => {
    const items = [po(1, 5, "none_found", "100.00", "CAD"), po(2, 5, "none_found", "900.00", "USD")];
    render(<LateOverview items={items} total={2} picked={null} onSupplier={() => undefined} />);
    const rows = within(screen.getByTestId("late-first")).getAllByRole("row");
    expect(rows[0]).toHaveTextContent("PO 1");
  });

  it("has a step for every confirmation state, and an unknown one opens the order", () => {
    for (const c of ["none_found", "found_unverified", "needs_check", "confirmed", null] as const) expect(nextStep(po(1, 1, c)).length).toBeGreaterThan(10);
    expect(nextStep({ ...po(1, 1, "something_new" as LatePo["confirmation"]), reason: "" })).toBe("Open the order to see where it stands");
    expect(nextStep(po(1, 1, null))).toBe("late; open the order to see where it stands");
  });
});
