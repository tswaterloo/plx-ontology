import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/errors";
import type { InsightInfo, InsightList } from "@/lib/types";

const getObserved = vi.fn();
const getGaps = vi.fn();
const getInsight = vi.fn();
const saveInsight = vi.fn();
const deleteInsight = vi.fn();
const setInsightSchedule = vi.fn();
const sendNoteFeedback = vi.fn();
const getAssistWorkflows = vi.fn();
const createAssistWorkflow = vi.fn();
const runAssistWorkflow = vi.fn();
const pauseAssistWorkflow = vi.fn();
const resumeAssistWorkflow = vi.fn();
const deleteAssistWorkflow = vi.fn();
const getMeasureCandidates = vi.fn().mockResolvedValue({ candidates: [], counts: { pending: 0, approved: 0, dismissed: 0 }, total: 0, can_review: true });
const getFeedback = vi.fn().mockResolvedValue({ entries: [], all_readers: false });
const getAllFeedback = vi.fn().mockResolvedValue({ entries: [], all_readers: false });
const setFeedbackStatus = vi.fn();

vi.mock("@/lib/api", () => ({
  getObserved: (...a: unknown[]) => getObserved(...a),
  getGaps: (...a: unknown[]) => getGaps(...a),
  getInsight: (...a: unknown[]) => getInsight(...a),
  saveInsight: (...a: unknown[]) => saveInsight(...a),
  deleteInsight: (...a: unknown[]) => deleteInsight(...a),
  setInsightSchedule: (...a: unknown[]) => setInsightSchedule(...a),
  sendNoteFeedback: (...a: unknown[]) => sendNoteFeedback(...a),
  getAssistWorkflows: (...a: unknown[]) => getAssistWorkflows(...a),
  createAssistWorkflow: (...a: unknown[]) => createAssistWorkflow(...a),
  runAssistWorkflow: (...a: unknown[]) => runAssistWorkflow(...a),
  pauseAssistWorkflow: (...a: unknown[]) => pauseAssistWorkflow(...a),
  resumeAssistWorkflow: (...a: unknown[]) => resumeAssistWorkflow(...a),
  deleteAssistWorkflow: (...a: unknown[]) => deleteAssistWorkflow(...a),
  getMeasureCandidates: (...a: unknown[]) => getMeasureCandidates(...a),
  getFeedback: (...a: unknown[]) => getFeedback(...a),
  getAllFeedback: (...a: unknown[]) => getAllFeedback(...a),
  setFeedbackStatus: (...a: unknown[]) => setFeedbackStatus(...a),
}));

import { BENCHMARKS_STORAGE_KEY, BenchmarksPanel } from "./benchmarks-panel";
import { CreateInsightModal } from "./create-insight-modal";

const savedMonitor: InsightInfo = {
  id: "saved_late_pos",
  title: "Late POs by supplier",
  about: "Open late purchase orders by supplier",
  group: "Purchasing",
  saved: true,
  question: "Which suppliers have late purchase orders?",
  schedule: "daily",
  notify: "new_rows",
  chart: null,
};

const revenueTile: InsightInfo = {
  id: "measure_revenue",
  title: "Revenue invoiced",
  about: "Net line revenue on posted sales invoices.",
  group: "Measures: Finance",
  saved: false,
  question: null,
  schedule: "",
  notify: "never",
  chart: { kind: "line", x: "month", y: ["revenue"] },
  measure: {
    id: "revenue",
    name: "Revenue invoiced",
    definition: "Net line revenue on posted sales invoices.",
    owner: "Finance",
    version: "v1",
    defined_on: "2026-11-23",
  },
};

const list: InsightList = {
  scope: "everyone",
  saving: true,
  items: [savedMonitor, revenueTile],
  measures: [
    {
      id: "revenue",
      name: "Revenue invoiced",
      definition: "Net line revenue on posted sales invoices.",
      owner: "Finance",
      unit: "money",
      source: "sales_invoice_lines",
      dimensions: ["customer", "month"],
      period: "last 30 days by invoice_date",
      ask: "Why did revenue change last month?",
    },
  ],
};

beforeEach(() => {
  window.localStorage.clear();
  getAssistWorkflows.mockReset().mockResolvedValue({
    workflows: [],
    routines: [],
    templates: [],
    status: "ok",
    reason: "",
  });
  createAssistWorkflow.mockReset().mockResolvedValue({
    workflow_id: "wf-101",
    name: "Late purchase orders monitor",
    instruction: "Check Late purchase orders and alert when it exceeds 15 orders. Surface an alert on Home when triggered.",
    trigger_kind: "schedule",
    trigger: { cadence: "daily", hour: 8, minute: 0 },
    trigger_summary: "Daily at 8:00 AM ET",
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
  saveInsight.mockReset().mockResolvedValue({
    ...revenueTile,
    id: "saved_custom_1",
    title: "Monthly revenue by customer",
    saved: true,
  });
  deleteInsight.mockReset().mockResolvedValue(undefined);
  setInsightSchedule.mockReset().mockResolvedValue({ ...savedMonitor, schedule: "" });
  runAssistWorkflow.mockReset().mockResolvedValue({
    workflow: {
      workflow_id: "wf-101",
      name: "Late purchase orders monitor",
      instruction: "Check Late purchase orders and alert when it exceeds 15 orders. Surface an alert on Home when triggered.",
      trigger_kind: "schedule",
      trigger: { cadence: "daily", hour: 8, minute: 0 },
      trigger_summary: "Daily at 8:00 AM ET",
      source_kind: "plain_language",
      source_ref: "",
      status: "active",
      pause_reason: "",
      created_at: "2026-11-26T12:00:00Z",
      updated_at: "2026-11-26T12:00:00Z",
      consecutive_failures: 0,
      last_error: "",
      runs: [],
    },
    job: { job_id: "j-1", text: "Run", status: "queued" },
  });
  pauseAssistWorkflow.mockReset().mockResolvedValue({
    workflow_id: "wf-101",
    name: "Late purchase orders monitor",
    instruction: "Check Late purchase orders and alert when it exceeds 15 orders. Surface an alert on Home when triggered.",
    trigger_kind: "schedule",
    trigger: { cadence: "daily", hour: 8, minute: 0 },
    trigger_summary: "Daily at 8:00 AM ET",
    source_kind: "plain_language",
    source_ref: "",
    status: "paused",
    pause_reason: "",
    created_at: "2026-11-26T12:00:00Z",
    updated_at: "2026-11-26T12:00:00Z",
    consecutive_failures: 0,
    last_error: "",
    runs: [],
  });
  resumeAssistWorkflow.mockReset().mockResolvedValue({
    workflow_id: "wf-101",
    name: "Late purchase orders monitor",
    instruction: "Check Late purchase orders and alert when it exceeds 15 orders. Surface an alert on Home when triggered.",
    trigger_kind: "schedule",
    trigger: { cadence: "daily", hour: 8, minute: 0 },
    trigger_summary: "Daily at 8:00 AM ET",
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
  deleteAssistWorkflow.mockReset().mockResolvedValue({ workflow_id: "wf-101" });
  getObserved.mockReset().mockResolvedValue({
    available: true,
    days: [
      { day: "2026-11-24", answered: 18, failed: 2, unavailable: 0, median_ms: 2400, slowest_ms: 6100, median_steps: 2 },
    ],
    failures: [
      { at: "2026-11-24T12:00:00Z", question: "Show backlog by plant", error: "timeout" },
    ],
  });
  getGaps.mockReset().mockResolvedValue({
    on: true,
    gaps: [
      { kind: "measure", name: "gross_margin_by_lot", asked: 7, first: "2026-11-20", last: "2026-11-24", defined: false },
    ],
  });
  getInsight.mockReset().mockImplementation((id: string) =>
    Promise.resolve({
      ...(list.items.find((i) => i.id === id) ?? savedMonitor),
      sql: `SELECT * FROM biz.${id}`,
      columns: ["supplier", "late_pos"],
      rows: [["Croda Canada", "4"], ["Brenntag", "2"]],
      row_count: 2,
      truncated: false,
      as_of: "2026-11-25",
      read_at: "2026-11-25T10:00:00Z",
      drawn: null,
    }),
  );
  sendNoteFeedback.mockReset().mockResolvedValue({ id: "fb-1", status: "open" });
});

describe("BenchmarksPanel", () => {
  it("lists real saved monitors on tab 1, runs live getInsight on Run now, and opens the in-page CreateTriggerModal", async () => {
    render(<BenchmarksPanel list={list} />);
    await waitFor(() => expect(screen.getByTestId("bench-kpi-accuracy")).toHaveTextContent("90%"));
    expect(screen.getByTestId("bench-kpi-speed")).toHaveTextContent("2.4s");
    expect(screen.getByTestId("monitors-table")).toHaveTextContent("Late POs by supplier");

    fireEvent.click(screen.getByTestId("run-monitor-saved_late_pos"));
    await waitFor(() => expect(getInsight).toHaveBeenCalledWith("saved_late_pos", expect.any(AbortSignal), true));
    await waitFor(() => expect(screen.getByTestId("monitor-result-saved_late_pos")).toHaveTextContent("2 rows"));

    // Clicking "New trigger / monitor" opens the in-page builder instead of redirecting to /ask
    fireEvent.click(screen.getByTestId("new-monitor-btn"));
    expect(screen.getByTestId("create-trigger-modal")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("trigger-threshold-input"), { target: { value: "15" } });
    fireEvent.click(screen.getByTestId("trigger-schedule-daily_8am"));
    fireEvent.click(screen.getByTestId("trigger-delivery-both"));
    fireEvent.click(screen.getByTestId("create-trigger-submit"));

    await waitFor(() =>
      expect(createAssistWorkflow).toHaveBeenCalledWith(
        expect.objectContaining({
          name: "Late purchase orders monitor",
          trigger_kind: "schedule",
          trigger: { cadence: "daily", hour: 8, minute: 0 },
          instruction: expect.stringContaining("exceeds 15 orders"),
        }),
      ),
    );
    await waitFor(() => expect(screen.getByTestId("create-trigger-success")).toBeInTheDocument());
    expect(screen.getByTestId("triggers-table")).toHaveTextContent("Late purchase orders monitor");

    // Inline Run now, Pause, and Delete actions work directly on the flush triggers table
    fireEvent.click(screen.getByTestId("run-trigger-wf-101"));
    await waitFor(() => expect(runAssistWorkflow).toHaveBeenCalledWith("wf-101"));

    fireEvent.click(screen.getByTestId("toggle-trigger-wf-101"));
    await waitFor(() => expect(pauseAssistWorkflow).toHaveBeenCalledWith("wf-101"));
    await waitFor(() => expect(screen.getByTestId("trigger-row-wf-101")).toHaveTextContent("Paused"));

    fireEvent.click(screen.getByTestId("delete-trigger-wf-101"));
    await waitFor(() => expect(deleteAssistWorkflow).toHaveBeenCalledWith("wf-101"));
    await waitFor(() => expect(screen.queryByTestId("trigger-row-wf-101")).toBeNull());
  });

  it("starts accuracy checks honestly as Not run (never fake Checked), executes real Run, and supports Verify / Flag issue", async () => {
    render(<BenchmarksPanel list={list} initialSubTab="checks" />);
    await waitFor(() => expect(screen.getByTestId("bench-kpi-accuracy")).toHaveTextContent("90%"));

    // Honest default: Not run (NEVER fake Checked)
    expect(screen.getByTestId("check-status-measure_check_revenue")).toHaveTextContent("Not run");
    expect(screen.queryByText("Checked")).toBeNull();

    // Real Run executes getInsight(tileId) and records live row count + real server SQL
    fireEvent.click(screen.getByTestId("run-check-measure_check_revenue"));
    await waitFor(() => expect(getInsight).toHaveBeenCalledWith("measure_revenue", expect.any(AbortSignal), true));
    await waitFor(() => expect(screen.getByTestId("check-status-measure_check_revenue")).toHaveTextContent("Ran"));
    expect(screen.getByTestId("check-run-meta-measure_check_revenue")).toHaveTextContent("2 rows");

    // Verify and Flag issue actions
    fireEvent.click(screen.getByTestId("verify-check-measure_check_revenue"));
    expect(screen.getByTestId("check-status-measure_check_revenue")).toHaveTextContent("Verified");

    fireEvent.click(screen.getByTestId("flag-check-measure_check_revenue"));
    expect(screen.getByTestId("check-status-measure_check_revenue")).toHaveTextContent("Flagged");
    await waitFor(() => expect(sendNoteFeedback).toHaveBeenCalledTimes(1));
  });

  it("handles 403 admin-only telemetry cleanly on Data gaps & feedback tab", async () => {
    getObserved.mockRejectedValue(new ApiError(403, "forbidden"));
    getGaps.mockRejectedValue(new ApiError(403, "forbidden"));
    render(<BenchmarksPanel list={list} initialSubTab="gaps" />);
    await waitFor(() => expect(screen.getByTestId("bench-kpi-accuracy")).toHaveTextContent("admin only"));
    expect(screen.getByTestId("bench-kpi-gaps")).toHaveTextContent("admin only");
    expect(screen.getByTestId("gaps-feedback-panel")).toHaveTextContent("limited to the owner and admins");
  });

  it("lets a user add a custom accuracy check, persists it to localStorage, and prefills from a definition gap", async () => {
    render(<BenchmarksPanel list={list} initialSubTab="gaps" />);
    await waitFor(() => expect(screen.getByText("gross_margin_by_lot")).toBeTruthy());
    expect(screen.getByText("Show backlog by plant")).toBeTruthy();

    // Clicking Add check on a gap switches to Accuracy checks with prefilled form
    fireEvent.click(screen.getByRole("button", { name: "Add check" }));
    const input = screen.getByLabelText("Question to check") as HTMLInputElement;
    expect(input.value).toContain("gross margin by lot");

    fireEvent.change(input, {
      target: { value: "What are the top 5 late suppliers by open value?" },
    });
    fireEvent.change(screen.getByLabelText("Expected measure"), {
      target: { value: "Late purchase orders" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save check" }));

    await waitFor(() => expect(screen.getByText("What are the top 5 late suppliers by open value?")).toBeTruthy());
    expect(sendNoteFeedback).toHaveBeenCalledTimes(1);
    const savedRaw = window.localStorage.getItem(BENCHMARKS_STORAGE_KEY);
    expect(savedRaw).toContain("What are the top 5 late suppliers by open value?");
  });
});

describe("CreateInsightModal", () => {
  it("picks a governed measure with chart type and width preset and adds it immediately to My canvas", () => {
    const onAddTile = vi.fn();
    const onClose = vi.fn();
    render(
      <CreateInsightModal
        open
        onClose={onClose}
        items={list.items}
        measures={list.measures}
        canvasIdSet={new Set()}
        onAddTile={onAddTile}
      />,
    );

    expect(screen.getByTestId("create-insight-modal")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("create-insight-option-measure_revenue"));
    fireEvent.click(screen.getByTestId("create-insight-chart-horizontal_bar"));
    fireEvent.click(screen.getByTestId("create-insight-width-l"));
    fireEvent.click(screen.getByTestId("create-insight-submit"));

    expect(onAddTile).toHaveBeenCalledWith("measure_revenue", { size: "l", chartKind: "horizontal_bar" });
    expect(onClose).toHaveBeenCalled();
  });

  it("supports plain-English custom chart mode with transparent Instant live tile vs Custom AI query indicator", async () => {
    const onAddTile = vi.fn();
    const onCreated = vi.fn();
    const onClose = vi.fn();
    render(
      <CreateInsightModal
        open
        onClose={onClose}
        items={list.items}
        measures={list.measures}
        onAddTile={onAddTile}
        onCreated={onCreated}
      />,
    );

    fireEvent.click(screen.getByTestId("create-insight-mode-custom"));
    expect(screen.getByTestId("create-insight-mode-badge")).toHaveTextContent("Custom AI query");

    fireEvent.change(screen.getByTestId("create-insight-custom-prompt"), {
      target: { value: "Monthly revenue by top 10 customers" },
    });
    expect(screen.getByTestId("create-insight-ask-handoff").getAttribute("href")).toContain("/ask?q=");

    // Providing SQL upgrades it to an instant live tile saved via /api/insights
    fireEvent.click(screen.getByRole("button", { name: /Have SQL/i }));
    fireEvent.change(screen.getByTestId("create-insight-custom-sql"), {
      target: { value: "SELECT customer, SUM(net_amount) AS value FROM biz.sales_invoices GROUP BY 1 LIMIT 10" },
    });
    expect(screen.getByTestId("create-insight-mode-badge")).toHaveTextContent("Instant live tile");

    fireEvent.click(screen.getByTestId("create-insight-submit"));
    await waitFor(() => expect(saveInsight).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onAddTile).toHaveBeenCalledWith("saved_custom_1", expect.objectContaining({ size: "m" })));
    expect(onCreated).toHaveBeenCalledTimes(1);
  });
});
