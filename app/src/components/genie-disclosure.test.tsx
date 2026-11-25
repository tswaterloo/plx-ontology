import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  readStatement: vi.fn(),
  saveInsight: vi.fn(),
  proposeMeasureCandidate: vi.fn(),
  exportXlsx: vi.fn(),
  saveMemory: vi.fn(),
  uploadAskFile: vi.fn(),
}));

import * as api from "@/lib/api";
import { addFiles, refusal } from "@/lib/attachments";
import type { Answer, InsightInfo, InsightResult } from "@/lib/types";
import { AnswerCharts, pinChartSpec, type AskChart } from "./answer-chart";
import { AskNote } from "./ask-note";
import { chartNotes } from "./charts/chart-view";
import { chartToTable, csvCell, toCsv } from "./charts/export";
import { InsightTile } from "./insights/dashboard";
import { AnswerCard, type Turn } from "./answer-card";

describe("GG1c: chart aggregation disclosure", () => {
  const aggregatedChart: AskChart = {
    kind: "line",
    title: "Daily invoiced revenue",
    x: "day",
    labels: ["2026-01-05", "2026-01-12", "2026-01-19"],
    series: [{ name: "revenue", values: [71.75, 84.0, 62.5] }],
    rows: 240,
    shown: 240,
    aggregation: "240 daily points shown as 35 weekly totals",
    currency: "CAD",
  };

  it("renders the aggregation disclosure cleanly in the chart subtitle without duplicating it in bottom notes", () => {
    render(<AnswerCharts charts={[aggregatedChart]} />);
    const subtitle = screen.getByTestId("chart-aggregation");
    expect(subtitle).toHaveTextContent("240 daily points shown as 35 weekly totals");
    expect(subtitle.textContent).not.toContain("\u2014");
    // Exact once in the rendered chart
    expect(screen.getAllByText(/240 daily points shown as 35 weekly totals/)).toHaveLength(1);
  });

  it("includes the aggregation disclosure in chartNotes for PNG exports and omits it when not aggregated", () => {
    expect(chartNotes(aggregatedChart)).toContain("240 daily points shown as 35 weekly totals.");
    const plain: AskChart = { ...aggregatedChart, aggregation: null };
    expect(chartNotes(plain).join(" ")).not.toContain("weekly totals");
  });
});

describe("GG2: Sources panel result profile and coverage rows", () => {
  const answerWithProfile: Answer = {
    answer_id: "a-gg2",
    question: "How much did we spend on packaging this year?",
    conversation_id: "c-gg2",
    outcome: "answered",
    latency_ms: 4200,
    access_label: null,
    direct_answer: "Packaging spend totaled 90,000 CAD through Nov 20.",
    facts: [],
    not_found: [{ what: "freight surcharges", where_it_would_live: "carrier invoices" }],
    hidden: { count: 0 },
    conflicts: [],
    freshness: { as_of: "2026-11-25T10:00:00Z" },
    coverage: [
      { part: "Purchase orders", sources: ["purchase_orders"], period: "2026-01-10 to 2026-11-20", reads: 1, found: 240 },
    ],
    cited: [
      {
        about: "purchase orders",
        columns: ["order_date", "total"],
        rows: [["2026-01-10", "100"], ["2026-11-20", "250"]],
        records: 240,
        as_of: "2026-11-25",
        profile: {
          rows_read: 240,
          rows_shown: 2,
          exact: true,
          time_range: "2026-01-10 to 2026-11-20",
          views: ["purchase_orders"],
          coverage_note: "Showing 2 of 240 rows.",
        },
      },
    ],
  };

  it("surfaces structured result profile metadata and coverage rows inside the expandable Sources panel without cluttering the main answer", async () => {
    const turn: Turn = { id: "t-gg2", question: answerWithProfile.question, phase: "done", streamed: "", answer: answerWithProfile };
    render(<AnswerCard turn={turn} />);

    // Main answer text stays uncluttered
    const answerText = screen.getByTestId("simple-answer-text");
    expect(answerText).toHaveTextContent("Packaging spend totaled 90,000 CAD through Nov 20.");
    expect(answerText).not.toHaveTextContent("Rows read");

    // Expand Sources panel
    const sources = screen.getByTestId("simple-sources");
    expect(sources).not.toHaveAttribute("open");
    await userEvent.click(screen.getByText("Sources (240 records)"));
    expect(sources).toHaveAttribute("open");

    // Structured result profile strip inside Sources
    const profile = screen.getByTestId("source-profile");
    expect(profile).toHaveTextContent("240 rows read (showing 2)");
    expect(profile).toHaveTextContent("2026-01-10 to 2026-11-20");
    expect(profile).toHaveTextContent("purchase_orders");
    expect(screen.getByTestId("source-coverage-note")).toHaveTextContent("Showing 2 of 240 rows.");

    // Coverage rows and missing coverage note inside Sources
    const coverage = screen.getByTestId("sources-coverage");
    expect(coverage).toHaveTextContent("Purchase orders");
    expect(coverage).toHaveTextContent("read, 240 found");
    expect(screen.getByTestId("sources-coverage-gaps")).toHaveTextContent(
      "Not captured: freight surcharges (expected in carrier invoices).",
    );
    expect(sources.textContent).not.toContain("\u2014");
  });
});

describe("Genie 2026 completeness: verified measures, how calculated, Chart/Table toggle, CSV, follow-ups, attachments, instructions", () => {
  const revChart: AskChart = {
    kind: "bar",
    title: "Revenue by month",
    x: "month",
    labels: ["2026-10", "2026-11"],
    series: [{ name: "revenue", values: [125000, 148500] }],
    currency: "CAD",
    sql: "SELECT month, revenue FROM sales_invoices",
    measure: {
      id: "revenue",
      name: "Revenue",
      definition: "Posted sales invoices net of credits.",
      owner: "Finance",
      period: "Last 2 months",
    },
  };

  it("shows Verified measure badge, How this was calculated steps, Inspect SQL, AttachmentNotes, and clickable follow-up chips on SimpleAnswer", async () => {
    const onAsk = vi.fn();
    const ans: Answer = {
      answer_id: "a-genie",
      question: "What is monthly revenue?",
      conversation_id: "c-genie",
      outcome: "answered",
      latency_ms: 1200,
      access_label: null,
      direct_answer: "Revenue was 148,500 CAD in November.",
      facts: [],
      not_found: [],
      hidden: { count: 0 },
      conflicts: [],
      freshness: { as_of: "2026-11-25T10:00:00Z" },
      definitions: [{ id: "revenue", name: "Revenue", definition: "Posted sales invoices net of credits.", owner: "Finance", version: "v2a3b4c5d", defined_on: "2026-11-23" }],
      follow_ups: ["Break down Revenue by customer", "Compare Revenue against the same period last year"],
      attachments: [{ name: "targets.csv", kind: "spreadsheet", size: 420, digest: "d1", read: true, note: null }],
      cited: [{
        about: "sales invoices",
        columns: ["month", "revenue"],
        rows: [["2026-10", "125000"], ["2026-11", "148500"]],
        records: 2,
        as_of: "2026-11-25",
        query: "SELECT month, revenue FROM sales_invoices WHERE status = 'posted'",
        work: { views: ["sales_invoices"], more_filters: 0, filters: [{ index: 0, text: "status = 'posted'" }] },
      }],
    };
    const turn: Turn = { id: "t-genie", question: ans.question, phase: "done", streamed: "", answer: ans };
    render(<AnswerCard turn={turn} onAsk={onAsk} />);

    // Verified measure badge
    expect(screen.getByTestId("verified-measure-badge")).toHaveTextContent("Verified measure");
    // Attachment status shown on SimpleAnswer
    expect(screen.getByTestId("attachment-notes")).toHaveTextContent("Read from your attachment: targets.csv.");
    // Plain-language How this was calculated + Inspect SQL
    const verify = screen.getByTestId("simple-verification");
    expect(verify).toHaveTextContent("How this was calculated");
    await userEvent.click(verify.querySelector("summary")!);
    expect(screen.getByTestId("how-calculated-steps")).toHaveTextContent("Used verified measure Revenue (Finance)");
    expect(screen.getByTestId("how-calculated-steps")).toHaveTextContent("Read sales invoices filtered by status = 'posted'");
    expect(screen.getByTestId("how-calculated-sql")).toHaveTextContent("SELECT month, revenue FROM sales_invoices WHERE status = 'posted'");
    // Contextual follow-up chips submit cleanly
    const followUps = screen.getByTestId("simple-follow-ups");
    expect(followUps.textContent).not.toContain("\u2014");
    await userEvent.click(screen.getByRole("button", { name: "Break down Revenue by customer" }));
    expect(onAsk).toHaveBeenCalledWith("Break down Revenue by customer");
  });

  it("toggles cleanly between Chart view and Data Table view on Ask charts and exports RFC-4180 CSV", async () => {
    render(<AnswerCharts charts={[revChart]} question="Plot revenue" />);
    expect(screen.getByTestId("verified-measure-badge")).toHaveTextContent("Verified measure");
    expect(screen.getByRole("img", { name: "Revenue by month" })).toBeInTheDocument();
    expect(screen.queryByTestId("chart-data-table")).toBeNull();

    const toggle = screen.getByTestId("chart-view-toggle");
    await userEvent.click(toggle.querySelectorAll("button")[1]);
    expect(screen.getByTestId("chart-data-table")).toBeInTheDocument();
    expect(screen.getByTestId("chart-data-table")).toHaveTextContent("148,500 CAD");

    // Save menu includes CSV alongside Excel/PNG
    await userEvent.click(screen.getByLabelText("Save the chart"));
    expect(screen.getByRole("menuitem", { name: "CSV" })).toBeInTheDocument();

    // CSV formatting & formula safety + multi-panel conversion
    expect(csvCell("=SUM(A1)")).toBe("'=SUM(A1)");
    expect(csvCell(-42.5)).toBe("-42.5");
    const tbl = chartToTable(revChart);
    expect(tbl.columns).toEqual(["Month", "Revenue"]);
    expect(toCsv(tbl.columns, tbl.rows)).toBe("Month,Revenue\r\n2026-10,125000\r\n2026-11,148500\r\n");

    // pinChartSpec normalizes stacked_bar and panel-only charts cleanly
    expect(pinChartSpec({
      ...revChart,
      kind: "stacked_bar" as unknown as AskChart["kind"],
      spec: undefined,
    })).toEqual({ kind: "bar", x: "month", y: ["revenue"], stacked: true });
  });

  it("toggles cleanly between Chart view and Data Table view on Insights tiles with CSV download", async () => {
    const item: InsightInfo = {
      id: "measure_revenue",
      title: "Monthly Revenue",
      about: "Posted sales invoices",
      group: "Measures: Finance",
      saved: false,
      schedule: "",
      notify: "never",
    };
    const result: InsightResult = {
      ...item,
      sql: "SELECT month, revenue FROM sales_invoices",
      columns: ["month", "revenue"],
      rows: [["2026-10", "125000"], ["2026-11", "148500"]],
      people: [null, null],
      row_count: 2,
      truncated: false,
      read_at: "2026-11-25T10:00:00Z",
      as_of: "2026-11-25",
      drawn: revChart,
    };
    render(<InsightTile item={item} entry={{ result }} streaming={false} />);
    expect(screen.getByTestId("chart-body-measure_revenue")).toBeInTheDocument();
    const toggle = screen.getByTestId("tile-view-toggle-measure_revenue");
    await userEvent.click(toggle.querySelectorAll("button")[1]);
    expect(screen.queryByTestId("chart-body-measure_revenue")).toBeNull();
    expect(screen.getByTestId("csv-export-btn")).toBeInTheDocument();
    expect(screen.getByText("148500")).toBeInTheDocument();
  });

  it("AskNote saves standing instructions as kind=instruction and context notes as kind=fact", async () => {
    vi.mocked(api.saveMemory).mockReset().mockResolvedValue({
      id: 42, kind: "instruction", text: "Always show CAD first", source: "you",
      at: "2026-11-25T10:00:00Z", records: [], confidence: 1, importance: 0.9,
    });
    render(<AskNote record="SalesOrder:SO-1" inline />);
    await userEvent.click(screen.getByTestId("ask-note-open"));
    await userEvent.click(screen.getByTestId("ask-note-kind-instruction"));
    await userEvent.type(screen.getByLabelText(/Standing instruction/i), "Always show CAD first");
    await userEvent.click(screen.getByTestId("ask-note-save"));
    expect(api.saveMemory).toHaveBeenCalledWith({ kind: "instruction", text: "Always show CAD first" });
    expect(screen.getByRole("status")).toHaveTextContent("Instruction saved");
  });

  it("attachments validation supports pptx/xls/doc, rejects empty files, and surfaces server upload errors", async () => {
    expect(refusal({ name: "deck.pptx", size: 120, type: "" }, [])).toBeNull();
    expect(refusal({ name: "legacy.xls", size: 120, type: "" }, [])).toBeNull();
    expect(refusal({ name: "memo.doc", size: 120, type: "" }, [])).toBeNull();
    expect(refusal({ name: "empty.csv", size: 0, type: "text/csv" }, [])).toContain("is empty");
    vi.mocked(api.uploadAskFile).mockRejectedValueOnce(new Error("Disk full"));
    const csv = new File(["a,b\n1,2"], "data.csv", { type: "text/csv" });
    const res = await addFiles([csv], []);
    expect(res.next).toHaveLength(0);
    expect(res.problems[0]).toContain("data.csv");
  });
});
