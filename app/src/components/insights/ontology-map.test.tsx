import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { InsightInfo, InsightList } from "@/lib/types";

const getMeasureCandidates = vi.fn();
const getInsight = vi.fn();
const createAssistWorkflow = vi.fn();
vi.mock("@/lib/api", () => ({
  getMeasureCandidates: (...a: unknown[]) => getMeasureCandidates(...a),
  approveMeasureCandidate: vi.fn(),
  dismissMeasureCandidate: vi.fn(),
  getInsight: (...a: unknown[]) => getInsight(...a),
  createAssistWorkflow: (...a: unknown[]) => createAssistWorkflow(...a),
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
  createAssistWorkflow.mockReset().mockResolvedValue({
    workflow_id: "wf-1",
    name: "Revenue invoiced monitor",
    instruction: "Check Revenue invoiced",
    trigger_kind: "schedule",
    trigger: { cadence: "weekdays", hour: 8, minute: 0 },
    trigger_summary: "Weekdays at 8:00 AM ET",
    source_kind: "plain_language",
    source_ref: "",
    status: "active",
    pause_reason: "",
    created_at: "2026-11-26T12:00:00Z",
    updated_at: "2026-11-26T12:00:00Z",
    consecutive_failures: 0,
    last_error: "",
    runs: [],
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

  it("renders side-by-side Split Explorer immediately with Measures catalog, dotted grid Lineage canvas, and docked SQL Inspector", async () => {
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

    // Side-by-side Split Explorer mounts BOTH left catalog AND right dotted grid canvas + docked inspector immediately
    expect(screen.getByTestId("measures-catalog-table")).toBeTruthy();
    expect(screen.getByTestId("dot-grid-canvas")).toBeTruthy();
    expect(screen.getByTestId("ontology-inspector")).toBeTruthy();
    expect(screen.getByTestId("measure-row-revenue_invoiced")).toHaveTextContent("Net line revenue on posted sales invoices.");

    // 1-click Pin to canvas from catalog
    fireEvent.click(screen.getByTestId("measure-add-canvas-revenue_invoiced"));
    expect(onToggleCanvas).toHaveBeenCalledWith(sampleItems[0]);

    // 1-click Set trigger opens the in-page trigger modal prefilled for the measure
    fireEvent.click(screen.getByTestId("measure-set-trigger-revenue_invoiced"));
    expect(screen.getByTestId("create-trigger-modal")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Close trigger builder"));
    expect(screen.queryByTestId("create-trigger-modal")).toBeNull();

    // 1-click Inspect selects the measure and keeps both catalog & dotted grid Lineage canvas visible
    fireEvent.click(screen.getByTestId("measure-inspect-lineage-revenue_invoiced"));
    expect(screen.getByTestId("dot-grid-canvas")).toBeTruthy();
    expect(screen.getByTestId("ontology-inspector")).toHaveTextContent("Revenue invoiced");
    expect(screen.getByTestId("ontology-inspector")).toHaveTextContent("biz.sales_invoices");
    expect(screen.getByTestId("inspector-explore-data-btn").getAttribute("href")).toBe("/records?kind=documents");
    expect(screen.getByTestId("inspector-copy-sql")).toBeTruthy();

    await waitFor(() =>
      expect(screen.getByTestId("ontology-inspector")).toHaveTextContent("SUM(net_amount)"),
    );

    // Docked SQL inspector collapses and re-expands cleanly
    fireEvent.click(screen.getByTestId("inspector-collapse-toggle"));
    expect(screen.queryByTestId("inspector-copy-sql")).toBeNull();
    fireEvent.click(screen.getByTestId("inspector-collapse-toggle"));
    expect(screen.getByTestId("inspector-copy-sql")).toBeTruthy();

    // Pin to canvas from inspector triggers toggle callback
    fireEvent.click(screen.getByTestId("inspector-pin-btn"));
    expect(onToggleCanvas).toHaveBeenCalledTimes(2);

    // Expand graph toggle focuses full-width canvas while keeping search and filters usable
    fireEvent.click(screen.getByTestId("subtab-ontology-graph"));
    expect(screen.getByTestId("dot-grid-canvas")).toBeTruthy();

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

    const table = screen.getByTestId("measures-catalog-table");
    expect(table.querySelectorAll("tbody tr")).toHaveLength(12);
    fireEvent.click(screen.getByTestId("measures-show-more"));
    expect(table.querySelectorAll("tbody tr")).toHaveLength(16);

    // Switch to Proposed measures with null/undefined counts payload
    fireEvent.click(screen.getByTestId("subtab-candidate-review"));
    await waitFor(() => expect(screen.getByTestId("candidates-empty")).toBeTruthy());
  });
});
