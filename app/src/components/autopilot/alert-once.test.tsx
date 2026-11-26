import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { PreparedView } from "@/components/autopilot/prepared";
import type { PreparedItem } from "@/lib/api";

// Home's prepared items: a title that is its own body is said once; a Mission Control approval
// is one plain line with its details on open (the item is never hidden).
const raw = "Fix run on coding-1 worker/agent-alpha for TASK-5846 failed at apply.py:995 and needs a decision before the next run can start";
const base: PreparedItem = {
  item_id: "ap_mc", sender: "", channel: "system", subject: "Approval", message: raw, kind: "alert", to: [], cc: [], body: "", edited: false,
  ask: "", facts: [], summary: raw, created_at: "2026-11-24T14:00:00Z", alert_kind: "mc_approval_pending",
};

afterEach(cleanup);

describe("alert card says each thing once", () => {
  it("prints a Mission Control approval's text once in the line, the full text under Details", () => {
    render(<PreparedView items={[base]} empty="none" onGone={() => undefined} onChanged={() => undefined} />);
    expect(screen.getByTestId("alert-line").textContent).toBe(raw);
    const details = screen.getByTestId("alert-details");
    expect(details.textContent).toContain(raw);
    expect(details.textContent?.split(raw).length).toBe(2);   // once inside, not twice
  });
  it("prints an ordinary alert's title and identical body once", () => {
    render(<PreparedView items={[{ ...base, alert_kind: "qc_hold" }]} empty="none" onGone={() => undefined} onChanged={() => undefined} />);
    expect(screen.getByTestId("prepared-alert").textContent?.split(raw).length).toBe(2);
  });
  it("keeps a body that adds something", () => {
    render(<PreparedView items={[{ ...base, alert_kind: "qc_hold", message: "Lot B-1 is held by QC." }]} empty="none" onGone={() => undefined} onChanged={() => undefined} />);
    expect(screen.getByTestId("prepared-alert").textContent).toContain("Lot B-1 is held by QC.");
  });
  it("renders structured 2-column fact grid for QC hold details", () => {
    render(
      <PreparedView
        items={[
          {
            ...base,
            alert_kind: "qc_hold",
            subject: "QC hold on Lot B-441",
            summary: "Quality hold active on Lot B-441.",
            message: "",
            facts: [
              "Lot: B-441",
              "Product: Botanical Cleansing Gel 250ml",
              "Affected orders: SO 88210, SO 88219",
              "Reason: Viscosity out of range",
            ],
          },
        ]}
        empty="none"
        onGone={() => undefined}
        onChanged={() => undefined}
      />,
    );
    const grid = screen.getByTestId("qc-fact-grid");
    expect(grid).toHaveTextContent("Lot");
    expect(grid).toHaveTextContent("B-441");
    expect(grid).toHaveTextContent("Botanical Cleansing Gel 250ml");
    expect(grid).toHaveTextContent("SO 88210, SO 88219");
  });
});
