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
const proposeMeasureCandidate = vi.fn();
const getObserved = vi.fn();
const getGaps = vi.fn();
const sendNoteFeedback = vi.fn();
const getMeasureCandidates = vi.fn().mockResolvedValue({ candidates: [], counts: { pending: 0, approved: 0, dismissed: 0 }, total: 0, can_review: true });
const approveMeasureCandidate = vi.fn();
const dismissMeasureCandidate = vi.fn();
const getFeedback = vi.fn().mockResolvedValue({ entries: [], all_readers: false });
const getAllFeedback = vi.fn().mockResolvedValue({ entries: [], all_readers: false });
const setFeedbackStatus = vi.fn();
vi.mock("@/lib/api", () => ({
  readInsightFiltered: (...a: unknown[]) => readInsightFiltered(...a),
  setInsightSchedule: (...a: unknown[]) => setInsightSchedule(...a),
  getInsight: (...a: unknown[]) => getInsight(...a),
  deleteInsight: (...a: unknown[]) => deleteInsight(...a),
  saveInsight: (...a: unknown[]) => saveInsight(...a),
  proposeMeasureCandidate: (...a: unknown[]) => proposeMeasureCandidate(...a),
  getObserved: (...a: unknown[]) => getObserved(...a),
  getGaps: (...a: unknown[]) => getGaps(...a),
  sendNoteFeedback: (...a: unknown[]) => sendNoteFeedback(...a),
  getMeasureCandidates: (...a: unknown[]) => getMeasureCandidates(...a),
  approveMeasureCandidate: (...a: unknown[]) => approveMeasureCandidate(...a),
  dismissMeasureCandidate: (...a: unknown[]) => dismissMeasureCandidate(...a),
  getFeedback: (...a: unknown[]) => getFeedback(...a),
  getAllFeedback: (...a: unknown[]) => getAllFeedback(...a),
  setFeedbackStatus: (...a: unknown[]) => setFeedbackStatus(...a),
}));

vi.mock("@/components/insights/ask-here", () => ({ InsightsAsk: () => <div data-testid="insights-ask" /> }));

import { AnswerCharts } from "@/components/answer-chart";
import { PinnedHere } from "@/components/insights/pinned";
import { InsightsDashboard, cellText, chartPointCount, moved, ScheduleControl, TOP, placed, askAbout, grouped, monitorLine, tellMeWhenHref, topOf } from "./dashboard";

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
  window.localStorage.clear();
  getInsight.mockReset().mockImplementation((id: string) => Promise.resolve(result(id)));
  deleteInsight.mockReset().mockResolvedValue(undefined);
  saveInsight.mockReset().mockResolvedValue({});
  proposeMeasureCandidate.mockReset().mockResolvedValue({ id: "mc-1", status: "pending" });
  getObserved.mockReset().mockResolvedValue({ available: true, days: [], failures: [] });
  getGaps.mockReset().mockResolvedValue({ on: true, gaps: [] });
  sendNoteFeedback.mockReset().mockResolvedValue({ id: "fb-1", status: "open" });
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

  it("shows a clean Preparing chart state while warming after a cold start (waiting / 425) and draws once ready", async () => {
    let finish: (r: InsightResult) => void = () => undefined;
    getInsight.mockImplementation((_id: string, _sig?: AbortSignal, _again?: boolean, onStored?: { onFilling?: () => void }) => {
      onStored?.onFilling?.();
      return new Promise<InsightResult>((res) => { finish = res; });
    });
    const feed = { team_output: { later: true, waiting: true }, s7: { result: result("s7") } };
    render(<InsightsDashboard list={list} onChanged={vi.fn()} feed={feed} streaming={false} />);
    await waitFor(() => expect(screen.getByText("Preparing chart...")).toBeTruthy());
    expect(screen.queryByRole("alert")).toBeNull();
    finish(result("team_output"));
    await waitFor(() => expect(screen.getByRole("img", { name: "Work sent per person" })).toBeTruthy());
    expect(screen.queryByText("Preparing chart...")).toBeNull();
  });
});

describe("a measure's tile", () => {
  it("shows compact owner/version in the header and full agreed definition inside Details", async () => {
    const measure = { id: "revenue", name: "Revenue invoiced", definition: "Net line revenue on posted sales invoices.", owner: "Finance", version: "v78103d58", defined_on: "2026-11-23" };
    const tile = { ...base, id: "measure_revenue", title: "Revenue invoiced", group: "Measures: Finance", chart: null, measure };
    const one: InsightList = { ...list, items: [tile] };
    getInsight.mockImplementation(() => Promise.resolve({ ...result("team_output"), ...tile, drawn: null }));
    render(<InsightsDashboard list={one} onChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByTestId("verified-pill-measure_revenue")).toBeTruthy());
    expect(screen.getByTestId("verified-pill-measure_revenue")).toHaveTextContent("Finance");
    expect(screen.getByTestId("verified-pill-measure_revenue")).toHaveTextContent("v78103d58");
    fireEvent.click(screen.getByText("Details"));
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
      // every tile watches the view (it is drawn only near it); the later one also reads then
      expect(watched.map((w) => w.el.id)).toEqual(["insight-team_output", "insight-s7"]);
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
    // the tile's one-line takeaway is its about line (each tile shows it, opened or not)
    expect(screen.getAllByText("Per person: mail sent.").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Hide details").length).toBe(1);
    // the reader sees what it counts in words, never the database statement
    expect(screen.queryByText("SELECT person, mail_sent FROM x")).toBeNull();
    // each tile asks about itself in Ask
    expect(screen.getAllByRole("link", { name: "Ask about this" })[0].getAttribute("href")).toBe(`/ask?q=${encodeURIComponent(askAbout("Work sent per person"))}`);
    // a saved one without a chart shows its rows; only a saved one can be removed via its overflow menu
    await waitFor(() => expect(screen.getAllByText("A. Person").length).toBeGreaterThan(0));
    fireEvent.click(screen.getByTestId("tile-menu-s7"));
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

  it("lists the agreed measures in Ontology & Measures with their definitions and a question to try each in Ask", () => {
    const measures = [{ id: "revenue", name: "Revenue invoiced", definition: "Net line revenue on posted sales invoices.", owner: "Finance", unit: "money", source: "sales_invoice_lines",
      dimensions: ["customer", "month"], period: "last 30 days by invoice_date", ask: "Why did revenue change last month?" }];
    render(<InsightsDashboard list={{ ...list, measures }} onChanged={() => {}} />);
    // No redundant Measures accordion dumped at the bottom of the Dashboard tab
    expect(screen.queryByText("Show all 1")).toBeNull();
    fireEvent.click(screen.getByTestId("tab-ontology"));
    expect(screen.getByTestId("ontology-inspector")).toHaveTextContent("Net line revenue on posted sales invoices.");
    const askLink = screen.getByTestId("inspector-ask-btn");
    expect(askLink.getAttribute("href")).toBe(`/ask?q=${encodeURIComponent("Why did revenue change last month?")}`);
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
    expect(screen.getByTestId("monitors")).toHaveTextContent("Daily schedule, alerts when late orders goes above 5");
    expect(screen.getByTestId("monitors")).not.toHaveTextContent("Work sent per person");   // a built-in runs no schedule
    expect(monitorLine({ ...watched, notify: "new_rows", watch: null })).toBe("Daily schedule, alerts on new rows");
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
      .toBe("Daily schedule, alerts when the latest revenue in USD goes below 400000");
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

const seeing = () => {
  const watched: { el: Element; cb: IntersectionObserverCallback }[] = [];
  class Seen {
    constructor(private cb: IntersectionObserverCallback) {}
    observe(el: Element) { watched.push({ el, cb: this.cb }); }
    disconnect() {}
    unobserve() {}
    takeRecords() { return []; }
  }
  vi.stubGlobal("IntersectionObserver", Seen);
  return { watched, show: (id: string, on: boolean) => watched.find((w) => w.el.id === `insight-${id}`)!.cb([{ isIntersecting: on } as IntersectionObserverEntry], {} as IntersectionObserver) };
};

describe("a tile is drawn only while it is near the view", () => {
  it.each([1, 10, 60])("N=%i stored tiles: none drawn until near, each drawn once near, undrawn when it leaves", async (n) => {
    const seen = seeing();
    try {
      const items = Array.from({ length: n }, (_, i) => ({ ...list.items[0], id: `t${i}`, title: `Tile ${i}` }));
      const feed = Object.fromEntries(items.map((it) => [it.id, { result: { ...result("team_output"), ...it } }]));
      render(<InsightsDashboard list={{ ...list, items }} onChanged={vi.fn()} feed={feed} streaming={false} />);
      await waitFor(() => expect(screen.getAllByTestId(/^undrawn-/)).toHaveLength(n));   // none lost
      expect(screen.queryAllByRole("img")).toHaveLength(0);                              // none drawn
      expect(getInsight).not.toHaveBeenCalled();
      const few = Math.min(n, 3);
      for (let i = 0; i < few; i++) seen.show(`t${i}`, true);
      await waitFor(() => expect(screen.getAllByRole("img")).toHaveLength(few));        // exactly those
      expect(screen.queryAllByTestId(/^undrawn-/)).toHaveLength(n - few);
      seen.show("t0", false);
      await waitFor(() => expect(screen.getByTestId("undrawn-t0")).toBeTruthy());
      expect(screen.queryAllByRole("img")).toHaveLength(few - 1);
      seen.show("t0", true);
      await waitFor(() => expect(screen.getAllByRole("img")).toHaveLength(few));
      expect(screen.queryByTestId("undrawn-t0")).toBeNull();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("arranging your dashboard", () => {
  const three: InsightList = { ...list, scope: "everyone", items: [
    { ...base, id: "a", title: "A", chart: null }, { ...base, id: "b", title: "B", chart: null },
    { ...base, id: "c", title: "C", group: "Org", chart: null }] };

  it("moved: a tile swaps with its neighbour in its own group only, and stays when it cannot move", () => {
    expect(moved(three.items, "a", 1)).toEqual(["b", "a", "c"]);
    expect(moved(three.items, "b", -1)).toEqual(["b", "a", "c"]);
    expect(moved(three.items, "a", -1)).toEqual(["a", "b", "c"]);   // first of its group
    expect(moved(three.items, "b", 1)).toEqual(["a", "b", "c"]);    // last of its group: not across
    expect(moved(three.items, "c", 1)).toEqual(["a", "b", "c"]);
    expect(moved(three.items, "zz", 1)).toEqual(["a", "b", "c"]);
  });

  it("placed: a dropped tile takes its target's place within its group; across groups it stays", () => {
    expect(placed(three.items, "a", "b")).toEqual(["b", "a", "c"]);
    expect(placed(three.items, "b", "a")).toEqual(["b", "a", "c"]);
    expect(placed(three.items, "a", "c")).toEqual(["a", "b", "c"]);
    expect(placed(three.items, "a", "zz")).toEqual(["a", "b", "c"]);
  });

  it("moves, takes off and adds back through one call with the whole order and the whole hidden set", () => {
    const onArrange = vi.fn();
    const withHidden: InsightList = { ...three, hidden: [{ ...base, id: "d", title: "D", chart: null }], customised: true };
    render(<InsightsDashboard list={withHidden} onChanged={vi.fn()} onArrange={onArrange} />);
    expect(screen.queryByTestId("arrange-a")).toBeNull();   // not arranging: no controls on the tiles
    fireEvent.click(screen.getByRole("button", { name: "Customize" }));
    fireEvent.click(screen.getByRole("button", { name: "Move A down" }));
    expect(onArrange).toHaveBeenLastCalledWith(["b", "a", "c"], ["d"]);
    expect((screen.getByRole("button", { name: "Move A up" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Take B off your dashboard" }));
    expect(onArrange).toHaveBeenLastCalledWith(["a", "c"], ["d", "b"]);
    fireEvent.click(screen.getByRole("button", { name: "Add D back" }));
    expect(onArrange).toHaveBeenLastCalledWith(["a", "b", "c", "d"], []);
    fireEvent.click(screen.getByText("Reset to the standard dashboard"));
    expect(onArrange).toHaveBeenLastCalledWith([], []);
  });

  it("says when the person's arrangement could not be read", () => {
    render(<InsightsDashboard list={{ ...three, layout_problem: "Your own arrangement could not be read; the standard one is shown." }} onChanged={vi.fn()} onArrange={vi.fn()} />);
    expect(screen.getByTestId("layout-problem")).toHaveTextContent("could not be read");
  });

  it("renders a single category tab bar with a Browse measures drawer without repeating the Measures prefix, and supports header-controlled Customize", () => {
    const many: InsightList = {
      ...list,
      scope: "everyone",
      items: [
        { ...base, id: "t1", title: "T1", group: "Team", chart: null },
        { ...base, id: "o1", title: "O1", group: "Org", chart: null },
        { ...base, id: "m1", title: "M1", group: "Measures: Finance", chart: null },
        { ...base, id: "m2", title: "M2", group: "Measures: Sales", chart: null },
      ],
    };
    const { rerender } = render(
      <InsightsDashboard list={many} onChanged={vi.fn()} onArrange={vi.fn()} editing={false} onEditingChange={vi.fn()} />,
    );
    expect(screen.queryByRole("button", { name: "Customize" })).toBeNull();
    const nav = screen.getByTestId("insight-groups");
    expect(nav).toHaveTextContent("All");
    expect(nav).toHaveTextContent("Finance");
    expect(nav).toHaveTextContent("Browse measures (2)");
    expect(nav).not.toHaveTextContent("Measures: Finance");
    fireEvent.click(screen.getByTestId("browse-measures"));
    expect(screen.getByTestId("measures-drawer")).toHaveTextContent("M1");
    rerender(
      <InsightsDashboard list={many} onChanged={vi.fn()} onArrange={vi.fn()} editing={true} onEditingChange={vi.fn()} />,
    );
    expect(screen.getByTestId("hidden-charts")).toBeTruthy();
  });
});

describe("a chart pinned from the question box on Insights", () => {
  it("is shown on the dashboard at once (the page's callback runs after the pin is kept)", async () => {
    const pinned = vi.fn();
    const chart = { kind: "bar", title: "Top customers", x: "customer", y: ["total"], labels: ["A"], series: [{ name: "total", values: [1] }], sql: "SELECT customer, total FROM t", spec: { kind: "bar", x: "customer", y: ["total"] } } as never;
    render(<PinnedHere.Provider value={pinned}><AnswerCharts charts={[chart]} question="top customers" /></PinnedHere.Provider>);
    fireEvent.click(screen.getByText("Pin to your charts"));
    await waitFor(() => expect(pinned).toHaveBeenCalledTimes(1));
    expect(saveInsight).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Added to your charts below")).toBeTruthy();
  });
});

describe("the checked line", () => {
  it("shows a reconciliation or a difference, never a line that only says there is nothing to check against", async () => {
    const one = { ...list, items: [list.items[0]] };
    const withChecked = (checked: { status: string; says: string }) => ({ ...result("team_output"), checked }) as InsightResult;
    const { unmount } = render(<InsightsDashboard list={one} onChanged={vi.fn()} feed={{ team_output: { result: withChecked({ status: "not_checked", says: "no second figure to set it against" }) } }} streaming={false} />);
    await waitFor(() => expect(screen.getByRole("img", { name: "Work sent per person" })).toBeTruthy());
    expect(screen.queryByTestId("checked-team_output")).toBeNull();
    unmount();
    render(<InsightsDashboard list={one} onChanged={vi.fn()} feed={{ team_output: { result: withChecked({ status: "reconciles", says: "The months add up." }) } }} streaming={false} />);
    await waitFor(() => expect(screen.getByTestId("checked-team_output")).toHaveTextContent("Checked: The months add up."));
  });
});

describe("proposing a saved tile as a shared definition", () => {
  it("submits the saved tile's SQL to /api/measures/candidates and confirms submission", async () => {
    const savedOnly = { ...list, items: [list.items[1]] };
    render(<InsightsDashboard list={savedOnly} onChanged={vi.fn()} feed={{ s7: { result: result("s7") } }} streaming={false} />);
    await waitFor(() => expect(screen.getByTestId("tile-menu-s7")).toBeTruthy());
    fireEvent.click(screen.getByTestId("tile-menu-s7"));
    await waitFor(() => expect(screen.getByTestId("propose-s7")).toBeTruthy());
    fireEvent.click(screen.getByTestId("propose-s7"));
    await waitFor(() => expect(proposeMeasureCandidate).toHaveBeenCalledWith({
      insight_id: "s7",
      title: "Open orders by month",
      question: undefined,
      sql: "SELECT person, mail_sent FROM x",
      source_kind: "saved_tile",
    }));
    await waitFor(() => expect(screen.getByTestId("proposed-s7")).toHaveTextContent("Proposed for review"));
  });
});

describe("multi-panel chart counts and category breakdown", () => {
  it("counts total items across panels for multi-panel charts and never shows 'of 0' or 'Show all 0'", async () => {
    const cadNames = Array.from({ length: 20 }, (_, i) => `CAD-${i + 1}`);
    const usdNames = Array.from({ length: 23 }, (_, i) => `USD-${i + 1}`);
    const multiChart = {
      kind: "bar" as const,
      title: "Receivables to chase: overdue per customer",
      x: "customer",
      labels: [],
      series: [],
      panels: [
        { title: "CAD", labels: cadNames, series: [{ name: "overdue", values: cadNames.map((_, i) => 1000 - i) }] },
        { title: "USD", labels: usdNames, series: [{ name: "overdue", values: usdNames.map((_, i) => 2000 - i) }] },
      ],
      spec: { kind: "bar" as const, x: "customer", y: ["overdue"], facet: "currency", horizontal: true },
      as_of: "2026-11-25",
      sql: "SELECT customer, currency, overdue FROM ar",
    };
    expect(chartPointCount(multiChart)).toBe(43);
    const tile = { ...base, id: "ar_chase", title: multiChart.title, group: "Finance", chart: null };
    const tileResult: InsightResult = {
      ...result("team_output"),
      ...tile,
      row_count: 43,
      drawn: multiChart,
    };
    render(<InsightsDashboard list={{ ...list, items: [tile] }} onChanged={vi.fn()} feed={{ ar_chase: { result: tileResult } }} streaming={false} />);
    await waitFor(() => expect(screen.getByText("Show all 43")).toBeTruthy());
    const card = screen.getByTestId("insight-ar_chase");
    expect(card).toHaveTextContent(`Top ${TOP} per currency`);
    expect(card).not.toHaveTextContent("of 0");
    expect(card).not.toHaveTextContent("Show all 0");
  });

  it("clarifies charts vs measures breakdown when a category tab combines both", () => {
    const combined: InsightList = {
      ...list,
      scope: "everyone",
      items: [
        { ...base, id: "f1", title: "F1", group: "Finance", chart: null },
        { ...base, id: "f2", title: "F2", group: "Finance", chart: null },
        { ...base, id: "fm1", title: "FM1", group: "Measures: Finance", chart: null },
        { ...base, id: "fm2", title: "FM2", group: "Measures: Finance", chart: null },
        { ...base, id: "fm3", title: "FM3", group: "Measures: Finance", chart: null },
        { ...base, id: "s1", title: "S1", group: "Sales", chart: null },
      ],
    };
    render(<InsightsDashboard list={combined} onChanged={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: /Finance/ }));
    const breakdown = screen.getByTestId("category-breakdown");
    expect(breakdown).toHaveTextContent("2 charts");
    expect(breakdown).toHaveTextContent("3 measures");
    expect(breakdown).toHaveTextContent("Jump to measures (3)");
  });
});

describe("Bento grid resize, KPI cards, SQL inspector, and Benchmarks tab", () => {
  it("resizes tiles via S/M/L/Full controls in Customize mode and persists in localStorage", async () => {
    const one: InsightList = { ...list, items: [list.items[0]] };
    render(<InsightsDashboard list={one} onChanged={vi.fn()} feed={{ team_output: { result: result("team_output") } }} streaming={false} />);
    const tile = screen.getByTestId("insight-team_output");
    expect(tile.getAttribute("data-bento-size")).toBe("m");

    fireEvent.click(screen.getByRole("button", { name: "Customize" }));
    const resizeGroup = screen.getByTestId("tile-resize-team_output");
    fireEvent.click(resizeGroup.querySelectorAll("button")[3]); // Full (12-col)
    expect(tile.getAttribute("data-bento-size")).toBe("full");
    expect(window.localStorage.getItem("plx.insights.bento.v1")).toContain('"team_output":"full"');

    fireEvent.click(screen.getByTestId("reset-bento-layout"));
    expect(tile.getAttribute("data-bento-size")).toBe("m");
  });

  it("renders Executive KPI summary for KPI tiles and opens SQL inspector from the header SQL button", async () => {
    const kpiTile = {
      ...base,
      id: "kpi_rev",
      title: "Monthly Revenue",
      measure: { id: "revenue", name: "Revenue invoiced", definition: "Net revenue.", owner: "Finance", version: "v1", defined_on: "2026-11-23" },
      chart: null,
    };
    const kpiResult: InsightResult = {
      ...result("team_output"),
      ...kpiTile,
      sql: "SELECT month, revenue FROM monthly_rev",
      drawn: {
        kind: "kpi",
        title: "Monthly Revenue",
        x: "month",
        labels: ["Oct", "Nov"],
        series: [{ name: "revenue", values: [100000, 120000] }],
        currency: "USD",
        as_of: "2026-11-25",
        sql: "SELECT month, revenue FROM monthly_rev",
      },
    };
    render(<InsightsDashboard list={{ ...list, items: [kpiTile] }} onChanged={vi.fn()} feed={{ kpi_rev: { result: kpiResult } }} streaming={false} />);
    await waitFor(() => expect(screen.getByTestId("kpi-bento-summary")).toBeTruthy());
    expect(screen.getByTestId("insight-kpi_rev").getAttribute("data-bento-size")).toBe("s");
    expect(screen.getByTestId("verified-pill-kpi_rev")).toHaveTextContent("Finance");
    expect(screen.getByTestId("verified-pill-kpi_rev")).toHaveTextContent("v1");
    expect(screen.getByTestId("resize-handle-kpi_rev")).toBeTruthy();

    fireEvent.click(screen.getByTestId("sql-trigger-kpi_rev"));
    expect(screen.getByText("SELECT month, revenue FROM monthly_rev")).toBeTruthy();
  });

  it("resizes tiles via the 1-click Layout popover and keeps corner drag free of conflicting click jumps", async () => {
    const one: InsightList = { ...list, items: [list.items[0]] };
    render(<InsightsDashboard list={one} onChanged={vi.fn()} feed={{ team_output: { result: result("team_output") } }} streaming={false} />);
    const tile = screen.getByTestId("insight-team_output");
    expect(tile.getAttribute("data-bento-size")).toBe("m");

    // Clean overflow menu resizes width & height cleanly
    fireEvent.click(screen.getByTestId("tile-menu-team_output"));
    const menu = screen.getByTestId("tile-menu-popover-team_output");
    const wideBtn = Array.from(menu.querySelectorAll("button")).find((b) => b.textContent?.trim() === "L");
    expect(wideBtn).toBeTruthy();
    fireEvent.click(wideBtn!);
    expect(tile.getAttribute("data-bento-size")).toBe("l");

    // Clicking the corner resize handle without dragging must NOT unexpectedly snap/cycle to another size
    const corner = screen.getByTestId("resize-handle-team_output");
    fireEvent.click(corner);
    expect(tile.getAttribute("data-bento-size")).toBe("l");

    // Dragging the corner handle smoothly previews and snaps on pointer release
    fireEvent.pointerDown(corner, { button: 0, clientX: 400, clientY: 200 });
    expect(screen.getByTestId("resize-preview-team_output")).toBeTruthy();
    fireEvent.pointerMove(window, { clientX: 900, clientY: 280 });
    fireEvent.pointerUp(window);
    expect(screen.queryByTestId("resize-preview-team_output")).toBeNull();
    expect(tile.getAttribute("data-bento-size")).toBe("full");
    expect(tile.getAttribute("data-bento-height")).toBe("tall");
  });

  it("switches smoothly between the Liquid Glass tabs (My canvas, All views, Ontology, Monitors) with zero AI brochure cards", async () => {
    const measures = [{
      id: "revenue",
      name: "Revenue invoiced",
      definition: "Net line revenue on posted sales invoices.",
      owner: "Finance",
      unit: "money",
      source: "sales_invoice_lines",
      dimensions: ["customer", "month"],
      period: "last 30 days by invoice_date",
      ask: "Why did revenue change last month?",
    }];
    render(<InsightsDashboard list={{ ...list, measures }} onChanged={vi.fn()} />);

    expect(screen.getByTestId("tab-all-views")).toBeTruthy();
    expect(screen.getByTestId("new-chart-link").getAttribute("href")).toBe("/ask");

    fireEvent.click(screen.getByTestId("tab-ontology"));
    expect(screen.getByTestId("ontology-tab-panel")).toBeTruthy();
    expect(screen.getByTestId("dot-grid-canvas")).toBeTruthy();
    expect(screen.getByTestId("ontology-inspector")).toHaveTextContent("Revenue invoiced");

    fireEvent.click(screen.getByTestId("subtab-measures-catalog"));
    expect(screen.getByTestId("measures-catalog-table")).toHaveTextContent("Revenue invoiced");

    fireEvent.click(screen.getByTestId("tab-monitors"));
    expect(screen.getByTestId("monitors-tab-panel")).toHaveTextContent("Scheduled alerts");
    await waitFor(() => expect(screen.getByTestId("benchmarks-panel")).toBeTruthy());
    expect(screen.queryByTestId("genie-15-capabilities")).toBeNull();
    fireEvent.click(screen.getByTestId("subtab-accuracy"));
    expect(screen.getByText("Why did revenue change last month?")).toBeTruthy();

    fireEvent.click(screen.getByTestId("tab-dashboard"));
    expect(screen.getAllByTestId("bento-grid").length).toBeGreaterThan(0);
  });

  it("starts with a curated 6-tile executive canvas for large libraries and toggles widgets via the library drawer without a Show all dump", () => {
    const bigItems = Array.from({ length: 12 }, (_, idx) => ({
      ...base,
      id: `tile_${idx + 1}`,
      title: `Chart ${idx + 1}`,
      group: ["Finance", "Sales", "Purchasing", "Operations", "Quality", "Customers"][idx % 6],
      chart: { kind: "bar" as const, x: "name", y: ["val"] },
    }));
    render(<InsightsDashboard list={{ ...list, items: bigItems }} onChanged={vi.fn()} />);
    expect(screen.getByTestId("curated-canvas")).toBeTruthy();
    expect(screen.getAllByRole("region").length).toBeGreaterThan(0);
    expect(document.querySelectorAll('section[id^="insight-"]').length).toBe(6);

    fireEvent.click(screen.getByTestId("open-widget-library"));
    const drawer = screen.getByTestId("widget-library-drawer");
    expect(drawer).toBeTruthy();
    expect(drawer).not.toHaveTextContent("Show all (12)");
    fireEvent.click(screen.getByTestId("library-toggle-tile_7"));
    expect(document.querySelectorAll('section[id^="insight-"]').length).toBe(7);
  });
});
