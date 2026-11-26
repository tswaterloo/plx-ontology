import { beforeEach, describe, expect, it } from "vitest";
import type { InsightInfo, InsightResult } from "@/lib/types";
import {
  BENTO_STORAGE_KEY,
  MAX_STORED_CANVAS_TILES,
  applyBentoOrder,
  clearBentoState,
  defaultTileSize,
  extractKpiSummary,
  formatResizeBadge,
  loadBentoState,
  moveIdStep,
  nextBentoSize,
  pickDefaultCanvasIds,
  reorderIds,
  saveBentoState,
  snapColsToBentoSize,
  snapHeightDeltaToBentoHeight,
} from "./bento-layout";

const baseInfo: InsightInfo = {
  id: "t1",
  title: "Tile 1",
  about: "About tile 1",
  group: "Finance",
  saved: false,
  question: null,
  schedule: "",
  notify: "never",
  chart: null,
};

const baseResult = (overrides: Partial<InsightResult> = {}): InsightResult => ({
  ...baseInfo,
  sql: "SELECT 1",
  columns: ["name", "value"],
  rows: [["A", "10"]],
  people: [],
  row_count: 1,
  truncated: false,
  as_of: null,
  read_at: "2026-11-25T10:00:00Z",
  drawn: null,
  ...overrides,
});

beforeEach(() => {
  window.localStorage.clear();
});

describe("bento-layout smart sizing", () => {
  it("sizes KPI charts and single-row summaries as compact 4-col (s)", () => {
    const kpiChart = {
      kind: "kpi" as const,
      title: "Monthly revenue",
      x: "month",
      labels: ["Oct", "Nov"],
      series: [{ name: "revenue", values: [100000, 125000] }],
      currency: "USD",
    };
    expect(defaultTileSize(baseInfo, baseResult({ drawn: kpiChart }))).toBe("s");
    expect(defaultTileSize(baseInfo, baseResult({ drawn: null, rows: [["120"]], columns: ["open_pos"], row_count: 1 }))).toBe("s");
  });

  it("sizes multi-panel charts and wide tables as full 12-col", () => {
    const multi = {
      kind: "bar" as const,
      title: "By currency",
      x: "customer",
      labels: [],
      series: [],
      panels: [
        { title: "CAD", labels: ["A"], series: [{ name: "v", values: [1] }] },
        { title: "USD", labels: ["B"], series: [{ name: "v", values: [2] }] },
      ],
    };
    expect(defaultTileSize(baseInfo, baseResult({ drawn: multi }))).toBe("full");
    expect(
      defaultTileSize(
        baseInfo,
        baseResult({
          drawn: null,
          columns: ["c1", "c2", "c3", "c4", "c5", "c6"],
          rows: [["1", "2", "3", "4", "5", "6"], ["7", "8", "9", "10", "11", "12"]],
          row_count: 2,
        }),
      ),
    ).toBe("full");
  });

  it("sizes time-series lines as balanced 6-col (or full when dense) and standard bars as medium 6-col", () => {
    const line6 = {
      kind: "line" as const,
      title: "Trend",
      x: "month",
      labels: ["Jan", "Feb", "Mar", "Apr", "May", "Jun"],
      series: [{ name: "rev", values: [1, 2, 3, 4, 5, 6] }],
    };
    expect(defaultTileSize(baseInfo, baseResult({ drawn: line6 }))).toBe("m");
    const line16 = {
      ...line6,
      labels: Array.from({ length: 16 }, (_, i) => `M${i + 1}`),
      series: [{ name: "rev", values: Array.from({ length: 16 }, (_, i) => i + 1) }],
    };
    expect(defaultTileSize(baseInfo, baseResult({ drawn: line16 }))).toBe("full");
    const bar = {
      kind: "bar" as const,
      title: "Suppliers",
      x: "supplier",
      labels: ["S1", "S2", "S3"],
      series: [{ name: "orders", values: [10, 8, 5] }],
    };
    expect(defaultTileSize(baseInfo, baseResult({ drawn: bar }))).toBe("m");
  });

  it("cycles sizes cleanly for one-click hover resize", () => {
    expect(nextBentoSize("s")).toBe("m");
    expect(nextBentoSize("m")).toBe("l");
    expect(nextBentoSize("l")).toBe("full");
    expect(nextBentoSize("full")).toBe("s");
  });
});

describe("extractKpiSummary", () => {
  it("extracts headline value, delta percent, and sparkline for KPI tiles", () => {
    const kpiChart = {
      kind: "kpi" as const,
      title: "Revenue",
      x: "month",
      labels: ["Sep", "Oct", "Nov"],
      series: [{ name: "revenue", values: [100, 200, 250] }],
      currency: "USD",
    };
    const summary = extractKpiSummary(baseInfo, baseResult({ drawn: kpiChart }));
    expect(summary).not.toBeNull();
    expect(summary?.kind).toBe("kpi");
    expect(summary?.rawValue).toBe(250);
    expect(summary?.deltaPct).toBeCloseTo(25, 2);
    expect(summary?.positive).toBe(true);
    expect(summary?.sparkline).toEqual([100, 200, 250]);
  });

  it("extracts single-row numeric summaries with paired currency column", () => {
    const summary = extractKpiSummary(
      baseInfo,
      baseResult({
        drawn: null,
        columns: ["amount_due", "amount_due_currency"],
        rows: [["45200.50", "CAD"]],
        row_count: 1,
      }),
    );
    expect(summary).not.toBeNull();
    expect(summary?.headline).toBe("45,200.5 CAD");
    expect(summary?.currency).toBe("CAD");
  });
});

describe("order and persistence helpers", () => {
  it("reorders and moves ids without mutating input", () => {
    const ids = ["a", "b", "c", "d"];
    expect(reorderIds(ids, "a", "c")).toEqual(["b", "c", "a", "d"]);
    expect(moveIdStep(ids, "b", -1)).toEqual(["b", "a", "c", "d"]);
    expect(moveIdStep(ids, "a", -1)).toEqual(["a", "b", "c", "d"]);
    expect(ids).toEqual(["a", "b", "c", "d"]);
  });

  it("applies custom order while keeping unseen items at the end", () => {
    const items = [{ id: "a" }, { id: "b" }, { id: "c" }, { id: "d" }];
    expect(applyBentoOrder(items, ["c", "a"]).map((x) => x.id)).toEqual(["c", "a", "b", "d"]);
  });

  it("persists and clears Bento state in localStorage under plx.insights.bento.v1", () => {
    saveBentoState({ sizes: { t1: "full", t2: "s" }, order: ["t2", "t1"], hidden: ["t3"] });
    expect(window.localStorage.getItem(BENTO_STORAGE_KEY)).toBeTruthy();
    expect(loadBentoState()).toEqual({
      sizes: { t1: "full", t2: "s" },
      order: ["t2", "t1"],
      hidden: ["t3"],
    });
    clearBentoState();
    expect(loadBentoState()).toEqual({ sizes: {}, order: [], hidden: [] });
  });

  it("curates a balanced 6-tile default canvas for large libraries and snaps direct drag resizes cleanly", () => {
    const many = Array.from({ length: 14 }, (_, i) => ({
      ...baseInfo,
      id: `m${i + 1}`,
      group: ["Finance", "Sales", "Purchasing", "Operations", "Quality", "Customers"][i % 6],
      saved: i === 13,
    }));
    const curated = pickDefaultCanvasIds(many, 6);
    expect(curated).toHaveLength(6);
    expect(curated[0]).toBe("m14"); // pinned tile always kept first
    expect(snapColsToBentoSize(4.2)).toBe("s");
    expect(snapColsToBentoSize(6.5)).toBe("m");
    expect(snapColsToBentoSize(9)).toBe("l");
    expect(snapColsToBentoSize(11.4)).toBe("full");
    expect(snapHeightDeltaToBentoHeight("standard", 80)).toBe("tall");
    expect(snapHeightDeltaToBentoHeight("standard", -80)).toBe("compact");
    expect(formatResizeBadge("l", "tall")).toBe("Wide · Tall");
    expect(formatResizeBadge("m", "standard")).toBe("Half · Medium");
  });

  it("auto-trims bloated legacy canvasIds (> 16 tiles) back to the curated 6 default while preserving deliberate selections", () => {
    const bloated = Array.from({ length: 42 }, (_, i) => `tile_${i + 1}`);
    saveBentoState({ sizes: {}, order: [], hidden: [], canvasIds: bloated });
    expect(loadBentoState().canvasIds).toBeUndefined();

    const deliberate = Array.from({ length: MAX_STORED_CANVAS_TILES }, (_, i) => `tile_${i + 1}`);
    saveBentoState({ sizes: {}, order: [], hidden: [], canvasIds: [...deliberate, "tile_1"] });
    expect(loadBentoState().canvasIds).toEqual(deliberate);
  });
});
