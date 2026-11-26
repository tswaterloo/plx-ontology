import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ChartView, type AskChart } from "./chart-view";
import { INK, PALETTE, THRESHOLD } from "./scale";

const marks = (svg: Element, tag: string) =>
  Array.from(svg.querySelectorAll(tag)).filter((n) => !n.hasAttribute("data-hover"));

describe("chart palette and SVG styling", () => {
  it("exposes the zero-purple Genie-calibrated palette and Pure Matte Graphite ink tokens", () => {
    expect(PALETTE.dark).toEqual([
      "#5c9ce6", "#5da56f", "#dca138", "#d95b48",
      "#4ca6a8", "#d98245", "#7ea6d6", "#8fb35a",
    ]);
    expect(PALETTE.light).toEqual([
      "#2b6cb0", "#2f855a", "#b7791f", "#c53030",
      "#2c7a7b", "#c05621", "#3182ce", "#689f38",
    ]);
    const bannedPurple = ["#4f46e5", "#8994fa", "#a5b4fc", "#c084fc", "#7c3aed", "#a78bfa", "#6d28d9"];
    for (const hex of [...PALETTE.dark, ...PALETTE.light]) {
      expect(bannedPurple).not.toContain(hex.toLowerCase());
    }
    expect(INK.dark).toEqual({
      text: "#f3f3f3", muted: "#c8c8c8", grid: "#2a2a2a", axis: "#3d3d3d", ground: "#1e1e1e",
    });
    expect(INK.light).toEqual({
      text: "#111318", muted: "#3f4652", grid: "#e4e7ec", axis: "#cfd5de", ground: "#ffffff",
    });
  });

  it("renders subtle vertical gradient fills and crisp 2.5px strokes under line series", () => {
    const chart: AskChart = {
      kind: "line",
      title: "Monthly revenue",
      x: "month",
      labels: ["Jan", "Feb", "Mar"],
      series: [{ name: "revenue", values: [100, 140, 180] }],
    };
    render(<ChartView chart={chart} />);
    const svg = screen.getByRole("img", { name: "Monthly revenue" });
    const grad = svg.querySelector("defs linearGradient");
    expect(grad).toBeTruthy();
    const stops = Array.from(grad?.querySelectorAll("stop") ?? []);
    expect(stops).toHaveLength(2);
    expect(stops[0].getAttribute("stop-opacity")).toBe("0.2");
    expect(stops[1].getAttribute("stop-opacity")).toBe("0");
    const areaPath = svg.querySelector("path");
    expect(areaPath?.getAttribute("fill")).toBe(`url(#${grad?.getAttribute("id")})`);
    const line = svg.querySelector("polyline");
    expect(line?.getAttribute("stroke-width")).toBe("2.5");
  });

  it("renders rounded bar corners (rx=4), dashed horizontal gridlines, and inside value labels on wide horizontal bars", () => {
    const vertical: AskChart = {
      kind: "bar",
      title: "Orders by site",
      x: "site",
      labels: ["CAN", "USA"],
      series: [{ name: "orders", values: [42, 65] }],
    };
    render(<ChartView chart={vertical} />);
    const vSvg = screen.getByRole("img", { name: "Orders by site" });
    const vBars = marks(vSvg, "rect");
    expect(vBars).toHaveLength(2);
    for (const bar of vBars) {
      expect(bar.getAttribute("rx")).toBe("4");
      expect(bar.getAttribute("class")).toContain("transition-opacity");
    }
    const dashed = Array.from(vSvg.querySelectorAll('line[stroke-dasharray="3 3"]'));
    expect(dashed.length).toBeGreaterThanOrEqual(2);
    for (const grid of dashed) {
      expect(grid.getAttribute("opacity")).toBe("0.7");
      expect(grid.getAttribute("y1")).toBe(grid.getAttribute("y2"));
    }

    const horizontal: AskChart = {
      kind: "bar",
      title: "Orders horizontal",
      x: "site",
      labels: ["CAN", "USA"],
      series: [{ name: "orders", values: [42, 65] }],
      spec: { kind: "bar", x: "site", y: ["orders"], horizontal: true },
    };
    render(<ChartView chart={horizontal} />);
    const hSvg = screen.getByRole("img", { name: "Orders horizontal" });
    const hBars = marks(hSvg, "rect");
    expect(hBars).toHaveLength(2);
    for (const bar of hBars) {
      expect(bar.getAttribute("rx")).toBe("4");
      expect(bar.getAttribute("ry")).toBe("4");
    }
    expect(hSvg.querySelectorAll("line[stroke-dasharray]")).toHaveLength(0);
    // Wide horizontal bars display crisp value labels inside the bar
    expect(hSvg.textContent).toContain("42");
    expect(hSvg.textContent).toContain("65");
  });

  it("renders semantic threshold coloring and dashed reference badge for percentage score bars", () => {
    const chart: AskChart = {
      kind: "bar",
      title: "On-time rate by supplier",
      x: "supplier",
      labels: ["Acme", "Beacon", "Crest"],
      series: [{ name: "on_time_pct", values: [92, 74, 48] }],
      spec: { kind: "bar", x: "supplier", y: ["on_time_pct"], horizontal: true },
    };
    render(<ChartView chart={chart} />);
    const svg = screen.getByRole("img", { name: "On-time rate by supplier" });
    const refGroup = svg.querySelector("[data-reference-line]");
    expect(refGroup).toBeTruthy();
    expect(refGroup?.textContent).toContain("80%");
    const bars = Array.from(svg.querySelectorAll("rect.chart-bar-h"));
    expect(bars.map((b) => b.getAttribute("fill"))).toEqual([
      THRESHOLD.light.ok,
      THRESHOLD.light.risk,
      THRESHOLD.light.late,
    ]);
  });

  it("renders rich scatter dots (r=6.5) with quadrant mean crosshairs and diverging heatmap vertical scale", () => {
    const scatter: AskChart = {
      kind: "scatter",
      title: "Lead time vs volume",
      x: "volume",
      labels: ["A", "B", "C"],
      xs: [10, 20, 30],
      series: [{ name: "days", values: [4, 8, 12] }],
    };
    render(<ChartView chart={scatter} />);
    const sSvg = screen.getByRole("img", { name: "Lead time vs volume" });
    const dots = Array.from(sSvg.querySelectorAll("circle"));
    expect(dots).toHaveLength(3);
    expect(dots[0].getAttribute("r")).toBe("6.5");
    expect(dots[0].getAttribute("fill-opacity")).toBe("0.68");
    const crosshairs = sSvg.querySelector("[data-quadrant-crosshair]");
    expect(crosshairs).toBeTruthy();
    expect(crosshairs?.textContent).toContain("Avg X: 20");
    expect(crosshairs?.textContent).toContain("Avg Y: 8");

    const heatmap: AskChart = {
      kind: "heatmap",
      title: "Defects by shift",
      x: "week",
      labels: ["W1", "W2"],
      series: [{ name: "Day", values: [2, 10] }, { name: "Night", values: [6, 14] }],
    };
    render(<ChartView chart={heatmap} />);
    const hSvg = screen.getByRole("img", { name: "Defects by shift" });
    const scale = hSvg.querySelector("[data-heatmap-scale]");
    expect(scale).toBeTruthy();
    expect(scale?.textContent).toContain("14");
    expect(scale?.textContent).toContain("2");
  });

  it("shows the elevated frosted tooltip card on hover", () => {
    const chart: AskChart = {
      kind: "bar",
      title: "Tooltip check",
      x: "site",
      labels: ["CAN", "USA"],
      series: [{ name: "orders", values: [12, 34] }],
    };
    render(<ChartView chart={chart} />);
    const svg = screen.getByRole("img", { name: "Tooltip check" });
    fireEvent.mouseMove(svg.querySelectorAll("[data-hover]")[0]);
    const tip = screen.getByRole("tooltip");
    expect(tip.className).toContain("bg-raised");
    expect(tip.className).toContain("border-border-strong");
    expect(tip.className).toContain("shadow-lg");
    expect(tip.className).toContain("rounded-lg");
    expect(tip.className).toContain("px-3");
    expect(tip.className).toContain("py-2");
    expect(tip.className).toContain("tabular-nums");
  });
});
