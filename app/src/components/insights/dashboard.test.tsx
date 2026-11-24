import { chartNotes } from "@/components/charts/chart-view";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiErrorFrom } from "@/lib/errors";
import type { InsightList, InsightResult } from "@/lib/types";

const getInsight = vi.fn();
const deleteInsight = vi.fn();
const saveInsight = vi.fn();
const setInsightSchedule = vi.fn();
const readInsightFiltered = vi.fn();
vi.mock("@/lib/api", () => ({
  readInsightFiltered: (...a: unknown[]) => readInsightFiltered(...a),
  setInsightSchedule: (...a: unknown[]) => setInsightSchedule(...a),
  getInsight: (...a: unknown[]) => getInsight(...a),
  deleteInsight: (...a: unknown[]) => deleteInsight(...a),
  saveInsight: (...a: unknown[]) => saveInsight(...a),
}));

import { AnswerCharts } from "@/components/answer-chart";
import { InsightsDashboard, cellText, ScheduleControl, TOP, askAbout, grouped, monitorLine, tellMeWhenHref, topOf } from "./dashboard";

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

describe("insights dashboard in one request", () => {
  it("draws what the one request gave, asks for nothing itself, waits for a tile not yet read, and Refresh reads that tile again", async () => {
    const feed = { team_output: { result: result("team_output") }, s7: { error: { code: "insight_not_ready", status: 503 } } };
    const { rerender } = render(<InsightsDashboard list={list} onChanged={vi.fn()} feed={{ team_output: feed.team_output }} streaming />);
    await waitFor(() => expect(screen.getByRole("img", { name: "Work sent per person" })).toBeTruthy());
    expect(screen.getAllByText("Reading").length).toBe(1);   // s7 not arrived yet
    rerender(<InsightsDashboard list={list} onChanged={vi.fn()} feed={feed} streaming={false} />);
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());   // its own error, the other tile untouched
    expect(screen.getByRole("img", { name: "Work sent per person" })).toBeTruthy();
    expect(getInsight).not.toHaveBeenCalled();
    fireEvent.click(screen.getAllByText("Refresh")[0]);
    await waitFor(() => expect(getInsight).toHaveBeenCalledWith("team_output", expect.anything(), true, expect.anything()));
  });
});

describe("a measure's tile", () => {
  it("states the agreed definition its figures are, with owner, version and the day it was set, without opening Details", async () => {
    const measure = { id: "revenue", name: "Revenue invoiced", definition: "Net line revenue on posted sales invoices.", owner: "Finance", version: "v78103d58", defined_on: "2026-11-23" };
    const tile = { ...base, id: "measure_revenue", title: "Revenue invoiced", group: "Measures: Finance", chart: null, measure };
    const one: InsightList = { ...list, items: [tile] };
    getInsight.mockImplementation(() => Promise.resolve({ ...result("team_output"), ...tile, drawn: null }));
    render(<InsightsDashboard list={one} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByTestId("measure-definition-measure_revenue")).toBeTruthy());
    expect(screen.getByTestId("measure-definition-measure_revenue")).toHaveTextContent(
      "Revenue invoiced (agreed definition): Net line revenue on posted sales invoices. Owner Finance, definition v78103d58, set 23 Nov 2026.");
  });
});

describe("a tile shows its work", () => {
  it("lists the views and filters its statement reads; a filter taken out reads the tile again as the reader, and Reset reads it as it is", async () => {
    const work = { views: ["emails", "staff"], more_filters: 2, filters: [{ index: 0, text: "sent_at >= now() - interval '28 days'" }, { index: 1, text: "NOT is_bulk" }] };
    getInsight.mockImplementation((id: string) => Promise.resolve({ ...result(id), work }));
    readInsightFiltered.mockReset().mockResolvedValue({ ...result("team_output"), rows: [["B. Person", "40"]], drawn: null, work: { ...work, filters: [] } });
    render(<InsightsDashboard list={{ ...list, items: [list.items[0]] }} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("img", { name: "Work sent per person" })).toBeTruthy());
    fireEvent.click(screen.getByText("Details"));
    const shown = screen.getByTestId("insight-work-team_output");
    expect(shown).toHaveTextContent("Reads emails, staff");
    expect(shown).toHaveTextContent("2 further filters are in the whole query (Show query)");
    expect(screen.queryByText("SELECT person, mail_sent FROM x")).toBeNull();   // words first; the statement when asked
    fireEvent.click(screen.getByText("Show query"));
    expect(screen.getByText("SELECT person, mail_sent FROM x")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "NOT is_bulk" }));
    fireEvent.click(screen.getByRole("button", { name: "Take out" }));
    await waitFor(() => expect(screen.getByTestId("filtered-team_output")).toBeTruthy());
    expect(readInsightFiltered).toHaveBeenCalledWith("team_output", [{ index: 1, text: null }]);
    expect(screen.getAllByText("B. Person").length).toBeGreaterThan(0);
    // the chips stay the tile's own statement's, not the changed read's
    expect(screen.getByRole("button", { name: "NOT is_bulk" })).toBeTruthy();
    getInsight.mockClear();
    fireEvent.click(screen.getByText("Reset filters"));
    await waitFor(() => expect(getInsight).toHaveBeenCalledWith("team_output", expect.anything(), true, expect.anything()));
    await waitFor(() => expect(screen.queryByTestId("filtered-team_output")).toBeNull());
  });
});

describe("insights tiles that are cut or cannot be measured", () => {
  it("says first N of M for a cut list, and not measurable instead of an empty chart", async () => {
    const cut = { ...result("s7"), truncated: true, row_count: 118, total_rows: 233, rows: [["A. Person", "12"]] };
    const unknown = { ...result("team_output"), rows: [], row_count: 0, drawn: null, not_measurable: "Not measurable yet: emails.owner_role is not filled in for any message." };
    render(<InsightsDashboard list={list} onChanged={vi.fn()} feed={{ s7: { result: cut }, team_output: { result: unknown } }} streaming={false} />);
    await waitFor(() => expect(screen.getAllByText(/First 118 of 233 rows/).length).toBeGreaterThan(0));
    expect(screen.getByTestId("unmeasurable-team_output").textContent).toMatch(/Not measurable yet/);
    expect(screen.queryByRole("img", { name: "Work sent per person" })).toBeNull();
  });
});

describe("a measure tile's stated exclusions", () => {
  it("prints how many records the definition leaves out, in its own words, and says one not counted", async () => {
    const late = { ...result("s7"), excluded: [
      { parameter: "bc_only", says: "held only in Business Central", n: 1, value: "1", why: null },
      { parameter: "other", says: "in another system", n: null, value: null, why: "too slow" }] };
    render(<InsightsDashboard list={list} onChanged={vi.fn()} feed={{ s7: { result: late } }} streaming={false} />);
    await waitFor(() => expect(screen.getByTestId("excluded-s7-bc_only").textContent).toBe("1 more held only in Business Central"));
    expect(screen.getByTestId("excluded-s7-other").textContent).toMatch(/Not counted: what is in another system \(too slow\)/);
  });
});

describe("insights dashboard in one request, a tile left for later", () => {
  it("a tile the stream left for later reads itself only once it nears the view", async () => {
    const watched: { el: Element; cb: IntersectionObserverCallback }[] = [];
    class Seen {
      constructor(private cb: IntersectionObserverCallback) {}
      observe(el: Element) { watched.push({ el, cb: this.cb }); }
      disconnect() {}
      unobserve() {}
      takeRecords() { return []; }
    }
    vi.stubGlobal("IntersectionObserver", Seen);
    try {
      const feed = { team_output: { later: true }, s7: { result: result("s7") } };
      render(<InsightsDashboard list={list} onChanged={vi.fn()} feed={feed} streaming={false} />);
      await waitFor(() => expect(screen.getByTestId("unread-team_output")).toBeTruthy());
      expect(getInsight).not.toHaveBeenCalled();
      // the streamed tile is drawn and watches nothing; the later one waits for the view
      expect(watched.map((w) => w.el.id)).toEqual(["insight-team_output"]);
      watched[0].cb([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver);
      await waitFor(() => expect(screen.getByRole("img", { name: "Work sent per person" })).toBeTruthy());
      expect(getInsight).toHaveBeenCalledTimes(1);
      expect(getInsight).toHaveBeenCalledWith("team_output", expect.anything(), false, expect.anything());
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("insights dashboard", () => {
  it("reads a tile only once it nears the view, the tile opened from Home at once", async () => {
    const watched: { el: Element; cb: IntersectionObserverCallback; margin?: string }[] = [];
    class Seen {
      constructor(private cb: IntersectionObserverCallback, private opts?: IntersectionObserverInit) {}
      observe(el: Element) { watched.push({ el, cb: this.cb, margin: this.opts?.rootMargin }); }
      disconnect() {}
      unobserve() {}
      takeRecords() { return []; }
    }
    vi.stubGlobal("IntersectionObserver", Seen);
    window.location.hash = "#insight-s7";
    try {
      render(<InsightsDashboard list={list} onChanged={vi.fn()} />);
      // the tile opened from Home reads at once; the other waits for the view, not drawn as busy
      await waitFor(() => expect(getInsight).toHaveBeenCalledWith("s7", expect.anything(), false, expect.anything()));
      expect(getInsight).not.toHaveBeenCalledWith("team_output", expect.anything(), false, expect.anything());
      expect(screen.getByTestId("unread-team_output")).toBeTruthy();
      const far = watched.find((w) => w.el.id === "insight-team_output")!;
      expect(far.margin).toBeTruthy();
      // not yet in view: still unread
      far.cb([{ isIntersecting: false } as IntersectionObserverEntry], {} as IntersectionObserver);
      expect(getInsight).not.toHaveBeenCalledWith("team_output", expect.anything(), false, expect.anything());
      far.cb([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver);
      await waitFor(() => expect(screen.getByRole("img", { name: "Work sent per person" })).toBeTruthy());
      expect(getInsight.mock.calls.filter((c) => c[0] === "team_output")).toHaveLength(1);
    } finally {
      vi.unstubAllGlobals();
      window.location.hash = "";
    }
  });

  it("groups built-ins by their group and the reader's own under Pinned by you", () => {
    expect(grouped(list.items).map(([g]) => g)).toEqual(["Team", "Pinned by you"]);
  });

  it("reads each insight live, draws it, and opens its definition and rows", async () => {
    const changed = vi.fn();
    render(<InsightsDashboard list={list} onChanged={changed} />);
    expect(screen.getByText("The team views show your own row only.")).toBeTruthy();
    await waitFor(() => expect(screen.getByRole("img", { name: "Work sent per person" })).toBeTruthy());
    expect(getInsight).toHaveBeenCalledWith("team_output", expect.anything(), false, expect.anything());
    // Refresh reads it again now, not the answer kept from a few minutes ago
    fireEvent.click(screen.getAllByText("Refresh")[0]);
    await waitFor(() => expect(getInsight).toHaveBeenCalledWith("team_output", expect.anything(), true, expect.anything()));
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

  it("with no monitor there is no empty Monitors section, and the page says how to make one", () => {
    render(<InsightsDashboard list={{ ...list, saving: true, items: [list.items[0]] }} onChanged={vi.fn()} />);
    expect(screen.queryByTestId("monitors")).toBeNull();
    expect(screen.getByText(/listed under Monitors/)).toBeTruthy();
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

describe("cellText", () => {
  it("says money only where the data pairs the column with its currency", () => {
    const cols = ["amount_due", "amount_due_currency", "margin_pct", "overdue_days", "total_count", "avg_open_days", "po"];
    const row = ["214417.98", "CAD", "12.5", "3.25", "1200.00", "4.5", "45508"];
    const of = (c: string) => cellText(row[cols.indexOf(c)], c, row, cols);
    expect(of("amount_due")).toBe("214,417.98 CAD");
    expect(of("margin_pct")).toBe("12.5");
    expect(of("overdue_days")).toBe("3.25");
    expect(of("total_count")).toBe("1,200");
    expect(of("avg_open_days")).toBe("4.5");
    expect(of("po")).toBe("45508");
    expect(cellText("28411605.52136943189612", "qty", row, cols)).toBe("28,411,605.52");
    expect(cellText(null, "x", row, cols)).toBe("");
  });
  it("a currency column beside an unpaired decimal does not make it money", () => {
    expect(cellText("9.5", "total_count", ["9.5", "CAD"], ["total_count", "currency"])).toBe("9.5");
  });
});
