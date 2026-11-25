import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MeasureCandidate, MeasureCandidateList } from "@/lib/types";

const getMeasureCandidates = vi.fn();
const approveMeasureCandidate = vi.fn();
const dismissMeasureCandidate = vi.fn();
vi.mock("@/lib/api", () => ({
  getMeasureCandidates: (...a: unknown[]) => getMeasureCandidates(...a),
  approveMeasureCandidate: (...a: unknown[]) => approveMeasureCandidate(...a),
  dismissMeasureCandidate: (...a: unknown[]) => dismissMeasureCandidate(...a),
}));

import { MeasureCandidates, sourceWords, usageWords } from "./measure-candidates";

const sample = (over: Partial<MeasureCandidate> = {}): MeasureCandidate => ({
  id: "mc-1",
  signature: "sig-abc",
  status: "pending",
  source_kind: "accepted_answer",
  source_ref: "ans-1",
  tile_title: null,
  question: "What is item revenue by customer?",
  sql: "SELECT customer, sum(revenue) FROM sales_invoice_lines GROUP BY customer",
  views: ["sales_invoice_lines"],
  sql_mode: "structured",
  usage_count: 4,
  first_seen_at: "2026-11-24T12:00:00Z",
  last_seen_at: "2026-11-25T05:00:00Z",
  proposed_id: "item_revenue_by_customer",
  proposed_name: "Item revenue by customer",
  proposed_description: "Defined from usage: What is item revenue by customer?",
  proposed_owner: "Finance",
  proposed_unit: "money",
  proposed_grain: "row",
  proposed_view: "sales_invoice_lines",
  proposed_value: "sum(revenue)",
  proposed_per: "revenue_currency",
  proposed_date: "",
  proposed_where: "",
  proposed_dimensions: ["customer"],
  proposer_name: "Ana",
  reviewed_by: null,
  reviewed_at: null,
  dismiss_reason: null,
  approved_measure_id: null,
  withheld: [],
  ...over,
});

const listOf = (candidates: MeasureCandidate[], can_review = true): MeasureCandidateList => ({
  can_review,
  total: candidates.length,
  counts: {
    pending: candidates.filter((c) => c.status === "pending").length,
    approved: candidates.filter((c) => c.status === "approved").length,
    dismissed: candidates.filter((c) => c.status === "dismissed").length,
  },
  candidates,
});

beforeEach(() => {
  getMeasureCandidates.mockReset().mockResolvedValue(listOf([sample()]));
  approveMeasureCandidate.mockReset().mockImplementation((id: string, body?: { name?: string; description?: string; owner?: string }) =>
    Promise.resolve(
      sample({
        id,
        status: "approved",
        approved_measure_id: "item_revenue_by_customer",
        proposed_name: body?.name ?? "Item revenue by customer",
        proposed_description: body?.description ?? "Defined from usage: What is item revenue by customer?",
        proposed_owner: body?.owner ?? "Finance",
        reviewed_by: "Owner",
        reviewed_at: "2026-11-25T06:00:00Z",
      }),
    ),
  );
  dismissMeasureCandidate.mockReset().mockImplementation((id: string) =>
    Promise.resolve(sample({ id, status: "dismissed", reviewed_by: "Owner", reviewed_at: "2026-11-25T06:00:00Z" })),
  );
});

describe("MeasureCandidates review queue", () => {
  it("lists pending candidates with SQL toggle and usage count, and lets the owner edit and approve", async () => {
    render(<MeasureCandidates />);
    await waitFor(() => expect(screen.getByTestId("candidate-mc-1")).toBeTruthy());
    const card = screen.getByTestId("candidate-mc-1");
    expect(card).toHaveTextContent("Item revenue by customer");
    expect(card).toHaveTextContent("Used 4 times");
    expect(card).toHaveTextContent("From accepted answer");
    expect(card).toHaveTextContent("Views: sales_invoice_lines");

    // SQL is behind the query toggle and expands cleanly.
    const toggle = screen.getByTestId("candidate-sql-mc-1");
    fireEvent.click(screen.getByText("Show query"));
    expect(toggle).toHaveTextContent("SELECT customer, sum(revenue)");

    // Owner edits details and approves.
    fireEvent.click(screen.getByText("Edit details"));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Net item revenue" } });
    fireEvent.change(screen.getByLabelText("Owner"), { target: { value: "Sales" } });
    fireEvent.click(screen.getByText("Save and approve"));

    await waitFor(() =>
      expect(approveMeasureCandidate).toHaveBeenCalledWith("mc-1", {
        name: "Net item revenue",
        description: "Defined from usage: What is item revenue by customer?",
        owner: "Sales",
      }),
    );
    await waitFor(() => expect(screen.getByTestId("candidates-empty")).toBeTruthy());

    // Switching to Approved shows the promoted definition.
    fireEvent.click(screen.getByText("Approved (1)"));
    await waitFor(() => expect(screen.getByTestId("candidate-mc-1")).toHaveTextContent("Approved as item_revenue_by_customer"));
  });

  it("lets the owner dismiss a pending candidate", async () => {
    render(<MeasureCandidates />);
    await waitFor(() => expect(screen.getByText("Dismiss")).toBeTruthy());
    fireEvent.click(screen.getByText("Dismiss"));
    await waitFor(() => expect(dismissMeasureCandidate).toHaveBeenCalledWith("mc-1"));
    await waitFor(() => expect(screen.getByTestId("candidates-empty")).toBeTruthy());
  });

  it("hides Approve and Dismiss for non-owner admins and shows withheld notice when masked", async () => {
    getMeasureCandidates.mockResolvedValue(
      listOf([
        sample({
          sql: null,
          question: null,
          proposed_name: "Withheld candidate definition",
          proposed_description: null,
          proposed_where: "",
          proposed_dimensions: [],
          views: [],
          withheld: ["finance"],
        }),
      ], false),
    );
    render(<MeasureCandidates />);
    await waitFor(() => expect(screen.getByTestId("candidates-owner-only")).toBeTruthy());
    expect(screen.queryByText("Approve")).toBeNull();
    expect(screen.queryByText("Dismiss")).toBeNull();
    expect(screen.queryByTestId("candidate-sql-mc-1")).toBeNull();
    const card = screen.getByTestId("candidate-mc-1");
    expect(card).toHaveTextContent("Withheld candidate definition");
    expect(card).toHaveTextContent("Definition details withheld");
    expect(card).toHaveTextContent("Parts of this statement are withheld for your access.");
    expect(card).not.toHaveTextContent("Custom statement");
  });

  it("pure: source and usage wording", () => {
    expect(sourceWords("accepted_answer")).toBe("From accepted answer");
    expect(sourceWords("saved_tile")).toBe("From pinned chart");
    expect(sourceWords("manual")).toBe("Proposed");
    expect(usageWords(1)).toBe("Used once");
    expect(usageWords(7)).toBe("Used 7 times");
  });
});
