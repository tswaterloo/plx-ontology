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
});
