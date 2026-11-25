import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { businessToday } from "@/lib/format";
import { OrderLine } from "./parts";
import { doorNeededOf } from "../object-view";
import type { BizPage } from "@/lib/types";

afterEach(() => vi.useRealTimers());

describe("an order's line", () => {
  it("names today as the business's day: 8 p.m. in Toronto is not yet tomorrow", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-10T01:00:00Z")); // 8 p.m. 9 Nov in Toronto
    expect(businessToday()).toBe("2026-11-09");
    render(<OrderLine ordered={Date.parse("2026-09-26T12:00:00Z")} needed={Date.parse("2026-10-17T12:00:00Z")} />);
    const line = screen.getByTestId("order-line");
    expect(line).toHaveTextContent("Today9 Nov");
    expect(line).not.toHaveTextContent("10 Nov");
  });

  it("draws the days late once on a page whose header already says them (read aloud only)", () => {
    const props = { ordered: Date.parse("2026-09-26T12:00:00Z"), needed: Date.parse("2026-10-17T12:00:00Z"), daysLate: 12 };
    const { rerender } = render(<OrderLine {...props} />);
    expect(screen.getByText("12 days late")).not.toHaveClass("sr-only");
    rerender(<OrderLine {...props} saidAbove />);
    expect(screen.getByText("12 days late")).toHaveClass("sr-only");
  });

  it("shows line-derived needed-by label cleanly when provided", () => {
    render(<OrderLine ordered={Date.parse("2026-03-28T12:00:00Z")} needed={Date.parse("2026-06-24T12:00:00Z")} neededLabel="Needed by (from lines)" />);
    const line = screen.getByTestId("order-line");
    expect(line).toHaveTextContent("Needed by (from lines)");
    expect(line).toHaveTextContent("24 Jun");
    expect(line).not.toHaveTextContent("Not on record");
  });

  it("derives PO needed-by date from door facts or earliest open line expected date", () => {
    const withFact = {
      kind: "purchase_order",
      title: "PO 106001",
      facts: [{ label: "Needed by (from lines)", value: { day: "2026-06-24" } }],
      sections: [],
      sources: [],
    } as unknown as BizPage;
    expect(doorNeededOf(withFact)).toEqual({ iso: "2026-06-24", fromLines: true });

    const withLinesOnly = {
      kind: "purchase_order",
      title: "PO 106001",
      facts: [],
      sections: [
        {
          key: "lines",
          title: "Lines",
          columns: ["Item", "Ordered", "Received", "Expected"],
          rows: [
            [{ text: "5159" }, { number: 18 }, { number: 18 }, { day: "2026-05-01" }],
            [{ text: "5302" }, { number: 25 }, { number: 0 }, { day: "2026-06-24" }],
            [{ text: "5303" }, { number: 10 }, { number: 0 }, { day: "2026-07-02" }],
          ],
          total: 3,
        },
      ],
      sources: [],
    } as unknown as BizPage;
    expect(doorNeededOf(withLinesOnly)).toEqual({ iso: "2026-06-24", fromLines: true });
  });
});
