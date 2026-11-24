import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getDataHealth: () => Promise.resolve({ status: "read", partial: false, checks: [
    { type: "never", label: "Order without a customer PO", on: true, read: false, open: null, count: 0, hidden: 0, more: 0, items: [] },
    { type: "ran", label: "Customer entered twice", on: true, read: false, open: 0, checked_at: "2026-11-22T22:29:33+00:00", count: 0, hidden: 0, more: 0, items: [] },
  ] }),
}));

import { DataHealthSection } from "./alert-kinds";

describe("Data health, a check that never ran", () => {
  it("says not checked, never None, and a check that ran and found nothing says None with when", async () => {
    render(<DataHealthSection titled={false} />);
    await screen.findByTestId("data-health-never");
    expect(screen.getByTestId("data-health-count-never")).toHaveTextContent("Not checked");
    expect(screen.getByTestId("data-health-unchecked-never")).toBeInTheDocument();
    expect(screen.getByTestId("data-health-count-ran")).toHaveTextContent("None");
    expect(screen.getByText(/Last checked/)).toBeInTheDocument();
  });
});
