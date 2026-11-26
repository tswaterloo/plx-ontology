import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/errors";
import type { InsightInfo, InsightList } from "@/lib/types";

const getObserved = vi.fn();
const getGaps = vi.fn();
const getInsight = vi.fn();
const sendNoteFeedback = vi.fn();
const getMeasureCandidates = vi.fn().mockResolvedValue({ candidates: [], counts: { pending: 0, approved: 0, dismissed: 0 }, total: 0, can_review: true });
const getFeedback = vi.fn().mockResolvedValue({ entries: [], all_readers: false });
const getAllFeedback = vi.fn().mockResolvedValue({ entries: [], all_readers: false });
const setFeedbackStatus = vi.fn();

vi.mock("@/lib/api", () => ({
  getObserved: (...a: unknown[]) => getObserved(...a),
  getGaps: (...a: unknown[]) => getGaps(...a),
  getInsight: (...a: unknown[]) => getInsight(...a),
  sendNoteFeedback: (...a: unknown[]) => sendNoteFeedback(...a),
  getMeasureCandidates: (...a: unknown[]) => getMeasureCandidates(...a),
  getFeedback: (...a: unknown[]) => getFeedback(...a),
  getAllFeedback: (...a: unknown[]) => getAllFeedback(...a),
  setFeedbackStatus: (...a: unknown[]) => setFeedbackStatus(...a),
}));

import { BENCHMARKS_STORAGE_KEY, BenchmarksPanel } from "./benchmarks-panel";

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
  it("lists real saved monitors on tab 1 and runs live getInsight on Run now", async () => {
    render(<BenchmarksPanel list={list} />);
    await waitFor(() => expect(screen.getByTestId("bench-kpi-accuracy")).toHaveTextContent("90%"));
    expect(screen.getByTestId("bench-kpi-speed")).toHaveTextContent("2.4s");
    expect(screen.getByTestId("monitors-table")).toHaveTextContent("Late POs by supplier");

    fireEvent.click(screen.getByTestId("run-monitor-saved_late_pos"));
    await waitFor(() => expect(getInsight).toHaveBeenCalledWith("saved_late_pos", expect.any(AbortSignal), true));
    await waitFor(() => expect(screen.getByTestId("monitor-result-saved_late_pos")).toHaveTextContent("2 rows"));
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
