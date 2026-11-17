import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { addDecimal, lateTotals } from "@/lib/late";
import type { LatePo } from "@/lib/types";
import { Glance } from "./home-glance";

const po = (n: number, days: number, amount: string | null, currency = "CAD", over: Partial<LatePo> = {}): LatePo => ({
  object_id: `PurchaseOrder:demo:${n}`, label: String(n), open_url: `/object/PurchaseOrder/${n}`, reason: "late", supplier: `Supplier ${n}`, days_late: days,
  amount: amount === null ? null : { amount, currency }, ...over,
} as LatePo);

describe("My work at a glance", () => {
  it("adds late values exactly per currency and never across currencies", () => {
    expect(addDecimal("0.1", "0.2")).toBe("0.3");
    expect(addDecimal("18400.00", "2310.5")).toBe("20710.50");
    expect(addDecimal("-5.25", "1")).toBe("-4.25");
    const t = lateTotals([po(1, 3, "0.10"), po(2, 4, "0.20"), po(3, 5, "2310.50", "USD"), po(4, 6, "99", "CAD", { amount_suspect: "lines disagree" }), po(5, 7, null)]);
    expect(t).toEqual([{ currency: "USD", amount: "2310.50", count: 1 }, { currency: "CAD", amount: "0.30", count: 2 }]);
  });

  it("says how many are late, the oldest, the value per currency, the other groups and the replies owed", () => {
    render(
      <Glance
        late={[po(90412, 12, "18400.00"), po(90388, 5, "2310.50", "USD"), po(90500, 2, null)]}
        lateTotal={3}
        groups={[{ type: "po_late", label: "Late", count: 3 }, { type: "po_due", label: "Due soon", count: 4 }, { type: "eta_slipped", label: "Date slipped", count: 0 }]}
        replies={2}
      />,
    );
    const g = screen.getByTestId("home-glance");
    expect(g).toHaveTextContent("Late 3");
    expect(g).toHaveTextContent("Oldest 12 d");
    // narrow symbols: both dollars read "$", the code after each keeps them apart; 2310.50 rounds up
    expect(g).toHaveTextContent("Late value$18,400 CAD + $2,311 USD(1 without)");
    expect(g).toHaveTextContent("Due soon 4");
    expect(g.textContent).not.toMatch(/Date slipped/);
    expect(g).toHaveTextContent("Replies owed 2");
  });

  it("says the value covers only the POs read when the late list is longer", () => {
    render(<Glance late={[po(1, 30, "100.00")]} lateTotal={140} groups={[]} replies={null} />);
    const g = screen.getByTestId("home-glance");
    expect(g).toHaveTextContent("Late 140");
    expect(g).toHaveTextContent("Value of first 1");
  });

  it("shows nothing when nothing is open", () => {
    const { container } = render(<Glance late={[]} lateTotal={0} groups={[]} replies={0} />);
    expect(container).toBeEmptyDOMElement();
  });
});
