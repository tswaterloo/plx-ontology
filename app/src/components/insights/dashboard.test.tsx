import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiErrorFrom } from "@/lib/errors";
import type { InsightList, InsightResult } from "@/lib/types";

const getInsight = vi.fn();
const deleteInsight = vi.fn();
const saveInsight = vi.fn();
const setInsightSchedule = vi.fn();
vi.mock("@/lib/api", () => ({
  setInsightSchedule: (...a: unknown[]) => setInsightSchedule(...a),
  getInsight: (...a: unknown[]) => getInsight(...a),
  deleteInsight: (...a: unknown[]) => deleteInsight(...a),
  saveInsight: (...a: unknown[]) => saveInsight(...a),
}));

import { AnswerCharts } from "@/components/answer-chart";
import { InsightsDashboard, ScheduleControl, grouped } from "./dashboard";

const base = { about: "Per person: mail sent.", group: "Team", saved: false, question: null, schedule: "" as const, notify: "never" as const };
const list: InsightList = {
  scope: "self",
  saving: true,
  items: [
    { ...base, id: "team_output", title: "Work sent per person", chart: { kind: "bar", x: "person", y: ["mail_sent"] } },
    { ...base, id: "s7", title: "Open orders by month", group: "", saved: true, chart: null },
  ],
};
const result = (id: string): InsightResult => ({
  ...list.items.find((i) => i.id === id)!,
  sql: "SELECT person, mail_sent FROM x",
  columns: ["person", "mail_sent"],
  rows: [["A. Person", "12"]],
  people: ["k1"],
  row_count: 1,
  truncated: false,
  as_of: null,
  read_at: "2026-11-19T03:00:00+00:00",
  drawn: id === "team_output"
    ? { kind: "bar", title: "Work sent per person", x: "person", labels: ["A. Person"], series: [{ name: "mail_sent", values: [12] }], as_of: null, sql: "SELECT 1" }
    : null,
});

beforeEach(() => {
  getInsight.mockReset().mockImplementation((id: string) => Promise.resolve(result(id)));
  deleteInsight.mockReset().mockResolvedValue(undefined);
  saveInsight.mockReset().mockResolvedValue({});
});

describe("insights dashboard", () => {
  it("groups built-ins by their group and the reader's own under Pinned by you", () => {
    expect(grouped(list.items).map(([g]) => g)).toEqual(["Team", "Pinned by you"]);
  });

  it("reads each insight live, draws it, and opens its definition, statement and rows", async () => {
    const changed = vi.fn();
    render(<InsightsDashboard list={list} onChanged={changed} />);
    expect(screen.getByText("The team views show your own row only.")).toBeTruthy();
    await waitFor(() => expect(screen.getByRole("img", { name: "Work sent per person" })).toBeTruthy());
    expect(getInsight).toHaveBeenCalledWith("team_output", expect.anything(), false);
    // Refresh reads it again now, not the answer kept from a few minutes ago
    fireEvent.click(screen.getAllByText("Refresh")[0]);
    await waitFor(() => expect(getInsight).toHaveBeenCalledWith("team_output", expect.anything(), true));
    fireEvent.click(screen.getAllByText("Definition and rows")[0]);
    expect(screen.getByText("Per person: mail sent.")).toBeTruthy();
    expect(screen.getByText("SELECT person, mail_sent FROM x")).toBeTruthy();
    // a saved one without a chart shows its rows; only a saved one can be removed
    await waitFor(() => expect(screen.getAllByText("A. Person").length).toBeGreaterThan(0));
    expect(screen.getAllByText("Remove")).toHaveLength(1);
    fireEvent.click(screen.getByText("Remove"));
    await waitFor(() => expect(deleteInsight).toHaveBeenCalledWith("s7"));
    expect(changed).toHaveBeenCalled();
  });

  it("a person's name in the rows opens them on Team by the row's Team key", async () => {
    render(<InsightsDashboard list={list} onChanged={vi.fn()} />);
    const link = await screen.findByRole("link", { name: "A. Person" });   // s7 has no chart: its rows show
    expect(link.getAttribute("href")).toBe("/team?person=k1");
  });

  it("pins an answer's chart with its own statement and columns, and only a chart that has one", async () => {
    const chart = { kind: "bar" as const, title: "Top customers", x: "customer", labels: ["A"], series: [{ name: "total", values: [3] }], as_of: null, sql: "SELECT customer, total FROM t" };
    render(<AnswerCharts charts={[chart, { ...chart, title: "No statement", sql: null }]} question="top customers this year" />);
    expect(screen.getAllByText("Pin to your charts")).toHaveLength(1);
    fireEvent.click(screen.getByText("Pin to your charts"));
    await waitFor(() => expect(screen.getByText("Pinned to your charts")).toBeTruthy());
    expect(saveInsight).toHaveBeenCalledWith({ title: "Top customers", question: "top customers this year", sql: "SELECT customer, total FROM t", chart: { kind: "bar", x: "customer", y: ["total"] }, schedule: "", notify: "never" });
  });

  it("a tile that cannot be read says what is missing, never asks the reader to rephrase it", async () => {
    const said = "This view needs staff.person_id (their directory id), which the business tables on this server don't have yet. It shows once they are next rebuilt.";
    const failed = await apiErrorFrom(new Response(JSON.stringify({ error: { code: "insight_not_built", message: said } }), { status: 503 }));
    // an unknown code from the same route still gets a plain sentence, not the server's text
    const odd = await apiErrorFrom(new Response(JSON.stringify({ error: { code: "something_new", message: "column x does not exist" } }), { status: 422 }));
    getInsight.mockImplementation((id: string) => Promise.reject(id === "team_output" ? failed : odd));
    render(<InsightsDashboard list={list} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(said)).toBeTruthy());
    expect(screen.getByText(/This view could not be read just now/)).toBeTruthy();
    expect(screen.queryByText(/does not exist|rephras/i)).toBeNull();
  });

  it("a saved insight is scheduled to run by itself and says when it last ran", async () => {
    setInsightSchedule.mockImplementation((_id: string, body: { schedule: string; notify: string }) => Promise.resolve({ ...list.items[1], ...body }));
    const saved = { ...list.items[1], schedule: "" as const, notify: "never" as const, last_run: "Last ran Nov 19, 7:02 AM ET: 4 rows, 1 new." };
    render(<ScheduleControl item={saved} />);
    expect(screen.queryByText(/Last ran/)).toBeNull();          // not scheduled: no run line
    fireEvent.change(screen.getByLabelText("Runs by itself"), { target: { value: "daily" } });
    await waitFor(() => expect(setInsightSchedule).toHaveBeenCalledWith("s7", { schedule: "daily", notify: "new_rows", watch: null }));
    await waitFor(() => expect(screen.getByLabelText("Tell me on Home")).toBeTruthy());
    expect(screen.getByText(/Last ran Nov 19/)).toBeTruthy();
    // a built-in has no schedule control
    render(<InsightsDashboard list={{ ...list, items: [list.items[0]] }} onChanged={vi.fn()} />);
    expect(screen.queryByTestId("schedule-team_output")).toBeNull();
  });

  it("a number is watched against a line once the line is complete", async () => {
    setInsightSchedule.mockReset();
    setInsightSchedule.mockImplementation((_id: string, body: object) => Promise.resolve({ ...list.items[1], ...body }));
    const saved = { ...list.items[1], schedule: "daily" as const, notify: "rows" as const };
    render(<ScheduleControl item={saved} columns={["po", "days_late"]} />);
    fireEvent.change(screen.getByLabelText("Tell me on Home"), { target: { value: "crosses" } });
    expect(setInsightSchedule).not.toHaveBeenCalled();          // no line yet: nothing sent
    fireEvent.change(screen.getByLabelText("Column to watch"), { target: { value: "days_late" } });
    fireEvent.change(screen.getByLabelText("Line"), { target: { value: "abc" } });
    expect((screen.getByText("Save line") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Above or below"), { target: { value: "below" } });
    fireEvent.change(screen.getByLabelText("Line"), { target: { value: "30" } });
    fireEvent.click(screen.getByText("Save line"));
    await waitFor(() => expect(setInsightSchedule).toHaveBeenCalledWith("s7", { schedule: "daily", notify: "crosses", watch: { column: "days_late", op: "below", value: 30 } }));
    await waitFor(() => expect(screen.getByText("Watching days late below 30")).toBeTruthy());
  });
});
