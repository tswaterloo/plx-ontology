import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { UpcomingMeeting } from "@/lib/types";
import { cleanMeetingAgenda, hourSpan, layoutDay, weekBounds } from "./calendar";
import { Week } from "./upcoming";

// local-time fixtures: Wednesday 2026-11-11 is "now"
const now = new Date(2026, 10, 11, 10, 0);
const at = (d: number, h: number, mi = 0) => new Date(2026, 10, d, h, mi).toISOString();
const mt = (id: string, start: string, end: string | null, extra: Partial<UpcomingMeeting> = {}) =>
  ({ object: { object_id: id, type: "Meeting", label: id, open_url: `/m/${id}` }, start, end, parties: [], open_items: [], ...extra }) as unknown as UpcomingMeeting;

describe("week view (WEB-020): every event stays discoverable", () => {
  it("lays overlapping meetings side by side, and a following one back at full width", () => {
    const a = mt("A", at(11, 9), at(11, 10, 30));
    const b = mt("B", at(11, 10), at(11, 11));
    const c = mt("C", at(11, 10, 15), at(11, 10, 45));
    const d = mt("D", at(11, 13), at(11, 14));
    const by = Object.fromEntries(layoutDay([d, c, b, a]).map((p) => [p.m.object.object_id, p]));
    expect(by.A).toMatchObject({ col: 0, cols: 3 });
    expect(by.B).toMatchObject({ col: 1, cols: 3 });
    expect(by.C).toMatchObject({ col: 2, cols: 3 });
    expect(by.D).toMatchObject({ col: 0, cols: 1 });
  });

  it("widens the hours for a 6 AM or 8 PM meeting, and cuts nothing off", () => {
    expect(hourSpan([])).toEqual({ first: 7, last: 19 });
    expect(hourSpan([mt("e", at(11, 6), at(11, 7))])).toEqual({ first: 6, last: 19 });
    expect(hourSpan([mt("l", at(11, 20), at(11, 21, 30))])).toEqual({ first: 7, last: 22 });
    expect(hourSpan([mt("allday", at(11, 0), at(12, 0), { all_day: true })])).toEqual({ first: 7, last: 19 });
  });

  it("shows an all-day event, an early and a late meeting and two simultaneous ones", () => {
    const list = [
      mt("Holiday", at(11, 0), at(12, 0), { all_day: true }),
      mt("Early", at(11, 6), at(11, 7)),
      mt("Late", at(11, 20), at(11, 21)),
      mt("S1", at(12, 9), at(12, 10)),
      mt("S2", at(12, 9), at(12, 10)),
    ];
    render(<Week meetings={list} now={now} onOpen={vi.fn()} />);
    const shown = screen.getAllByTestId("week-block").map((b) => b.textContent);
    for (const label of ["Holiday", "Early", "Late", "S1", "S2"]) expect(shown.some((t) => t?.includes(label))).toBe(true);
    expect(screen.getAllByTestId("week-block")).toHaveLength(5);
    expect(screen.getAllByTestId("week-all-day").length).toBe(7);
  });

  it("a third calendar week is reachable when meetings are that far out; the arrows stop at the data", () => {
    const list = [mt("Far", at(25, 9), at(25, 10))]; // Wed 25 Nov: two weeks on
    expect(weekBounds(list, now)).toEqual({ min: 0, max: 2 });
    expect(weekBounds([], now)).toEqual({ min: 0, max: 1 });
    render(<Week meetings={list} now={now} onOpen={vi.fn()} />);
    const later = screen.getByRole("button", { name: "Later week" });
    fireEvent.click(later);
    fireEvent.click(later);
    expect(screen.getByTestId("week-block")).toHaveTextContent("Far");
    expect(later).toBeDisabled();
    // "This week" returns straight to offset 0
    fireEvent.click(screen.getByRole("button", { name: "This week" }));
    expect(screen.queryByRole("button", { name: "This week" })).toBeNull();
  });

  it("strips Teams/Zoom invite link soup while preserving real agenda text", () => {
    const raw = [
      "Review Q4 bottle lead times with Croda.",
      "________________________________________________________________________________",
      "Microsoft Teams Need help?",
      "Join the meeting now <https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc>",
      "Meeting ID: 281 945 123 456",
      "Passcode: xYz123",
      "Dial in by phone +1 416-555-0199",
    ].join("\n");
    expect(cleanMeetingAgenda(raw)).toEqual(["Review Q4 bottle lead times with Croda."]);
    const onlySoup = "Microsoft Teams meeting\nJoin on your computer\nhttps://teams.microsoft.com/l/meetup-join/abc\nMeeting ID: 123 456";
    expect(cleanMeetingAgenda(onlySoup)).toEqual([]);
  });
});
