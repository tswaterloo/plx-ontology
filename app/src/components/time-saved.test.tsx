import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ body: null as unknown, asked: [] as boolean[] }));
vi.mock("@/lib/api", () => ({
  getTimeSaved: (everyone: boolean) => {
    api.asked.push(everyone);
    return Promise.resolve(api.body);
  },
}));

import { savedText, TimeSavedPanel } from "./time-saved";

const weeks = ["2026-10-19", "2026-10-26", "2026-11-02", "2026-11-09"];
const person = (name: string, mins: number[], items: Record<string, number> = {}, mine = false) => ({
  name,
  mine,
  weeks: weeks.map((start, i) => ({ start, minutes: mins[i], items: i === 3 ? items : {} })),
});
const base = { available: true, weeks, minutes: { mail_sent: 6, record: 5, teams_sent: 2 }, basis: {}, method: "An estimate, not a measurement: a mail 6 min." };

describe("time saved", () => {
  it("shows the reader's week, the trend, what counted and how it is counted", async () => {
    api.body = { ...base, all_readers: false, people: [person("Ana", [0, 30, 90, 146], { mail_sent: 11, record: 16, teams_sent: 1 }, true)] };
    render(<TimeSavedPanel everyone={false} />);
    const panel = await screen.findByTestId("time-saved");
    expect(panel).toHaveTextContent("Time saved this week (estimate)");
    expect(panel).toHaveTextContent("2.4 h");
    expect(panel).toHaveTextContent("last week 1.5 h · 4 weeks 4.4 h");
    expect(panel).toHaveTextContent("16 lookups and entries done for you · 11 messages sent from a draft · 1 Teams message");
    expect(screen.getByTestId("time-saved-method")).toHaveTextContent("An estimate, not a measurement");
  });

  it("gives the owner one row per person, and nothing at all when the log is closed", async () => {
    api.body = { ...base, all_readers: true, people: [person("Vince", [0, 0, 60, 125], { record: 25 }), person("Stephen", [0, 0, 0, 12], { mail_sent: 2 })] };
    const { unmount } = render(<TimeSavedPanel everyone />);
    const rows = (await screen.findByTestId("time-saved")).querySelectorAll("tbody tr");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Vince2.1 h1.0 h3.1 h");
    expect(rows[1]).toHaveTextContent("Stephen12 min0 min12 min");
    unmount();
    api.body = { available: false, all_readers: false, weeks: [], people: [] };
    render(<TimeSavedPanel everyone={false} />);
    await Promise.resolve();
    expect(screen.queryByTestId("time-saved")).toBeNull();
    expect(api.asked).toEqual([false, true, false]);
  });

  it("says minutes under an hour and hours with one decimal under ten", () => {
    expect([savedText(0), savedText(59), savedText(60), savedText(596), savedText(597)]).toEqual(["0 min", "59 min", "1.0 h", "9.9 h", "10 h"]);
  });
});
