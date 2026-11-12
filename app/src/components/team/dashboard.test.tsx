import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { TeamPerson, TeamView } from "@/lib/types";
import { TeamDashboard, busiest, minutes } from "./dashboard";

const hours = Array.from({ length: 24 }, (_, h) => (h === 9 ? 30 : h === 14 ? 20 : h === 10 ? 12 : 0));

function person(key: string, name: string, extra: Partial<TeamPerson> = {}): TeamPerson {
  return { key, name, self: false, team: null, roles: [], d7: {}, d28: {}, awaiting_reply: 0, tasks: 0, alerts: 0,
    alerts_closed: 0, replies_28: 0, trend: Array(28).fill(1), hours, ...extra } as TeamPerson;
}

const view: TeamView = {
  scope: "everyone", state: "ready", as_of: "2026-11-10T08:30:00+00:00", window_to: "2026-11-10",
  note: "Mail, Teams, meetings and the systems that record who did what.",
  coverage: [{ system: "bc", share: 0, actions: 50 }],
  people: [
    person("k1", "Avery Lindqvist", { team: "Purchasing", roles: ["Purchasing"], d7: { mail_sent: 12, teams: 3 }, d28: { mail_sent: 40, teams: 30, planner: 2 }, awaiting_reply: 2, reply_minutes_7: 45 }),
    person("k2", "Bea Okafor", { team: "Customer service", roles: ["Customer service", "Receivables"], d7: { mail_sent: 30 }, d28: { mail_sent: 90 }, reply_minutes_7: 200 }),
    person("k3", "Cal Mendes", { d7: {}, d28: { teams: 4 } }),
  ],
};

const names = () => screen.getAllByTestId("team-row").map((r) => within(r).getAllByRole("button")[0].textContent);

describe("Team dashboard", () => {
  it("lists people by name, sorts by any column and leaves out columns nobody has a figure in", () => {
    render(<TeamDashboard data={view} />);
    expect(names()).toEqual(["Bea Okafor", "Avery Lindqvist", "Cal Mendes"]);   // most activity first
    const table = screen.getByTestId("team-table");
    expect(within(table).queryByTitle(/Planner tasks/)).toBeNull();              // nobody in 7 days
    expect(within(table).queryByTitle(/Business Central/)).toBeNull();
    fireEvent.click(within(table).getByText("Person"));
    expect(names()).toEqual(["Avery Lindqvist", "Bea Okafor", "Cal Mendes"]);
    fireEvent.click(within(table).getByText("Median"));
    expect(names()[0]).toBe("Bea Okafor");
    fireEvent.click(screen.getByTestId("period-28"));
    expect(within(table).getByTitle(/Planner tasks/)).toBeTruthy();
    // no disclaimer: only the count time, on one short line
    const note = screen.getByTestId("team-note");
    expect(note.textContent?.length).toBeLessThan(40);
    expect(note.getAttribute("title")).toBeNull();
    expect(screen.queryByText(/What is counted/)).toBeNull();
  });

  it("filters by team, role and system", () => {
    render(<TeamDashboard data={view} />);
    fireEvent.change(screen.getByTestId("filter-team"), { target: { value: "Purchasing" } });
    expect(names()).toEqual(["Avery Lindqvist"]);
    fireEvent.change(screen.getByTestId("filter-team"), { target: { value: "-" } });
    expect(names()).toEqual(["Cal Mendes"]);
    fireEvent.change(screen.getByTestId("filter-team"), { target: { value: "" } });
    fireEvent.change(screen.getByTestId("filter-role"), { target: { value: "Receivables" } });
    expect(names()).toEqual(["Bea Okafor"]);
    fireEvent.change(screen.getByTestId("filter-role"), { target: { value: "" } });
    fireEvent.change(screen.getByTestId("filter-system"), { target: { value: "teams" } });
    expect(names()).toEqual(["Avery Lindqvist"]);
  });

  it("a reader who sees only their own row gets no filters; an unbuilt count says so", () => {
    const { rerender } = render(<TeamDashboard data={{ ...view, scope: "self", people: [person("k9", "Jo Pemberton", { self: true })] }} />);
    expect(screen.queryByTestId("filter-team")).toBeNull();
    expect(screen.getByText("you")).toBeTruthy();
    rerender(<TeamDashboard data={{ ...view, state: "not_built", people: [] }} />);
    expect(screen.getByTestId("team-not-ready").textContent).toMatch(/every night/);
  });

  it("formats reply times and busiest hours", () => {
    expect([minutes(0.2), minutes(45), minutes(200), minutes(3000), minutes(null)]).toEqual(["1m", "45m", "3.3h", "2.1d", ""]);
    expect(busiest(hours)).toBe("9a, 10a, 2p");
  });
});
