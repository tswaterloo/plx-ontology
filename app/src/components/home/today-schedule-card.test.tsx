import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { UpcomingMeeting } from "@/lib/types";
import { forgetKeptReads } from "@/lib/use-read";

const api = vi.hoisted(() => ({
  meetings: vi.fn(),
}));

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getUpcomingMeetings: (...args: unknown[]) => api.meetings(...args),
}));

import { TodayScheduleCard, type ScheduleSummary } from "./today-schedule-card";

function makeMeeting(id: string, start: Date, end: Date, over: Partial<UpcomingMeeting> = {}): UpcomingMeeting {
  return {
    object: { object_id: id, type: "Meeting", label: `Meeting ${id}`, open_url: `/meetings/${id}` },
    start: start.toISOString(),
    end: end.toISOString(),
    all_day: false,
    online: true,
    join_url: "https://teams.microsoft.com/l/meetup-join/test",
    brief_url: `/meetings/brief/Meeting/${encodeURIComponent(id)}`,
    private: false,
    organizer: { name: "Theo Park", address: "theo@petra.example", role: "organizer", response: "organizer" },
    attendees: [
      { name: "Vince Rossi", address: "vince@petra.example", role: "required", response: "accepted" },
      { name: "Maya Lin", address: "maya@petra.example", role: "required", response: "accepted" },
      { name: "Ricardo Silva", address: "ricardo@petra.example", role: "optional", response: "none" },
      { name: "Jordan Lee", address: "jordan@supplier.example", role: "required", response: "accepted", outside: true },
    ],
    parties: [{ object_id: "Supplier:S1", type: "Supplier", label: "Croda Inc.", open_url: "/supplier/S1" }],
    open_items: [],
    sources: [],
    ...over,
  } as UpcomingMeeting;
}

beforeEach(() => {
  forgetKeptReads();
  api.meetings.mockReset();
});

describe("TodayScheduleCard", () => {
  it("renders today's meetings with Join link, stacked attendee initials, prep brief link, and Up next preview", async () => {
    const now = new Date();
    const s1 = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 0);
    const e1 = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 30);
    const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 10, 0);
    const tomorrowEnd = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 11, 0);

    api.meetings.mockResolvedValue({
      meetings: [
        makeMeeting("m1", s1, e1, { object: { object_id: "m1", type: "Meeting", label: "Croda Q4 Supply Review", open_url: "/m/1" } }),
        makeMeeting("m2", tomorrow, tomorrowEnd, { object: { object_id: "m2", type: "Meeting", label: "Ops Weekly Sync", open_url: "/m/2" }, online: false, join_url: null }),
      ],
    });

    const summaries: ScheduleSummary[] = [];
    render(<TodayScheduleCard onSummaryChange={(s) => summaries.push(s)} />);

    await screen.findByTestId("today-schedule-list");
    expect(screen.getByTestId("today-schedule-full-link")).toHaveAttribute("href", "/meetings");
    expect(screen.getByRole("link", { name: "Croda Q4 Supply Review" })).toHaveAttribute(
      "href",
      "/meetings/brief/Meeting/m1",
    );
    expect(screen.getByTestId("today-schedule-join")).toHaveAttribute(
      "href",
      "https://teams.microsoft.com/l/meetup-join/test",
    );
    // 5 total people -> 3 avatars + "+2" overflow on both meetings
    expect(screen.getAllByText("+2")).toHaveLength(2);
    // Fewer than 3 meetings today -> shows tomorrow under "Up next"
    expect(screen.getByTestId("today-schedule-up-next")).toHaveTextContent("Ops Weekly Sync");
    expect(screen.getByTestId("today-schedule-up-next")).toHaveTextContent("Tomorrow");

    await waitFor(() => {
      const latest = summaries[summaries.length - 1];
      expect(latest?.loading).toBe(false);
      expect(latest?.todayCount).toBe(1);
    });
  });

  it("shows a helpful empty state when no meetings are scheduled and supports manual refresh", async () => {
    api.meetings.mockResolvedValue({ meetings: [] });
    render(<TodayScheduleCard />);

    await screen.findByTestId("today-schedule-empty");
    expect(screen.getByText("No meetings scheduled")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("today-schedule-refresh"));
    await waitFor(() => expect(api.meetings).toHaveBeenCalledTimes(2));
  });
});
