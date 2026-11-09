import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { History } from "./history";
import type { TimelineEvent } from "@/lib/types";

const ev: TimelineEvent[] = [
  { at: "2026-10-01T10:00:00Z", system: "BC", title: "Invoice posted" },
  { at: "2026-10-02T10:00:00Z", system: "Email", title: "Buyer wrote" },
];

describe("history", () => {
  it("filters by system and kind with pressed chips", () => {
    render(<History events={ev} linked={[]} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Email" }));
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Email" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Invoices" }));
    expect(screen.getByTestId("history-none")).toBeTruthy();
  });

  it("empty and partial states are plain words without a count", () => {
    const { rerender } = render(<History events={[]} linked={[]} />);
    expect(screen.getByTestId("history-empty").textContent).toBe("Nothing dated on this record.");
    rerender(<History events={[]} linked={[]} partial />);
    expect(screen.getByTestId("history-empty").textContent).toContain("Some linked records are hidden from you");
    rerender(<History events={ev} linked={[]} partial />);
    expect(screen.getByText(/may not be everything/)).toBeTruthy();
  });
});
