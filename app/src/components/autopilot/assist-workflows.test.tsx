import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  workflows: [] as unknown[],
  routines: [] as unknown[],
  templates: [] as unknown[],
  created: [] as unknown[],
  updated: [] as { id: string; body: unknown }[],
  paused: [] as string[],
  resumed: [] as string[],
  stopped: [] as string[],
  deleted: [] as string[],
  ran: [] as string[],
  failDelete: false,
}));

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getAssistWorkflows: () => Promise.resolve({ workflows: state.workflows, routines: state.routines, templates: state.templates, status: "ok", reason: "" }),
  parseAssistWorkflow: (text: string) => Promise.resolve({ name: text, instruction: "Send me late POs by supplier", trigger_kind: "schedule", trigger: { cadence: "weekly", weekday: "monday", hour: 8, minute: 0 }, trigger_summary: "Every Monday at 8:00 AM ET" }),
  createAssistWorkflow: (body: unknown) => (state.created.push(body), Promise.resolve(wf("w_new", { name: "Every Monday at 8:00 AM ET: Send me late POs by supplier" }))),
  updateAssistWorkflow: (id: string, body: Record<string, unknown>) => (
    state.updated.push({ id, body }),
    Promise.resolve(wf(id, {
      name: String(body.name ?? "Weekly late POs"),
      instruction: String(body.instruction ?? "Send me late POs by supplier"),
      trigger_kind: (body.trigger_kind as string) ?? "schedule",
      trigger: (body.trigger as Record<string, unknown>) ?? { cadence: "weekly", weekday: "tuesday", hour: 9, minute: 0 },
      trigger_summary: "Every Tuesday at 9:00 AM ET",
    }))
  ),
  pauseAssistWorkflow: (id: string) => (state.paused.push(id), Promise.resolve(wf(id, { status: "paused", pause_reason: "paused_by_person" }))),
  resumeAssistWorkflow: (id: string) => (state.resumed.push(id), Promise.resolve(wf(id, { status: "active", pause_reason: "" }))),
  stopAssistWorkflow: (id: string) => (state.stopped.push(id), Promise.resolve(wf(id, { status: "stopped", pause_reason: "stopped_by_person" }))),
  deleteAssistWorkflow: (id: string) => {
    state.deleted.push(id);
    if (state.failDelete) return Promise.reject(new Error("Could not delete right now."));
    return Promise.resolve(wf(id, { status: "stopped", pause_reason: "deleted_by_person" }));
  },
  runAssistWorkflow: (id: string) => (state.ran.push(id), Promise.resolve({ workflow: wf(id, { runs: [{ job_id: "j_run", triggered_at: "2026-11-24T20:05:00Z", kind: "run_now", trigger_note: "Run on demand", job: job("j_run") }] }), job: job("j_run") })),
}));
vi.mock("@/components/prepared-change", () => ({ PreparedChange: ({ id }: { id: string }) => <p data-testid="card">{id}</p> }));

import { AssistWorkflowsView, MinedRoutinesSection } from "@/components/autopilot/assist-workflows";

function job(id: string) {
  return { job_id: id, text: "Send me late POs by supplier", status: "queued", reason: "", message: "", progress: "", requested_at: "2026-11-24T20:05:00Z", summary: "", checklist: [], found: "", not_found: "", nothing: "", planned: 0, unread: [], fallbacks: [], plan_reads: [], unread_reads: [], pieces: [], usd: null };
}

function wf(id: string, over: Record<string, unknown> = {}) {
  return {
    workflow_id: id, name: "Weekly late POs", instruction: "Send me late POs by supplier",
    trigger_kind: "schedule", trigger: { cadence: "weekly", weekday: "monday", hour: 8, minute: 0 },
    trigger_summary: "Every Monday at 8:00 AM ET", source_kind: "plain_language", source_ref: "",
    status: "active", pause_reason: "", created_at: "2026-11-24T20:00:00Z", updated_at: "2026-11-24T20:00:00Z",
    next_run_at: "2026-11-30T13:00:00Z", last_fired_at: null, consecutive_failures: 0, backoff_until: null,
    last_error: "", runs: [], ...over,
  };
}

describe("Assist workflows", () => {
  it("creates from plain language with preview, from templates, and from mined routines", async () => {
    state.workflows = [];
    state.created = [];
    state.templates = [
      { template_id: "late-pos-weekly", name: "Weekly late POs by supplier", description: "Every Monday 8 AM.", trigger_kind: "schedule", trigger: { cadence: "weekly", weekday: "monday", hour: 8, minute: 0 }, trigger_summary: "Every Monday at 8:00 AM ET", instruction: "List late POs" },
    ];
    state.routines = [
      { routine_id: "r1", kind: "po_followup", description: "Chase open POs", trigger: { relation: "clock", weekday: "Mon" }, mined: { acts: 12, days: 8 }, validated: {}, status: "candidate", suggested_name: "Weekly po followup (Monday)", suggested_trigger_summary: "Every Monday at 8:00 AM ET" },
    ];

    render(<AssistWorkflowsView />);
    const box = await screen.findByLabelText(/Describe when it should run/i);
    fireEvent.change(box, { target: { value: "every Monday 8 AM send me late POs by supplier" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview trigger" }));
    expect(await screen.findByTestId("workflow-parse-preview")).toHaveTextContent("Every Monday at 8:00 AM ET");

    fireEvent.click(screen.getByRole("button", { name: "Save workflow" }));
    await waitFor(() => expect(state.created).toHaveLength(1));
    expect(await screen.findByTestId("assist-workflow")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /Templates/ }));
    fireEvent.click(screen.getByRole("button", { name: "Use template" }));
    await waitFor(() => expect(state.created).toHaveLength(2));

    fireEvent.click(screen.getByRole("tab", { name: /Mined routines/ }));
    fireEvent.click(screen.getByRole("button", { name: "Turn into workflow" }));
    await waitFor(() => expect(state.created).toHaveLength(3));
  });

  it("supports Run now, Pause, Resume, Stop and shows failure backoff clearly", async () => {
    state.workflows = [wf("w1", { consecutive_failures: 2, backoff_until: "2026-11-24T21:00:00Z", last_error: "The plan could not be made." })];
    state.templates = [];
    state.routines = [];
    state.paused = [];
    state.resumed = [];
    state.stopped = [];
    state.ran = [];

    render(<AssistWorkflowsView />);
    expect(await screen.findByTestId("workflow-backoff")).toHaveTextContent("Waiting after 2 failed runs");
    expect(screen.getByTestId("workflow-error")).toHaveTextContent("The plan could not be made.");

    fireEvent.click(screen.getByRole("button", { name: "Run now" }));
    await waitFor(() => expect(state.ran).toEqual(["w1"]));
    expect(await screen.findByTestId("assist-job")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(state.paused).toEqual(["w1"]));
    fireEvent.click(await screen.findByRole("button", { name: "Resume" }));
    await waitFor(() => expect(state.resumed).toEqual(["w1"]));
    fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
    await waitFor(() => expect(state.stopped).toEqual(["w1"]));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Stop" })).toBeNull());
    expect(screen.getByTestId("workflow-toast")).toHaveTextContent("Workflow stopped.");
  });

  it("supports inline Edit and instant optimistic Delete with toast and rollback on error", async () => {
    state.workflows = [wf("w1")];
    state.templates = [];
    state.routines = [];
    state.updated = [];
    state.deleted = [];
    state.failDelete = false;

    render(<AssistWorkflowsView />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const nameInput = screen.getByLabelText("Workflow name");
    const instrInput = screen.getByLabelText("Workflow instruction");
    fireEvent.change(nameInput, { target: { value: "Tuesday late POs" } });
    fireEvent.change(instrInput, { target: { value: "Group late POs by supplier" } });
    fireEvent.change(screen.getByLabelText("Edit schedule weekday"), { target: { value: "tuesday" } });
    fireEvent.change(screen.getByLabelText("Edit schedule hour"), { target: { value: "9" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(state.updated).toHaveLength(1));
    expect(state.updated[0].id).toBe("w1");
    expect(await screen.findByText("Tuesday late POs")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-toast")).toHaveTextContent("Workflow updated.");

    // Delete succeeds optimistically and removes the card
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(screen.queryByTestId("assist-workflow")).toBeNull();
    expect(screen.getByTestId("workflow-toast")).toHaveTextContent("Workflow deleted.");
    await waitFor(() => expect(state.deleted).toEqual(["w1"]));
  });

  it("restores workflow and shows alert when Delete fails", async () => {
    state.workflows = [wf("w_keep")];
    state.templates = [];
    state.routines = [];
    state.deleted = [];
    state.failDelete = true;

    render(<AssistWorkflowsView />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(state.deleted).toEqual(["w_keep"]));
    expect(await screen.findByTestId("assist-workflow")).toBeInTheDocument();
    expect(await screen.findByTestId("workflow-action-error")).toBeInTheDocument();
    state.failDelete = false;
  });

  it("lets a person turn a mined routine into a workflow from the Routines tab", async () => {
    state.workflows = [];
    state.templates = [];
    state.created = [];
    state.routines = [
      { routine_id: "r7", kind: "po_followup", description: "Follow up on late POs", trigger: { relation: "clock", weekday: "Mon" }, mined: { acts: 9, days: 5 }, validated: {}, status: "candidate", suggested_name: "Weekly po followup (Monday)", suggested_trigger_summary: "Every Monday at 8:00 AM ET" },
    ];
    render(<MinedRoutinesSection />);
    fireEvent.click(await screen.findByRole("button", { name: "Turn into workflow" }));
    await waitFor(() => expect(state.created).toEqual([{ routine_id: "r7" }]));
    expect(await screen.findByText(/Workflow created/)).toBeInTheDocument();
  });

  it("lists supported event kinds without Mission Control approvals", async () => {
    state.workflows = [];
    state.templates = [];
    state.routines = [];
    render(<AssistWorkflowsView />);
    const triggerSelect = await screen.findByLabelText("Trigger:");
    fireEvent.change(triggerSelect, { target: { value: "event" } });
    const eventSelect = screen.getByLabelText("Event kind") as HTMLSelectElement;
    const values = Array.from(eventSelect.options).map((o) => o.value);
    expect(values).toEqual(["mail_from", "teams_from", "po_late", "mc_question", "qc_hold"]);
    expect(values).not.toContain("approval_arrived");
  });

  it("hides mined routines quietly on 403 and shows a calm note on Workflows tab for read-only sessions", async () => {
    const apiMod = await import("@/lib/api");
    const { ApiError } = await import("@/lib/errors");
    const spy = vi.spyOn(apiMod, "getAssistWorkflows").mockRejectedValue(new ApiError(403, "canary_route_refused"));
    try {
      const { container, unmount } = render(<MinedRoutinesSection />);
      await waitFor(() => expect(spy).toHaveBeenCalled());
      expect(container.textContent).toBe("");
      unmount();

      render(<AssistWorkflowsView />);
      const note = await screen.findByTestId("assist-workflows-restricted");
      expect(note).toHaveTextContent("read-only session");
    } finally {
      spy.mockRestore();
    }
  });
});
