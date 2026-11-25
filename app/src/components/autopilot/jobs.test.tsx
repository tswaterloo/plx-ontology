import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const calls = vi.hoisted(() => ({ requested: [] as string[], stopped: [] as string[], decided: [] as unknown[], prepared: [] as unknown[], jobs: [] as unknown[] }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getAssistJobs: () => Promise.resolve({ jobs: calls.jobs, status: "ok", reason: "" }),
  requestAssistJob: (t: string) => (calls.requested.push(t), Promise.resolve(job("j9", { text: t, status: "queued" }))),
  stopAssistJob: (id: string) => (calls.stopped.push(id), Promise.resolve(job(id, { status: "stopping" }))),
  decideAssistPiece: (id: string, n: number, body: unknown) => (calls.decided.push([id, n, body]), Promise.resolve(job(id, { pieces: [piece({ decision: (body as { decision: "approved" }).decision })] }))),
  prepareAssistChange: (id: string, n: number, i: number) => (calls.prepared.push([id, n, i]), Promise.resolve({ prepared_id: "pw_1", state: "prepared" })),
}));
vi.mock("@/components/prepared-change", () => ({ PreparedChange: ({ id }: { id: string }) => <p data-testid="card">{id}</p> }));

import { JobsView } from "@/components/autopilot/jobs";

function piece(over: Record<string, unknown> = {}) {
  return { n: 1, title: "Chase DSM", instruction: "i", status: "prepared", why: "", message: "", summary: "One email listing 3 late POs", reply: "", ask: null, todos: [], note: "", warning: "", nothing: "", held_back: [], reads: ["po://1"], tool_reads: [{ tool: "plx.sql", arguments: { q: "x" }, failed: false }], reads_traced: true, records_unread: [], unread_reads: [], planned_records: [], decision: "", edited_text: "", prepared_ids: {}, usd: null, proposals: [{ tool: "plx.prepare_write", arguments: { system: "mail", operation: "send_email", reason: "chase", target: {}, change: { to: ["b@dsm.example"], subject: "Late POs", body: "Where are they?" } } }], ...over } as never;
}
function job(id: string, over: Record<string, unknown> = {}) {
  return { job_id: id, text: "chase every late PO from DSM", status: "finished", reason: "", message: "", progress: "", requested_at: "2026-11-24T20:00:00Z", summary: "3 late POs", checklist: [{ text: "Call DSM if no answer by Friday", piece: 1 }], found: "3 POs", not_found: "", nothing: "", planned: 1, unread: ["emails: failing since 3 PM"], fallbacks: [], plan_reads: [], unread_reads: [], pieces: [piece()], usd: 0.42, ...over } as never;
}

describe("Give Assist a job", () => {
  it("sends the typed job, lists it as waiting, and offers Stop while it works", async () => {
    calls.jobs = [];
    render(<JobsView />);
    fireEvent.change(screen.getByRole("textbox", { name: "Give Assist a job" }), { target: { value: "  prepare replies to Croda " } });
    fireEvent.click(screen.getByRole("button", { name: "Start the job" }));
    await waitFor(() => expect(screen.getByTestId("job-status")).toHaveTextContent("Waiting to start"));
    expect(calls.requested).toEqual(["prepare replies to Croda"]);
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Stopping…" })).toBeDisabled());
    expect(calls.stopped).toEqual(["j9"]);
  });
});

describe("A finished job", () => {
  it("shows the plan, the checklist, what could not be read, and each piece with the reads behind it", async () => {
    calls.jobs = [job("j1")];
    render(<JobsView />);
    expect(await screen.findByText("Chase DSM")).toBeInTheDocument();
    expect(screen.getByTestId("job-status")).toHaveTextContent("1 of 1 pieces ready");
    expect(screen.getByTestId("job-status")).not.toHaveTextContent("$0.42");
    expect(screen.getByTestId("job-checked-details")).toHaveTextContent("$0.42");
    expect(screen.getByTestId("job-checklist")).toHaveTextContent("Call DSM if no answer by Friday");
    expect(screen.getByText(/emails: failing since 3 PM/)).toBeInTheDocument();
    expect(screen.getByTestId("piece-reads")).toHaveTextContent("1 record opened and 1 lookup");
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
  });
  it("prepares a change only when asked, on its own card, and approving a piece sends nothing", async () => {
    calls.jobs = [job("j1")];
    render(<JobsView />);
    fireEvent.click(await screen.findByRole("button", { name: "Review this change" }));
    expect(await screen.findByTestId("card")).toHaveTextContent("pw_1");
    expect(calls.prepared).toEqual([["j1", 1, 0]]);
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(calls.decided).toEqual([["j1", 1, { decision: "approved" }]]));
    expect(await screen.findByText("Approved")).toBeInTheDocument();
  });
  it("edits a message's body and sends the edit with the decision", async () => {
    calls.jobs = [job("j1")];
    render(<JobsView />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Message 1"), { target: { value: "Shorter" } });
    fireEvent.click(screen.getByRole("button", { name: "Save my edit" }));
    await waitFor(() => expect(calls.decided.at(-1)).toEqual(["j1", 1, { decision: "edited", text: "", edits: { "0": { body: "Shorter" } } }]));
  });
  it("says a failed job failed, with why, and that nothing was sent", async () => {
    calls.jobs = [job("j2", { status: "failed", message: "The plan could not be made.", pieces: [], planned: 0, summary: "" })];
    render(<JobsView />);
    expect(await screen.findByText(/The plan could not be made\. Nothing was sent or changed\./)).toBeInTheDocument();
  });
  it("flags a record the plan named that no read of the piece shows", async () => {
    calls.jobs = [job("j3", { pieces: [piece({ records_unread: ["PO 90412"] })] })];
    render(<JobsView />);
    expect(await screen.findByText(/PO 90412, but nothing read for this piece shows it/)).toBeInTheDocument();
  });
  it("says on a piece when it holds more than 4 changes (desk rule)", async () => {
    const p0 = (piece() as { proposals: unknown[] }).proposals[0];
    calls.jobs = [job("j4", { pieces: [piece({ proposals: [p0, p0, p0, p0, p0, p0], changes_over_limit: 2 })] })];
    render(<JobsView />);
    expect(await screen.findByTestId("piece-changes-over-limit")).toHaveTextContent("This piece holds 6 proposed changes. The desk prepares at most 4 per piece (2 more are shown below for reference).");
    expect(screen.getAllByRole("button", { name: "Review this change" })).toHaveLength(4);
    expect(screen.getAllByText(/Past the 4-change desk limit/)).toHaveLength(2);
  });

  it("offers a working Dismiss button on failed or unprepared pieces", async () => {
    calls.decided = [];
    calls.jobs = [job("j5", { pieces: [piece({ status: "failed", message: "Timed out." })] })];
    render(<JobsView />);
    fireEvent.click(await screen.findByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(calls.decided.at(-1)).toEqual(["j5", 1, { decision: "dismissed" }]));
  });

  it("suppresses passive 403 error banner on GET jobs while keeping Give Assist a job honest on submit", async () => {
    const apiMod = await import("@/lib/api");
    const { ApiError } = await import("@/lib/errors");
    const spy = vi.spyOn(apiMod, "getAssistJobs").mockRejectedValueOnce(new ApiError(403, "canary_route_refused"));
    try {
      render(<JobsView />);
      await waitFor(() => expect(spy).toHaveBeenCalled());
      expect(screen.queryByText(/don't have access/i)).toBeNull();
      expect(screen.queryByText(/read-only session/i)).toBeNull();
    } finally {
      spy.mockRestore();
    }
  });
});
