import { chartNotes } from "@/components/charts/chart-view";
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
import { InsightsDashboard, ScheduleControl, TOP, askAbout, grouped, monitorLine, tellMeWhenHref, topOf } from "./dashboard";

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

  it("reads each insight live, draws it, and opens its definition and rows", async () => {
    const changed = vi.fn();
    render(<InsightsDashboard list={list} onChanged={changed} />);
    expect(screen.getByText("The team views show your own row only.")).toBeTruthy();
    await waitFor(() => expect(screen.getByRole("img", { name: "Work sent per person" })).toBeTruthy());
    expect(getInsight).toHaveBeenCalledWith("team_output", expect.anything(), false);
    // Refresh reads it again now, not the answer kept from a few minutes ago
    fireEvent.click(screen.getAllByText("Refresh")[0]);
    await waitFor(() => expect(getInsight).toHaveBeenCalledWith("team_output", expect.anything(), true));
    fireEvent.click(screen.getAllByText("Details")[0]);
    expect(screen.getByText("Per person: mail sent.")).toBeTruthy();
    // the reader sees what it counts in words, never the database statement
    expect(screen.queryByText("SELECT person, mail_sent FROM x")).toBeNull();
    // each tile asks about itself in Ask
    expect(screen.getAllByRole("link", { name: "Ask about this" })[0].getAttribute("href")).toBe(`/ask?q=${encodeURIComponent(askAbout("Work sent per person"))}`);
    // a saved one without a chart shows its rows; only a saved one can be removed
    await waitFor(() => expect(screen.getAllByText("A. Person").length).toBeGreaterThan(0));
    expect(screen.getAllByText("Remove")).toHaveLength(1);
    fireEvent.click(screen.getByText("Remove"));
    await waitFor(() => expect(deleteInsight).toHaveBeenCalledWith("s7"));
    expect(changed).toHaveBeenCalled();
  });

  it("a chart of many people shows the top ones, every series cut alike, and says how many there are", () => {
    const names = Array.from({ length: 48 }, (_, i) => `P${i}`);
    const chart = { kind: "bar", title: "T", x: "person", labels: names, series: [{ name: "mail_sent", values: names.map((_, i) => 48 - i) }, { name: "teams_sent", values: names.map(() => 1) }] } as unknown as Parameters<typeof topOf>[0];
    const top = topOf(chart);
    expect(top.labels).toEqual(names.slice(0, TOP));
    expect(top.series.map((s) => s.values.length)).toEqual([TOP, TOP]);
    // how many there are is said once, by the tile's own "Top 15 of 48" line, never again under the chart
    expect(chartNotes(top).join(" ")).not.toMatch(/Shows \d+ of/);
    // a short chart, a pie and a scatter are never cut
    expect(topOf({ ...chart, labels: names.slice(0, TOP) })).toEqual({ ...chart, labels: names.slice(0, TOP) });
    const scatter = { ...chart, kind: "scatter" } as typeof chart;
    expect(topOf(scatter)).toBe(scatter);
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

  it("pins every chart of an answer at once, each with its own statement, and shows what a measure's numbers mean", async () => {
    const chart = { kind: "bar" as const, title: "Revenue by customer", x: "customer", labels: ["A"], series: [{ name: "value", values: [3] }], as_of: null, sql: "SELECT customer, value FROM t",
      measure: { id: "revenue", name: "Revenue invoiced", definition: "Net line revenue on posted sales invoices.", owner: "Finance", period: "2026-10 by invoice_date" } };
    const second = { ...chart, title: "Revenue by month", sql: "SELECT month, value FROM t", measure: null };
    render(<AnswerCharts charts={[chart, second, { ...chart, title: "Not pinnable", sql: null }]} question="revenue in october" />);
    expect(screen.getAllByText("Revenue invoiced")).toHaveLength(2);   // the second has no measure
    expect(screen.getAllByText(/Net line revenue on posted sales invoices/)).toHaveLength(2);
    expect(screen.getAllByText("How this was worked out")).toHaveLength(2);
    expect(screen.getByText("SELECT customer, value FROM t")).toBeTruthy();
    fireEvent.click(screen.getByText("Pin all 2 charts"));
    await waitFor(() => expect(screen.getByText("All 2 charts pinned to your charts")).toBeTruthy());
    expect(saveInsight).toHaveBeenCalledTimes(2);
    expect(saveInsight.mock.calls.map((c) => (c[0] as { sql: string }).sql)).toEqual(["SELECT customer, value FROM t", "SELECT month, value FROM t"]);
  });

  it("a single pinnable chart has no pin-all", () => {
    const chart = { kind: "bar" as const, title: "One", x: "x", labels: ["A"], series: [{ name: "v", values: [1] }], as_of: null, sql: "SELECT 1" };
    render(<AnswerCharts charts={[chart, { ...chart, sql: null }]} question="q" />);
    expect(screen.queryByText(/Pin all/)).toBeNull();
  });

  it("lists the agreed measures with their definitions and a question to try each in Ask", () => {
    const measures = [{ id: "revenue", name: "Revenue invoiced", definition: "Net line revenue on posted sales invoices.", owner: "Finance", unit: "money", source: "sales_invoice_lines",
      dimensions: ["customer", "month"], period: "last 30 days by invoice_date", ask: "Why did revenue change last month?" }];
    render(<InsightsDashboard list={{ ...list, measures }} onChanged={() => {}} />);
    fireEvent.click(screen.getByText("Show all 1"));
    expect(screen.getByText("Net line revenue on posted sales invoices.")).toBeTruthy();
    const link = screen.getByText("Why did revenue change last month?").closest("a");
    expect(link?.getAttribute("href")).toBe(`/ask?q=${encodeURIComponent("Why did revenue change last month?")}`);
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
    fireEvent.change(screen.getByLabelText("Check it"), { target: { value: "daily" } });
    await waitFor(() => expect(setInsightSchedule).toHaveBeenCalledWith("s7", { schedule: "daily", notify: "new_rows", watch: null }));
    await waitFor(() => expect(screen.getByLabelText("Tell me on Home")).toBeTruthy());
    expect(screen.getByText(/Last ran Nov 19/)).toBeTruthy();
    // a built-in has no schedule control
    render(<InsightsDashboard list={{ ...list, items: [list.items[0]] }} onChanged={vi.fn()} />);
    expect(screen.queryByTestId("schedule-team_output")).toBeNull();
  });

  it("the monitors the reader runs are listed first, each saying when it checks and what it tells", () => {
    const watched = { ...list.items[1], schedule: "daily" as const, notify: "crosses" as const, watch: { column: "late_orders", op: "above" as const, value: 5, row: "any" as const } };
    render(<InsightsDashboard list={{ ...list, items: [list.items[0], watched] }} onChanged={vi.fn()} />);
    expect(screen.getByTestId("monitors")).toHaveTextContent("Open orders by month");
    expect(screen.getByTestId("monitors")).toHaveTextContent("Every morning, tells you when late orders goes above 5");
    expect(screen.getByTestId("monitors")).not.toHaveTextContent("Work sent per person");   // a built-in runs no schedule
    expect(monitorLine({ ...watched, notify: "new_rows", watch: null })).toBe("Every morning, tells you when a new row appears");
  });

  it("with no monitor the list says how to make one", () => {
    render(<InsightsDashboard list={list} onChanged={vi.fn()} />);
    expect(screen.getByTestId("monitors")).toHaveTextContent("None yet");
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
    expect((screen.getByText("Watch") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Above or below"), { target: { value: "below" } });
    fireEvent.change(screen.getByLabelText("Line"), { target: { value: "30" } });
    fireEvent.click(screen.getByText("Watch"));
    await waitFor(() => expect(setInsightSchedule).toHaveBeenCalledWith("s7", { schedule: "daily", notify: "crosses", watch: { column: "days_late", op: "below", value: 30, row: "any" } }));
    await waitFor(() => expect(screen.getByText("Watching days late below 30")).toBeTruthy());
  });

  it("a chart along an axis is watched on its latest row, so an earlier month never comes back", async () => {
    setInsightSchedule.mockReset();
    setInsightSchedule.mockImplementation((_id: string, body: object) => Promise.resolve({ ...list.items[1], ...body }));
    const saved = { ...list.items[1], schedule: "daily" as const, notify: "crosses" as const, chart: { kind: "line" as const, x: "month", y: ["revenue"] } };
    render(<ScheduleControl item={saved} columns={["month", "currency", "revenue"]} />);
    fireEvent.change(screen.getByLabelText("Column to watch"), { target: { value: "revenue" } });
    fireEvent.change(screen.getByLabelText("Above or below"), { target: { value: "below" } });
    fireEvent.change(screen.getByLabelText("Line"), { target: { value: "400000" } });
    fireEvent.click(screen.getByText("Watch"));
    await waitFor(() => expect(setInsightSchedule).toHaveBeenCalledWith("s7", { schedule: "daily", notify: "crosses", watch: { column: "revenue", op: "below", value: 400000, row: "latest", order: "month" } }));
    await waitFor(() => expect(screen.getByText("Watching the latest revenue below 400000")).toBeTruthy());
    expect(monitorLine({ ...saved, watch: { column: "revenue", op: "below", value: 400000, row: "latest", where: { column: "currency", value: "USD" } } }))
      .toBe("Every morning, tells you when the latest revenue in USD goes below 400000");
  });

  it("'Tell me when' sends the words to the Ask with the chart it is on; nothing is set here", () => {
    setInsightSchedule.mockReset();
    const saved = { ...list.items[1], schedule: "" as const, notify: "never" as const };
    render(<ScheduleControl item={saved} />);
    expect(screen.queryByRole("link", { name: "Set up" })).toBeNull();     // no words yet
    fireEvent.change(screen.getByLabelText("Tell me when"), { target: { value: "usd drops under 400k" } });
    const href = screen.getByRole("link", { name: "Set up" }).getAttribute("href") ?? "";
    expect(href).toBe(tellMeWhenHref(saved, "usd drops under 400k"));
    const url = new URL(href, "http://x");
    expect(url.searchParams.get("q")).toBe('Tell me when usd drops under 400k (my pinned chart "Open orders by month")');
    expect(url.searchParams.get("page")).toBe("/insights#insight-s7");
    expect(setInsightSchedule).not.toHaveBeenCalled();
  });
});
