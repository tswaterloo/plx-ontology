import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Brief } from "@/lib/types";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: () => undefined }), usePathname: () => "/meetings" }));
const api = vi.hoisted(() => ({ getWeeklyReview: vi.fn(), getUpcomingMeetings: vi.fn() }));
vi.mock("@/lib/api", () => api);

import { WeeklyReview } from "./weekly";

const base = { kind: "weekly", title: "Weekly review", as_of: "2026-11-24", parties: [], possible_parties: [], subject_sources: [], gaps: [] };

describe("weekly review first build", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    api.getUpcomingMeetings.mockResolvedValue({ meetings: [] });
  });
  afterEach(() => vi.useRealTimers());

  it("says it is being built with the time it is ready about, then fills in without a reload", async () => {
    api.getWeeklyReview
      .mockResolvedValueOnce({ ...base, reading: true, ready_about: "2026-11-24T20:10:00Z", basis: "Being built.", sections: [] } as unknown as Brief)
      .mockResolvedValue({
        ...base, basis: "From the records.",
        sections: [{ key: "figures", title: "Figures", state: "read", rows: [], figures: [], more: 0 }],
      } as unknown as Brief);
    render(<WeeklyReview />);
    const building = await screen.findByTestId("weekly-building");
    expect(building.textContent).toMatch(/ready about/);
    expect(screen.queryByTestId("weekly-quiet")).toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_500);
    });
    expect(screen.queryByTestId("weekly-building")).toBeNull();
    expect(api.getWeeklyReview).toHaveBeenCalledTimes(2);
  });

  it("with no estimate it still says it is being built, never an empty review", async () => {
    api.getWeeklyReview.mockResolvedValue({ ...base, reading: true, basis: "Being built.", sections: [] } as unknown as Brief);
    render(<WeeklyReview />);
    const building = await screen.findByTestId("weekly-building");
    expect(building.textContent).toMatch(/Building the week/);
    expect(building.textContent).not.toMatch(/ready about/);
  });
});
