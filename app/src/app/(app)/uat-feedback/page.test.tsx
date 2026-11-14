import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { UatFeedback } from "@/lib/types";

const api = vi.hoisted(() => ({ body: null as unknown, asked: [] as (string | undefined)[] }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getUatFeedback: (status?: string) => {
    api.asked.push(status);
    return Promise.resolve(api.body);
  },
}));

import UatFeedbackPage from "./page";

const item = (n: number, over: Record<string, unknown> = {}) => ({
  record: `SharePointListItem:${n}`, list: "MRP UAT", title: `Item ${n}`, status: "open",
  status_word: "Pending", resolution_word: null, priority: "High", modified: "2026-11-01", age_days: 13, ...over,
});
const base: UatFeedback = {
  as_of: "2026-11-14", served: true, totals: { open: 3, fixed: 1, answered: 1 }, total: 5, matched: 5, cut: false, withheld: 0,
  open_priorities: [{ word: "Low", count: 1 }, { word: "Critical", count: 1 }, { word: "High", count: 1 }],
  lists: ["MRP UAT"], sent: 0, writes: 0,
  items: [item(1, { priority: "Low", title: "A".repeat(300) }), item(2, { priority: "Critical \u2014 Blocks work, no workaround" }), item(3, { priority: null, status: "fixed", age_days: null }), item(4, { status: "open", modified: null, age_days: null })],
};

describe("UAT feedback page", () => {
  it("shows totals, critical first, links, and filters by priority and search", async () => {
    api.body = base;
    render(<UatFeedbackPage />);
    expect(await screen.findByTestId("uat-totals")).toHaveTextContent("5 items: 3 open, 1 fixed, 1 answered.");
    expect(screen.getByTestId("uat-priorities")).toHaveTextContent("Open by priority: 1 Critical, 1 High, 1 Low.");
    const rows = screen.getAllByTestId("uat-row");
    expect(rows[0]).toHaveTextContent("Item 2");
    expect(rows[rows.length - 1]).toHaveTextContent("Item 3");
    expect(rows[0].querySelector("a")?.getAttribute("href")).toBe("/object/SharePointListItem/SharePointListItem%3A2");
    expect(screen.queryByText("List")).toBeNull();
    expect(rows[2]).toHaveTextContent("13 days");
    fireEvent.click(screen.getByRole("button", { name: "1 Critical" }));
    expect(screen.getAllByTestId("uat-row")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "1 Critical" }));
    fireEvent.change(screen.getByLabelText("Search titles"), { target: { value: "item 4" } });
    expect(screen.getAllByTestId("uat-row")).toHaveLength(1);
    expect(screen.getByTestId("uat-row")).toHaveTextContent("not dated");
    fireEvent.change(screen.getByLabelText("Search titles"), { target: { value: "zzz" } });
    expect(screen.getByTestId("uat-none")).toBeInTheDocument();
  });

  it("asks the server for a status, and says what is cut or withheld", async () => {
    api.body = { ...base, cut: true, matched: 303, withheld: 2, lists: ["MRP UAT", "MRP UAT 2"] };
    render(<UatFeedbackPage />);
    expect(await screen.findByTestId("uat-cut")).toHaveTextContent("Showing 4 of 303");
    expect(screen.getByTestId("uat-withheld")).toHaveTextContent("2 items you may not open are not counted above");
    expect(screen.getByText("List")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "fixed" } });
    await screen.findByTestId("uat-page");
    expect(api.asked).toContain("fixed");
  });

  it("says plainly when the view is not served or nothing is readable", async () => {
    api.body = { ...base, served: false, items: [] };
    const { unmount } = render(<UatFeedbackPage />);
    expect(await screen.findByTestId("uat-unserved")).toBeInTheDocument();
    unmount();
    api.body = { ...base, totals: {}, total: 0, items: [], matched: 0, open_priorities: [], withheld: 3 };
    render(<UatFeedbackPage />);
    expect(await screen.findByTestId("uat-empty")).toHaveTextContent("3 items you may not open are not shown");
  });
});
