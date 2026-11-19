import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { TeamPerson, TeamView } from "@/lib/types";
import { TeamDashboard, WeekTracker, busiest, minutes } from "./dashboard";

vi.mock("@/lib/api", () => ({
  getTeamPerson: vi.fn(async (key: string) => ({ person: person(key, "Avery Lindqvist"), items: [], withheld: 0, truncated: false, timeline: [], partners: [] })),
}));

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
    // nobody has an open task or alert: no columns of zeros for them
    expect(within(table).queryByTitle("Open tasks")).toBeNull();
    expect(within(table).queryByTitle("Other open alerts")).toBeNull();
    expect(within(table).getByTitle("Threads awaiting their reply")).toBeTruthy();
    // every row has as many cells as the header has columns
    const cols = within(table).getAllByRole("columnheader").filter((th) => th.closest("tr") === table.querySelectorAll("thead tr")[1]).length;
    for (const r of screen.getAllByTestId("team-row")) expect(r.querySelectorAll("td")).toHaveLength(cols);
    // one sentence on what the table is for
    expect(screen.getByTestId("team-intro").textContent).toMatch(/^Who is busy and what is waiting on whom/);
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

  it("with no team or role on record for anyone, the Team column and the Team and Role filters are left out", () => {
    render(<TeamDashboard data={{ ...view, people: (view.people ?? []).map((p) => ({ ...p, team: null, roles: [] })) }} />);
    const table = screen.getByTestId("team-table");
    expect(within(table).queryByText("Team")).toBeNull();
    expect(screen.queryByTestId("filter-team")).toBeNull();
    expect(screen.queryByTestId("filter-role")).toBeNull();
    expect(screen.getByTestId("filter-system")).toBeTruthy();
    expect(names()).toHaveLength(3);
  });

  it("a reader who sees only their own row gets no filters; an unbuilt count says so", () => {
    const { rerender } = render(<TeamDashboard data={{ ...view, scope: "self", people: [person("k9", "Jo Pemberton", { self: true })] }} />);
    expect(screen.queryByTestId("filter-team")).toBeNull();
    expect(screen.getByText("you")).toBeTruthy();
    rerender(<TeamDashboard data={{ ...view, state: "not_built", people: [] }} />);
    expect(screen.getByTestId("team-not-ready").textContent).toMatch(/every night/);
  });

  it("says a role under the name only when it is not the team already shown", () => {
    render(<TeamDashboard data={view} />);
    const [avery, bea] = ["Avery Lindqvist", "Bea Okafor"].map((n) => screen.getAllByTestId("team-row").find((r) => r.textContent?.includes(n))!);
    expect(within(avery).queryByTestId("team-roles")).toBeNull();
    expect(within(bea).getByTestId("team-roles")).toHaveTextContent(/^Receivables$/);
  });

  it("formats reply times and busiest hours", () => {
    expect([minutes(0.2), minutes(45), minutes(200), minutes(3000), minutes(null)]).toEqual(["1m", "45m", "3.3h", "2.1d", ""]);
    expect(busiest(hours)).toBe("9a, 10a, 2p");
  });

  it("the person sheet labels the busiest-hours chart under its own bars", async () => {
    render(<TeamDashboard data={view} />);
    fireEvent.click(screen.getAllByTestId("team-row")[0]);
    const chart = await screen.findByTestId("team-hours");
    const bars = chart.querySelectorAll("rect");
    const labels = [...chart.querySelectorAll("text")];
    expect(labels.map((t) => t.textContent)).toEqual(["12a", "6a", "12p", "6p", "11p"]);
    expect(screen.getByTestId("team-person")).toHaveTextContent("Busiest: 9a, 10a, 2p");
    for (const [i, h] of [[1, 6], [2, 12], [3, 18]]) {
      const bar = bars[h];
      expect(Number(labels[i].getAttribute("x"))).toBe(Number(bar.getAttribute("x")) + Number(bar.getAttribute("width")) / 2);
    }
  });
});

describe("WeekTracker", () => {
  const weeks = [
    { week: "2026-10-19", to: "2026-10-25", partial: false, done: 120, received: 80, closed: 2, reply_minutes: 45, replies: 9, saved_minutes: 30 },
    { week: "2026-10-26", to: "2026-11-01", partial: false, done: 0, received: 0, closed: 0, reply_minutes: null, replies: 0, saved_minutes: 0 },
    { week: "2026-11-02", to: "2026-11-08", partial: false, done: 1400, received: 300, closed: 0, reply_minutes: 125, replies: 31, saved_minutes: null },
    { week: "2026-11-09", to: "2026-11-11", partial: true, done: 40, received: 12, closed: 1, reply_minutes: 20, replies: 3, saved_minutes: 95 },
  ];

  it("shows each week's work against what came in, the open week marked, and what waits now", () => {
    render(<WeekTracker weeks={weeks} waiting={4} />);
    const rows = screen.getAllByTestId("team-week-row");
    expect(rows).toHaveLength(4);
    expect(within(rows[2]).getByText("1,400")).toBeTruthy();
    expect(rows[3].textContent).toContain("to Nov 11");
    expect(rows[0].textContent).not.toContain("to ");
    expect(rows[3].textContent).toContain(minutes(95));
    expect(rows[3].textContent).toContain("(3)");
    // a week with no replies shows no median, never "0 min"
    expect(rows[1].textContent).not.toContain("min");
    expect(screen.getByTestId("team-waiting").textContent).toBe("4 waiting on them now (replies, tasks and alerts).");
  });

  it("drops the saved column when the reader may see no figure", () => {
    render(<WeekTracker weeks={weeks.map((w) => ({ ...w, saved_minutes: null }))} waiting={0} />);
    expect(screen.queryByTitle(/Workbench saved/)).toBeNull();
    expect(screen.getByTestId("team-waiting").textContent).toBe("Nothing waiting on them now.");
  });
});
