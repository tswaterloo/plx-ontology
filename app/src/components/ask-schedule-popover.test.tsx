import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AssistWorkflow } from "@/lib/api";

const createAssistWorkflow = vi.fn();
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  createAssistWorkflow: (...args: unknown[]) => createAssistWorkflow(...args),
}));

import { AskSchedulePopover } from "./ask-schedule-popover";

const MOCK_WORKFLOW: AssistWorkflow = {
  workflow_id: "wf-1",
  name: "Which purchase orders are late?",
  instruction: "Which purchase orders are late?",
  trigger_kind: "schedule",
  trigger: { cadence: "weekdays", hour: 8, minute: 0 },
  trigger_summary: "Every weekday at 8:00 AM",
  source_kind: "plain_language",
  source_ref: "",
  status: "active",
  pause_reason: "",
  created_at: "2026-11-25T10:00:00Z",
  updated_at: "2026-11-25T10:00:00Z",
  consecutive_failures: 0,
  last_error: "",
  runs: [],
};

describe("AskSchedulePopover", () => {
  beforeEach(() => {
    createAssistWorkflow.mockReset().mockResolvedValue(MOCK_WORKFLOW);
  });

  it("creates a scheduled Assist workflow with selected cadence and optional delivery note, showing toast link to Workflows", async () => {
    render(<AskSchedulePopover question="Which purchase orders are late?" spaceLabel="Purchasing" />);
    await userEvent.click(screen.getByTestId("schedule-update-trigger"));
    expect(screen.getByTestId("schedule-update-popover")).toBeInTheDocument();

    // Pick Every 6 hours
    await userEvent.click(screen.getByTestId("cadence-every_6h"));
    await userEvent.type(screen.getByLabelText("Delivery note (optional)"), "Flag POs over 10 days late");
    await userEvent.click(screen.getByTestId("schedule-update-submit"));

    await waitFor(() => expect(createAssistWorkflow).toHaveBeenCalledTimes(1));
    expect(createAssistWorkflow).toHaveBeenCalledWith({
      name: "Which purchase orders are late?",
      instruction: "[Purchasing] Which purchase orders are late?\nDelivery note: Flag POs over 10 days late",
      trigger_kind: "schedule",
      trigger: { cadence: "hourly", every_hours: 6 },
    });

    const toast = await screen.findByTestId("schedule-update-toast");
    expect(toast).toHaveTextContent("Scheduled (Every 6 hours)");
    expect(toast.textContent).not.toContain("\u2014");
    const link = screen.getByRole("link", { name: "View in Workflows" });
    expect(link).toHaveAttribute("href", "/assist?show=workflows");
  });

  it("supports Every weekday 8 AM, Every Monday 8 AM, and Daily 8 AM schedules", async () => {
    render(<AskSchedulePopover question="Show monthly revenue" />);
    await userEvent.click(screen.getByTestId("schedule-update-trigger"));
    await userEvent.click(screen.getByTestId("cadence-monday_8am"));
    await userEvent.click(screen.getByTestId("schedule-update-submit"));

    await waitFor(() =>
      expect(createAssistWorkflow).toHaveBeenCalledWith({
        name: "Show monthly revenue",
        instruction: "Show monthly revenue",
        trigger_kind: "schedule",
        trigger: { cadence: "weekly", weekday: "monday", hour: 8, minute: 0 },
      }),
    );
  });

  it("shows a plain error if scheduling fails", async () => {
    createAssistWorkflow.mockRejectedValueOnce(new Error("Workflows are not available here."));
    render(<AskSchedulePopover question="Show monthly revenue" />);
    await userEvent.click(screen.getByTestId("schedule-update-trigger"));
    await userEvent.click(screen.getByTestId("schedule-update-submit"));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});
