import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LabourView } from "./labour-view";
import type { LabourPage } from "@/lib/types";

const src = [{ handle: "mail:att/1", sent: "2026-06-08" }];
const ready: LabourPage = {
  state: "ready", note: "Groups of fewer than 3 people are not shown.", built_at: "2026-11-08T00:00:00Z",
  weeks: [{ department: "production", week: "2026-06-01", run: null, hours: "168.00", overtime_hours: "8.00",
    headcount_days: 20, week_basis: "stated", markings: ["hr"], sources: src }],
  trend: [{ department: "production", points: [{ week: "2026-06-01", hours: "168.00", overtime_hours: "8.00" }] }],
  alerts: [{ kind: "labour_overtime_spike", department: "production", week: "2026-06-01", overtime_hours: "8.00",
    baseline_median: "4.00", baseline_weeks: 8, rise_percent: "100", markings: ["hr"], sources: src }],
};

describe("LabourView", () => {
  it("shows department totals and the spike", () => {
    render(<LabourView page={ready} />);
    expect(screen.getByRole("table", { name: "Latest week by department" }).textContent).toContain("168.00");
    expect(screen.getByRole("list", { name: "Overtime spikes" }).textContent).toContain("100% over");
  });
  it("a reader without the grant sees only the note", () => {
    render(<LabourView page={{ ...ready, state: "not_granted", note: "Labour figures need the hr or ops-management grant.", weeks: [], trend: [], alerts: [] }} />);
    expect(screen.getByTestId("labour-note").textContent).toContain("hr or ops-management");
    expect(screen.queryByRole("table")).toBeNull();
  });
});
