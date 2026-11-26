import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { InsightInfo, InsightList } from "@/lib/types";

const getMeasureCandidates = vi.fn();
const getInsight = vi.fn();
vi.mock("@/lib/api", () => ({
  getMeasureCandidates: (...a: unknown[]) => getMeasureCandidates(...a),
  approveMeasureCandidate: vi.fn(),
  dismissMeasureCandidate: vi.fn(),
  getInsight: (...a: unknown[]) => getInsight(...a),
}));

import { OntologyAndMeasuresPanel, buildNormalizedMeasures, buildOntologyGraph, recordsHrefFor } from "./ontology-map";

const sampleMeasures: NonNullable<InsightList["measures"]> = [
  {
    id: "revenue_invoiced",
    name: "Revenue invoiced",
    definition: "Net line revenue on posted sales invoices.",
    owner: "Finance",
    unit: "money",
    source: "sales_invoices",
    dimensions: ["customer", "month"],
    period: "last 12 months by invoice_date",
    ask: "Show monthly revenue invoiced by currency",
  },
  {
    id: "late_pos",
    name: "Late purchase orders",
    definition: "Open supplier purchase orders past promised receipt date.",
    owner: "Purchasing",
    unit: "count",
    source: "purchase_orders",
    dimensions: ["supplier", "buyer"],
    period: "open orders as of today",
    ask: "Which suppliers have the most late purchase orders?",
  },
];

const sampleItems: InsightInfo[] = [
  {
    id: "measure_revenue_invoiced",
    title: "Revenue invoiced",
    about: "Net line revenue on posted sales invoices.",
    group: "Measures: Finance",
    saved: false,
    question: null,
    schedule: "",
    notify: "never",
    chart: { kind: "line", x: "month", y: ["revenue"] },
    measure: {
      id: "revenue_invoiced",
      name: "Revenue invoiced",
      definition: "Net line revenue on posted sales invoices.",
      owner: "Finance",
      version: "v2",
      defined_on: "2026-11-23",
    },
  },
];

beforeEach(() => {
  getMeasureCandidates.mockReset().mockResolvedValue({
    can_review: true,
    total: 0,
    counts: undefined,
    candidates: [],
  });
  getInsight.mockReset().mockResolvedValue({
    ...sampleItems[0],
    sql: "SELECT month, SUM(net_amount) AS revenue FROM biz.sales_invoices GROUP BY 1",
    columns: ["month", "revenue"],
    rows: [["2026-11", "420000"]],
    row_count: 1,
    truncated: false,
    as_of: "2026-11-25",
    read_at: "2026-11-25T10:00:00Z",
    drawn: null,
  });
});

describe("OntologyAndMeasuresPanel", () => {
  it("builds lineage ONLY from real data (Data tables -> Governed measures -> Live tiles) with zero fake Ask agent nodes", () => {
    const norm = buildNormalizedMeasures(sampleMeasures, sampleItems);
    const graph = buildOntologyGraph(norm, sampleItems, new Set(["measure_revenue_invoiced"]), "All");
    expect(graph.nodes.some((n) => n.id === "asset:biz.sales_invoices")).toBe(true);
    expect(graph.nodes.some((n) => n.id === "measure:revenue_invoiced")).toBe(true);
    expect(graph.nodes.some((n) => n.id === "surface:tile:measure_revenue_invoiced")).toBe(true);
    expect(graph.nodes.some((n) => n.id.startsWith("surface:ask:"))).toBe(false);
    expect(graph.nodes.some((n) => /Ask agent/i.test(n.title))).toBe(false);
    expect(graph.edges.some((e) => e.from === "asset:biz.sales_invoices" && e.to === "measure:revenue_invoiced")).toBe(true);
    expect(recordsHrefFor("biz.purchase_orders", "Purchasing")).toBe("/records?kind=orders");
    expect(recordsHrefFor("biz.sales_invoices", "Finance")).toBe("/records?kind=documents");
  });

  it("renders interactive DotGridCanvas, instant search, domain filter, live SQL + Copy, Add to Dashboard, Ask, and Explore data", async () => {
    const onToggleCanvas = vi.fn();
    const onJumpToTile = vi.fn();
    render(
      <OntologyAndMeasuresPanel
        measures={sampleMeasures}
        items={sampleItems}
        canvasIdSet={new Set()}
        onToggleCanvas={onToggleCanvas}
        onJumpToTile={onJumpToTile}
      />,
    );

    expect(screen.getByTestId("dot-grid-canvas")).toBeTruthy();
    expect(screen.getByTestId("ontology-inspector")).toHaveTextContent("Revenue invoiced");
    expect(screen.getByTestId("ontology-inspector")).toHaveTextContent("biz.sales_invoices");
    expect(screen.getByTestId("inspector-explore-data-btn").getAttribute("href")).toBe("/records?kind=documents");
    expect(screen.getByTestId("inspector-copy-sql")).toBeTruthy();

    await waitFor(() =>
      expect(screen.getByTestId("ontology-inspector")).toHaveTextContent("SUM(net_amount)"),
    );

    // Add to Dashboard triggers toggle callback
    fireEvent.click(screen.getByTestId("inspector-pin-btn"));
    expect(onToggleCanvas).toHaveBeenCalledWith(sampleItems[0]);

    // Live instant search narrows graph & inspector immediately
    fireEvent.change(screen.getByTestId("ontology-search"), { target: { value: "purchase" } });
    expect(screen.getByTestId("ontology-inspector")).toHaveTextContent("Late purchase orders");
    expect(screen.getByTestId("inspector-explore-data-btn").getAttribute("href")).toBe("/records?kind=orders");

    // Clear search and filter by Purchasing domain
    fireEvent.change(screen.getByTestId("ontology-search"), { target: { value: "" } });
    const filters = screen.getByTestId("ontology-domain-filters");
    fireEvent.click(within(filters).getByRole("button", { name: /Purchasing/ }));
    expect(screen.getByTestId("ontology-inspector")).toHaveTextContent("Late purchase orders");
    expect(screen.getByTestId("ontology-inspector")).toHaveTextContent("biz.purchase_orders");
  });

  it("paginates the Measures catalog at 12 rows per page and renders Proposed measures null-safely", async () => {
    const manyMeasures = Array.from({ length: 16 }, (_, idx) => ({
      id: `m_${idx + 1}`,
      name: `Measure ${idx + 1}`,
      definition: `Governed definition ${idx + 1}.`,
      owner: idx % 2 === 0 ? "Finance" : "Operations",
      unit: "count",
      source: `view_${idx + 1}`,
      dimensions: ["month"],
      period: "last 30 days",
      ask: `Show measure ${idx + 1}`,
    }));

    render(
      <OntologyAndMeasuresPanel
        measures={manyMeasures}
        items={[]}
        canvasIdSet={new Set()}
        onToggleCanvas={vi.fn()}
        onJumpToTile={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByTestId("subtab-measures-catalog"));
    const table = screen.getByTestId("measures-catalog-table");
    expect(table.querySelectorAll("tbody tr")).toHaveLength(12);
    fireEvent.click(screen.getByTestId("measures-show-more"));
    expect(table.querySelectorAll("tbody tr")).toHaveLength(16);

    // Switch to Proposed measures with null/undefined counts payload
    fireEvent.click(screen.getByTestId("subtab-candidate-review"));
    await waitFor(() => expect(screen.getByTestId("candidates-empty")).toBeTruthy());
  });
});
