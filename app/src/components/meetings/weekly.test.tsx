import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Brief } from "@/lib/types";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: () => undefined }), usePathname: () => "/meetings" }));
vi.mock("@/lib/api", () => ({
  getUpcomingMeetings: async () => ({ meetings: [] }),
  getWeeklyReview: async (): Promise<Brief> => ({
    kind: "weekly", title: "Weekly review", as_of: "2026-11-21", basis: "From the records.", parties: [], possible_parties: [], subject_sources: [], gaps: [],
    sections: [
      { key: "late_pos", title: "Late purchase orders (open now)", state: "unread", note: "Still reading the late purchase orders; open the review again in a moment.", rows: [], figures: [], more: 0 },
      { key: "complaints", title: "Customer complaints", state: "empty", rows: [], figures: [], more: 0 },
      { key: "cash", title: "Cash position", state: "unread", rows: [], figures: [], more: 0 },
      { key: "ap", title: "Payables", state: "blocked", rows: [], figures: [], more: 0 },
    ],
  }) as unknown as Brief,
}));

import { WeeklyReview } from "./weekly";

describe("weekly review", () => {
  it("a section still being read says so under its title, never 'nothing this week'", async () => {
    render(<WeeklyReview />);
    const late = await screen.findByTestId("brief-section-late_pos");
    expect(within(late).getByText(/Still reading the late purchase orders/)).toBeTruthy();
    expect(screen.getByTestId("weekly-quiet").textContent).not.toMatch(/late purchase orders/i);
  });

  it("cash and payables that could not be read are named as not read, never counted as quiet (11-22 audit)", async () => {
    render(<WeeklyReview />);
    await screen.findByTestId("brief-section-late_pos");
    const quiet = screen.getAllByTestId("weekly-quiet").map((n) => n.textContent).join(" ");
    expect(quiet).toMatch(/customer complaints/i);
    expect(quiet).not.toMatch(/cash|payables/i);
    const unread = screen.getAllByTestId("weekly-unread").map((n) => n.textContent).join(" ");
    expect(unread).toMatch(/cash position/);
    expect(unread).toMatch(/payables/);
  });

  it("renders a single section count badge without duplicating the agenda number", async () => {
    const api = await import("@/lib/api");
    vi.spyOn(api, "getWeeklyReview").mockResolvedValueOnce({
      kind: "weekly", title: "Weekly review", as_of: "2026-11-25", basis: "From the records.", parties: [], possible_parties: [], subject_sources: [], gaps: [],
      sections: [
        {
          key: "figures",
          title: "Figures",
          state: "shown",
          rows: [],
          figures: [{ label: "Invoiced (CAD)", value: "122597.47", previous: "261092.68", delta: "-138495.21", unit: "money", currency: "CAD" }],
          more: 0,
        },
        {
          key: "late_pos",
          title: "Late purchase orders (open now)",
          state: "shown",
          rows: [{ label: "PO 106001", who: "DSM", detail: "153 days late", sources: [] }],
          figures: [],
          more: 0,
        },
      ],
    } as unknown as Brief);
    render(<WeeklyReview />);
    const late = await screen.findByTestId("brief-section-late_pos");
    const heading = within(late).getByRole("heading", { level: 3 });
    expect(heading.textContent).toBe("Late purchase orders (open now)1");
    expect(within(heading).getByTestId("section-count")).toHaveTextContent("1");
    expect(screen.getByTestId("brief-figure").textContent).toContain("$122,597.47CAD");
  });
});
