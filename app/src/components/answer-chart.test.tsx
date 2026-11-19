import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AnswerCharts, withoutChartBlocks, type AskChart } from "./answer-chart";
import { chartNotes } from "./charts/chart-view";
import { PALETTE, conversion, extent, ramp, short, ticks, waterfall } from "./charts/scale";
import { wrap } from "./charts/export";

const bars: AskChart = {
  kind: "bar",
  title: "Invoiced by currency",
  x: "currency",
  labels: ["CAD", "USD"],
  series: [{ name: "total", values: [703058.44, 701688.25] }],
  as_of: "2026-11-17T22:00:00+00:00",
};
const people = { x: "person", labels: ["Ann", "Bob", "Cy"] };
const marks = (svg: Element, tag: string) => Array.from(svg.querySelectorAll(tag)).filter((n) => !n.hasAttribute("data-hover"));

function drawn(chart: AskChart) {
  render(<AnswerCharts charts={[chart]} />);
  return screen.getByRole("img", { name: chart.title });
}

describe("answer charts", () => {
  it("draws one bar per label from the result's own values, titled, with its as-of", () => {
    const svg = drawn(bars);
    expect(screen.getByText("Invoiced by currency")).toBeTruthy();
    expect(marks(svg, "rect")).toHaveLength(2);
    expect(svg.textContent).toContain("CAD");
    expect(screen.getByText(/As of/)).toBeTruthy();
  });

  it("hover shows the exact values at that label", () => {
    const svg = drawn({ ...bars, currency: "CAD", series: [...bars.series, { name: "n_orders", values: [120, 98] }] });
    fireEvent.mouseMove(svg.querySelectorAll("[data-hover]")[1]);
    const tip = screen.getByRole("tooltip");
    expect(tip.textContent).toContain("USD");
    expect(tip.textContent).toContain("701,688.25 CAD");
    expect(tip.textContent).toContain("N orders");
  });

  it("stacks, groups and lays bars sideways", () => {
    const two = { ...people, title: "Stacked", kind: "bar" as const, series: [{ name: "a", values: [1, 2, 3] }, { name: "b", values: [4, null, 6] }] };
    const svg = drawn({ ...two, spec: { kind: "bar", x: "person", y: ["a", "b"], stacked: true, horizontal: true } });
    expect(marks(svg, "rect")).toHaveLength(5);   // a null is no bar
    expect(screen.getByText("A")).toBeTruthy();   // a legend for two series
  });

  it("draws a line, an area, a combo's right axis and a waterfall's total", () => {
    const line = drawn({ ...people, title: "L", kind: "line", series: [{ name: "v", values: [1, null, 3] }] });
    expect(line.querySelector("polyline")?.getAttribute("points")?.split(" ")).toHaveLength(2);
    const combo = drawn({ ...people, title: "C", kind: "combo", series: [{ name: "mail", values: [5, 6, 7] }, { name: "share", values: [0.1, 0.5, 0.9], axis: "right" }] });
    expect(marks(combo, "rect")).toHaveLength(3);
    expect(combo.querySelectorAll("polyline")).toHaveLength(1);
    expect(screen.getByText(/share \(right axis\)/i)).toBeTruthy();
    const fall = drawn({ ...people, title: "W", kind: "waterfall", series: [{ name: "change", values: [10, -4, 3] }] });
    expect(marks(fall, "rect")).toHaveLength(4);
    expect(fall.textContent).toContain("Total");
  });

  it("draws pie and donut shares of every slice, a scatter with its trend and r", () => {
    const pie = drawn({ ...people, title: "Share", kind: "donut", series: [{ name: "v", values: [2, 1, 1] }] });
    expect(pie.querySelectorAll("path")).toHaveLength(3);
    expect(pie.textContent).toContain("50%");
    expect(pie.textContent).toContain("total");
    const sc = drawn({ ...people, title: "S", kind: "scatter", xs: [1, 2, 3], series: [{ name: "y", values: [2, 4, 6] }], trend: { slope: 2, intercept: 0, r: 1, n: 3 } });
    expect(sc.querySelectorAll("circle")).toHaveLength(3);
    expect(sc.querySelectorAll("line[stroke-dasharray]")).toHaveLength(1);
    expect(screen.getByText(/r = 1.00 over 3 points/)).toBeTruthy();
    expect(screen.getByText(/does not show one causes the other/)).toBeTruthy();
  });

  it("colours a heatmap's cells and a table's values; KPI tiles show the last row and its change", () => {
    const heat = drawn({ x: "week", labels: ["w1", "w2"], title: "H", kind: "heatmap", series: [{ name: "Ops", values: [1, 9] }, { name: "QA", values: [5, null] }] });
    const fills = marks(heat, "rect").map((r) => r.getAttribute("fill"));
    expect(new Set(fills).size).toBeGreaterThan(2);
    render(<AnswerCharts charts={[{ ...people, title: "T", kind: "table", series: [{ name: "v", values: [1, 5, 9] }] }]} />);
    const cell = Array.from(screen.getByRole("table").querySelectorAll("td")).find((td) => td.textContent === "9");
    expect(cell?.getAttribute("style")).toContain("background");
    render(<AnswerCharts charts={[{ x: "week", labels: ["w1", "w2", "w3"], title: "K", kind: "kpi", series: [{ name: "orders", values: [10, 12, 15] }] }]} />);
    const tile = screen.getByTestId("kpi-tile");
    expect(tile.textContent).toContain("15");
    expect(tile.textContent).toContain("▲ 3 (+25%), w3 vs w2");
  });

  it("a funnel says what its percentages are of", () => {
    expect(chartNotes({ ...people, title: "F", kind: "funnel", series: [{ name: "n", values: [10, 5, 2] }] })).toContain("Each percentage is of the step before.");
  });

  it("a chart with one series says what its axes are", () => {
    render(<AnswerCharts charts={[{ ...bars, series: [{ name: "invoiced_cad", values: [1, 2] }] }]} />);
    expect(screen.getByTestId("axis-caption").textContent).toBe("Invoiced cad by currency");
    cleanup();
    render(<AnswerCharts charts={[{ ...people, title: "S", kind: "scatter", xs: [1, 2, 3], series: [{ name: "invoiced_cad", values: [5, 6, 7] }] }]} />);
    expect(screen.getByTestId("axis-caption").textContent).toBe("Up: Invoiced cad. Across: person.");
  });

  it("a PNG's notes wrap at the drawing's width and keep every word", () => {
    const text = "Drawn from 150 rows of a query read as you. Fitted line: r = 0.47 over 150 points.";
    const lines = wrap(text, 120, (s) => s.length * 6);
    expect(lines.every((l) => l.length * 6 <= 120 || !l.includes(" "))).toBe(true);
    expect(lines.join(" ")).toBe(text);
  });

  it("a KPI compares by place on the axis and names a period not over yet", () => {
    render(<AnswerCharts charts={[{ x: "month", labels: ["2026-09", "2026-10", "2026-11"], title: "K", kind: "kpi", as_of: "2026-11-18T22:00:00+00:00", open_period: "2026-11", series: [{ name: "orders", values: [197, null, 197] }] }]} />);
    expect(screen.getByTestId("kpi-tile").textContent).toContain("▲ 0 (+0%), 2026-11 so far vs 2026-09");
    expect(screen.getByText("2026-11 is not over yet: its figures are of part of it.")).toBeTruthy();
  });

  it("draws small multiples on one scale and says what it leaves out", () => {
    const chart: AskChart = { ...people, title: "M", kind: "line", labels: [], series: [], rows: 140, shown: 60, omitted: 2, partial: true,
      panels: [{ title: "Ops", labels: ["a", "b"], series: [{ name: "v", values: [1, 2] }] }, { title: "QA", labels: ["a", "b"], series: [{ name: "v", values: [3, 40] }] }] };
    render(<AnswerCharts charts={[chart]} />);
    expect(screen.getAllByRole("img", { name: "M" })).toHaveLength(2);
    expect(screen.getByText("QA")).toBeTruthy();
    const notes = chartNotes(chart).join(" ");
    expect(notes).toContain("Shows 60 of 140 rows");
    expect(notes).toContain("only its first rows");
    expect(notes).toContain("2 smaller series are not drawn");
  });

  it("offers the files and PNG under a chart", () => {
    render(<AnswerCharts charts={[bars]} />);
    for (const name of ["PNG", "Excel", "PowerPoint", "Word", "PDF"]) expect(screen.getByRole("button", { name })).toBeTruthy();
  });

  it("draws nothing without charts", () => {
    const { container } = render(<AnswerCharts charts={[]} />);
    expect(container.innerHTML).toBe("");
  });

  it("hides a chart block the model is still streaming", () => {
    expect(withoutChartBlocks('Top 3.\n\n```chart\n{"type": "bar", "res')).toBe("Top 3.");
    expect(withoutChartBlocks('A.\n```chart\n{}\n```\nB.')).toBe("A.\n\nB.");
  });
});

describe("chart scales", () => {
  it("ticks always cover the largest value, from a round number", () => {
    for (const [lo, hi] of [[-7, 14], [0, 9.5], [0, 703058.44], [0, 1], [-3, -1]]) {
      const t = ticks(lo, hi);
      expect(t[0]).toBeLessThanOrEqual(lo);
      expect(t[t.length - 1]).toBeGreaterThanOrEqual(hi);
    }
    expect(short(1_250_000)).toBe("1.3M");
    expect(short(34_000)).toBe("34k");
  });
  it("stacked extents, waterfall totals and funnel conversions add up", () => {
    expect(extent([{ values: [1, -2] }, { values: [3, -4] }], true)).toEqual([-6, 4]);
    const w = waterfall([10, -4, 3]);
    expect(w[w.length - 1]).toEqual({ from: 0, to: 9, total: true });
    expect(conversion([100, 40, 10]).map((c) => c.ofPrev)).toEqual([null, 0.4, 0.25]);
  });
  it("one palette for light and dark, the ramp running low to high", () => {
    expect(PALETTE.light).toHaveLength(PALETTE.dark.length);
    expect(ramp(0, 0, 10, "light")).not.toBe(ramp(10, 0, 10, "light"));
  });
});
