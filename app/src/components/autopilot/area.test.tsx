import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AutopilotArea, AutopilotItem, AutopilotWorkflowDetail } from "@/lib/types";

const api = vi.hoisted(() => ({
  decide: vi.fn(),
  mode: vi.fn(),
  global: vi.fn(),
  audit: vi.fn(),
  undo: vi.fn(),
  catalog: vi.fn(),
  detail: vi.fn(),
  schedules: vi.fn(),
  draft: vi.fn(),
  replay: vi.fn(),
}));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  decideAutopilotItem: api.decide,
  setAutopilotMode: api.mode,
  setAutopilotGlobal: api.global,
  getAutopilotAudit: api.audit,
  undoAutopilotAction: api.undo,
  getAutopilotCatalog: api.catalog,
  getAutopilotWorkflow: api.detail,
  getAutopilotSchedules: api.schedules,
  draftAutopilotWorkflow: api.draft,
  replayAutopilotWorkflow: api.replay,
}));

import { outcomeWords } from "@/lib/autopilot";
import { AutopilotView } from "./area";

const ME = "33333333-3333-4333-8333-333333333333";
const SAM = "55555555-5555-4555-8555-555555555555";

const item = (over: Partial<AutopilotItem>): AutopilotItem => ({
  item_id: "i1", autopilot: "customer_po", label: "Customer PO received", title: "Customer PO received: PO P-1",
  workflow: "customer_order_intake", system: "bc", roles: ["customer_service"], owner: ME, mine: true,
  happened: { sender: "buyer@customer.example", subject: "PO P-1", object_id: "Email:x:1" },
  prepared: { summary: "New PO P-1: 1 of 1 lines resolved", draft: { channel: "email", to: [{ address: "buyer@customer.example" }], subject: "RE: PO P-1", body: "Thanks, we have PO P-1." },
    write: { system: "bc", operation: "sales_order_create", reason: "Customer PO received" } },
  citations: ["graph-mail://x/m1@1", "bc://co/salesOrders/1"], due_at: "2026-11-10T17:00:00Z", created_at: "2026-11-10T15:00:00Z",
  today: true, state: "waiting", decision: null, ...over,
});

const area = (over: Partial<AutopilotArea> = {}): AutopilotArea => ({
  owner: true,
  switches: { global_off: false, master_switch: false, autopilot_off: false, workflows_off: [] },
  people: [{ id: ME, name: "Ana", you: true, roles: ["customer_service"] }, { id: SAM, name: "", you: false, roles: ["receivables"] }],
  roles: ["customer_service", "receivables"],
  items: [
    item({}),
    item({ item_id: "i2", title: "Payment question from a customer", workflow: "ar_collections", system: "mail", roles: ["receivables"], owner: SAM, mine: false, prepared: { summary: "Invoice 12 is 40 days past due" } }),
    item({ item_id: "i3", title: "Teams question", workflow: "teams_chat", system: "teams", roles: [], today: false, prepared: { draft: { channel: "teams", body: "On it." } } }),
  ],
  workflows: [
    { role: "person:ana@plx.example", person: ME, workflow: "customer_order_intake", dev: { judged: 40, matched: 37, percent: 92.5 }, fresh: { judged: 16, matched: 15, percent: 93.8 }, n: 16, state: "passed_awaiting_owner", on: true, live_flag: false, missing: ["owner_flag_off"], last_run: "2026-11-10T15:00:00Z", scored_at: null, open_items: 1 },
    { role: "receivables", person: null, workflow: "ar_collections", dev: { judged: 20, matched: 15, percent: 75 }, fresh: null, n: 20, state: "dry_run", on: true, live_flag: false, missing: ["gate_not_passed"], last_run: null, scored_at: null, open_items: 1 },
  ],
  notes: [],
  ...over,
});

const NO_BUILDER = { triggers: [], conditions: [], actions: [], builder: false };
const rows = () => screen.getAllByTestId("autopilot-row").map((r) => r.querySelector("[data-queue-row]")!.getAttribute("aria-label"));
const detail = () => screen.getByTestId("autopilot-item");

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset());
  api.catalog.mockResolvedValue(NO_BUILDER);
  api.schedules.mockResolvedValue({ schedules: [], notes: [] });
});

describe("autopilot studio: review queue", () => {
  it("an empty page says no workflow passed its gate and the Workflow filter still offers every workflow", () => {
    const none = "No workflow has passed its gate yet. Items appear here once one passes its replay check.";
    render(<AutopilotView area={area({ items: [], workflows: [], people: [], notes: [none], known_workflows: ["ar_collections", "customer_order_intake"] })} onReload={() => {}} />);
    expect(screen.getByText("Nothing prepared yet.")).toBeTruthy();
    expect(screen.getByText(none)).toBeTruthy();
    const pick = screen.getByLabelText("Workflow") as HTMLSelectElement;
    expect(Array.from(pick.options).map((o) => o.value)).toEqual(["", "ar_collections", "customer_order_intake"]);
    fireEvent.change(pick, { target: { value: "ar_collections" } });
    expect(pick.value).toBe("ar_collections");
    // nothing to choose (no item names a system) -> no System filter at all
    expect(screen.queryByLabelText("System")).toBeNull();
  });

  it("views by person, role, workflow and system, splits by workflow, and opens the selected item", () => {
    render(<AutopilotView area={area()} onReload={() => {}} />);
    // today's open items only: the not-today Teams item waits under "Every open item"
    expect(rows()).toEqual(["Customer PO received: PO P-1", "Payment question from a customer"]);
    expect(detail()).toHaveAttribute("aria-label", "Customer PO received: PO P-1");
    fireEvent.click(screen.getByRole("button", { name: /^Receivables colleague/ }));
    expect(rows()).toEqual(["Payment question from a customer"]);
    expect(detail()).toHaveTextContent("Receivables colleague · AR collections · Mail");
    expect(detail()).toHaveTextContent("view only");
    expect(within(detail()).queryByRole("button", { name: "Accept" })).toBeNull();
    // a view-only item cannot be selected for a bulk decision
    expect(screen.queryByRole("checkbox", { name: /Select Payment question/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Everyone/ }));
    fireEvent.change(screen.getByLabelText("When"), { target: { value: "all" } });
    fireEvent.change(screen.getByLabelText("System"), { target: { value: "teams" } });
    expect(rows()).toHaveLength(1);
    fireEvent.change(screen.getByLabelText("System"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Role"), { target: { value: "customer_service" } });
    expect(rows()).toHaveLength(1);
    fireEvent.change(screen.getByLabelText("Role"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Split by"), { target: { value: "workflow" } });
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["AR collections · 1", "Customer order intake · 1", "Teams chat · 1"]);
    fireEvent.change(screen.getByLabelText("State"), { target: { value: "rejected" } });
    expect(screen.getByTestId("empty-state")).toHaveTextContent("Nothing matches this view.");
    // the evidence is cited and opens the source
    fireEvent.change(screen.getByLabelText("State"), { target: { value: "open" } });
    fireEvent.click(screen.getByRole("button", { name: "Customer PO received: PO P-1" }));
    expect(within(detail()).getByRole("link", { name: "Source 2" })).toHaveAttribute("href", "/source?handle=bc%3A%2F%2Fco%2FsalesOrders%2F1");
  });

  it("accept in dry run records the decision only; an edit is sent with it; keys act on the selected item", async () => {
    api.decide.mockImplementation((id: string, body: { decision: string; text?: string }) =>
      Promise.resolve(item({ item_id: id, state: "accepted", decision: { decision: "accept", at: "2026-11-10T16:00:00Z", by_you: true, text: body.text ?? null, outcome: "dry_run", reasons: ["gate_not_passed"], sent: false } })));
    render(<AutopilotView area={area()} onReload={() => {}} />);
    expect(detail()).toHaveTextContent("Dry run: accept records your decision only. Nothing is sent or written.");
    fireEvent.click(within(detail()).getByRole("button", { name: "Edit" }));
    fireEvent.change(within(detail()).getByRole("textbox"), { target: { value: "Thanks, PO P-1 is in." } });
    fireEvent.click(within(detail()).getByRole("button", { name: "Accept with edit" }));
    expect(api.decide).toHaveBeenCalledWith("i1", { decision: "accept", text: "Thanks, PO P-1 is in." });
    expect(await screen.findByTestId("autopilot-decision")).toHaveTextContent("Dry run: your decision is recorded, nothing was sent or written.");
    // keyboard: focusing a row selects it, r rejects it; nothing acts on a view-only item
    const sam = screen.getByRole("button", { name: "Payment question from a customer" });
    act(() => sam.focus());
    fireEvent.keyDown(sam, { key: "r" });
    expect(api.decide).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByLabelText("When"), { target: { value: "all" } });
    const again = screen.getByRole("button", { name: "Payment question from a customer" });
    act(() => again.focus());
    fireEvent.keyDown(again, { key: "j" });
    expect(document.activeElement).toHaveAttribute("aria-label", "Teams question");
    fireEvent.keyDown(document.activeElement as Element, { key: "r" });
    expect(api.decide).toHaveBeenLastCalledWith("i3", { decision: "reject" });
  });

  it("a live item asks for a confirm before performing: the server answers a token and performs nothing until it comes back", async () => {
    api.decide
      .mockResolvedValueOnce(item({ live: true, live_kinds: ["bc_write"], confirm_token: "tok-1" }))
      .mockResolvedValueOnce(item({ state: "accepted", decision: { decision: "accept", at: "2026-11-10T16:00:00Z", by_you: true, outcome: "done", reasons: [], sent: false } }));
    // live from the server's view only (its kind opened to approved writes; no live workflow row)
    const live = area({ items: area().items!.map((i) => (i.item_id === "i1" ? { ...i, live: true, live_kinds: ["bc_write"] } : i)) });
    render(<AutopilotView area={live} onReload={() => {}} />);
    expect(detail()).toHaveTextContent("Live: accept performs it through the write path after your confirm.");
    fireEvent.click(within(detail()).getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(api.decide).toHaveBeenCalledWith("i1", { decision: "accept" }));
    expect(await within(detail()).findByText("This item is live: accepting performs it in Business Central. Accept again to confirm.")).toBeInTheDocument();
    fireEvent.click(within(detail()).getByRole("button", { name: "Confirm and perform" }));
    await waitFor(() => expect(api.decide).toHaveBeenLastCalledWith("i1", { decision: "accept", confirm_token: "tok-1" }));
    expect(await within(detail()).findByTestId("autopilot-decision")).toHaveTextContent("Done live through the write path.");
  });

  it("a bulk accept leaves live items out and never performs one; bulk reject decides each selected item", async () => {
    api.decide.mockImplementation((id: string, body: { decision: string }) => Promise.resolve(item({ item_id: id, state: body.decision === "accept" ? "accepted" : "rejected", decision: { decision: body.decision as "accept", at: "2026-11-10T16:00:00Z", by_you: true, outcome: "dry_run", reasons: [], sent: false } })));
    // i1 is live from the server's view (no live workflow row): left out of the bulk accept
    const live = area({ items: [...area().items!.map((i) => (i.item_id === "i1" ? { ...i, live: true, live_kinds: ["bc_write"] } : i)), item({ item_id: "i4", title: "Second PO", workflow: "ar_collections", roles: ["receivables"] })] });
    render(<AutopilotView area={live} onReload={() => {}} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "Select every item of yours" }));
    expect(screen.getByText(/1 live item left out of a bulk accept/)).toBeInTheDocument();
    fireEvent.click(within(screen.getByRole("group", { name: "With the selected" })).getByRole("button", { name: "Accept 1" }));
    await waitFor(() => expect(api.decide).toHaveBeenCalledTimes(1));
    expect(api.decide).toHaveBeenCalledWith("i4", { decision: "accept", bulk: true });
    expect(await screen.findByRole("status")).toHaveTextContent("1 item accepted.");
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Customer PO received: PO P-1" }));
    fireEvent.click(within(screen.getByRole("group", { name: "With the selected" })).getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(api.decide).toHaveBeenLastCalledWith("i1", { decision: "reject", bulk: true }));
  });
});

describe("autopilot studio: workflows, modes and the owner's controls", () => {
  it("the passed-workflows figure opens the workflows waiting on the owner", () => {
    render(<AutopilotView area={area()} onReload={vi.fn()} />);
    fireEvent.click(screen.getByTestId("autopilot-passed"));
    expect(screen.getByRole("tab", { name: "Workflows" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getAllByTestId("autopilot-workflow")[0]).toHaveTextContent("Passed, awaiting you");
  });

  it("the owner sets a mode per workflow and role (Auto only after its gate); anyone else sees no controls and no audit log", async () => {
    api.mode.mockResolvedValue({});
    const reload = vi.fn();
    const { unmount } = render(<AutopilotView area={area()} onReload={reload} />);
    fireEvent.click(screen.getByRole("tab", { name: "Workflows" }));
    const [passed, dry] = screen.getAllByTestId("autopilot-workflow");
    expect(passed).toHaveTextContent("Customer order intake");
    expect(passed).toHaveTextContent("93.8%");
    expect(passed).toHaveTextContent("n 16");
    expect(passed).toHaveTextContent("Passed, awaiting you");
    expect(dry).toHaveTextContent("75.0%");
    expect(dry).toHaveTextContent("tuning");
    expect(within(dry).getByRole("radio", { name: "Auto" })).toBeDisabled();
    // without the builder on the server, Suggest and Watch are drawn but closed
    expect(within(passed).getByRole("radio", { name: "Suggest" })).toBeDisabled();
    expect(within(passed).getByRole("radio", { name: "Watch" })).toBeDisabled();
    expect(within(passed).getByRole("radio", { name: "Draft" })).toHaveAttribute("aria-checked", "true");
    fireEvent.click(within(passed).getByRole("radio", { name: "Auto" }));
    expect(api.mode).toHaveBeenCalledWith("customer_order_intake", "auto", "person:ana@plx.example");
    await waitFor(() => expect(reload).toHaveBeenCalled());
    fireEvent.click(within(dry).getByRole("radio", { name: "Off" }));
    expect(api.mode).toHaveBeenLastCalledWith("ar_collections", "off", null);
    expect(screen.getByRole("tab", { name: "Audit log" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Builder" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop all writes" }));
    expect(api.global).not.toHaveBeenCalled();
    unmount();
    render(<AutopilotView area={area({ owner: false })} onReload={() => {}} />);
    expect(screen.queryByRole("tab", { name: "Audit log" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "Builder" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Stop all writes" })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "Workflows" }));
    expect(screen.queryAllByRole("radio")).toHaveLength(0);
  });

  it("Watch is set only after a second confirm, and a workflow's page shows when, if and then with its gate", async () => {
    api.catalog.mockResolvedValue({ ...NO_BUILDER, builder: true });
    api.mode.mockResolvedValue({});
    const detailDoc: AutopilotWorkflowDetail = {
      workflow: "customer_order_intake", label: "Customer PO received", trigger: { kind: "message.received", words: "A new email or Teams message arrives", params: {} },
      conditions: [{ kind: "intents_any", words: "It asks for: customer po", params: {} }], actions: [{ kind: "order_entry", words: "Draft the sales order and a reply", params: {} }],
      autopilots: ["customer_po"], mode: "draft", modes: ["off", "suggest", "draft", "auto", "watch"],
      gate: { percent: 93.8, judged: 16, matched: 15, fresh: true, passed: true, rule: "90% right on at least 15 held-out items" },
      rows: [area().workflows![0]], caught: 15, missed: 1, false_alarms: null, hours_saved: null, hours_basis: "Not measured on this server yet.", open_items: 1, history: [], notes: [],
    };
    api.detail.mockResolvedValue(detailDoc);
    render(<AutopilotView area={area()} onReload={() => {}} />);
    fireEvent.click(screen.getByRole("tab", { name: "Workflows" }));
    const [passed] = screen.getAllByTestId("autopilot-workflow");
    await waitFor(() => expect(within(passed).getByRole("radio", { name: "Watch" })).toBeEnabled());
    fireEvent.click(within(passed).getByRole("radio", { name: "Watch" }));
    expect(api.mode).not.toHaveBeenCalled();
    fireEvent.click(within(passed).getByRole("button", { name: "Confirm Watch" }));
    expect(api.mode).toHaveBeenCalledWith("customer_order_intake", "watch", "person:ana@plx.example");
    fireEvent.click(within(passed).getByRole("button", { name: "Customer order intake" }));
    const sheet = await screen.findByTestId("workflow-sheet");
    await within(sheet).findByText("Customer PO received");
    expect(within(sheet).getByTestId("workflow-steps")).toHaveTextContent("A new email or Teams message arrives");
    expect(within(sheet).getByTestId("workflow-steps")).toHaveTextContent("Draft the sales order and a reply");
    expect(within(sheet).getByTestId("workflow-figures")).toHaveTextContent("93.8%");
    expect(within(sheet).getByTestId("workflow-figures")).toHaveTextContent("Not measured on this server yet.");
  });

  it("Try again in a workflow's page asks the server again and shows the page (WEB-017)", async () => {
    api.catalog.mockResolvedValue({ ...NO_BUILDER, builder: true });
    const doc: AutopilotWorkflowDetail = {
      workflow: "customer_order_intake", label: "Customer PO received", trigger: { kind: "message.received", words: "A new email arrives", params: {} },
      conditions: [], actions: [], autopilots: ["customer_po"], mode: "draft", modes: ["off", "suggest", "draft", "auto", "watch"],
      gate: { percent: 93.8, judged: 16, matched: 15, fresh: true, passed: true, rule: "90% right on at least 15 held-out items" },
      rows: [area().workflows![0]], caught: 15, missed: 1, false_alarms: null, hours_saved: null, hours_basis: "x", open_items: 1, history: [], notes: [],
    };
    api.detail.mockRejectedValueOnce(new Error("down"));
    api.detail.mockResolvedValue(doc);
    render(<AutopilotView area={area()} onReload={() => {}} />);
    fireEvent.click(screen.getByRole("tab", { name: "Workflows" }));
    const [passed] = screen.getAllByTestId("autopilot-workflow");
    fireEvent.click(within(passed).getByRole("button", { name: "Customer order intake" }));
    const sheet = await screen.findByTestId("workflow-sheet");
    fireEvent.click(await within(sheet).findByRole("button", { name: "Try again" }));
    await within(sheet).findByText("Customer PO received");
    expect(api.detail).toHaveBeenCalledTimes(2);
  });

  it("the builder keeps a draft and replays it on past days; it never offers to go live", async () => {
    api.catalog.mockResolvedValue({
      triggers: [{ kind: "record.due", words: "A record comes due", params: {} }],
      conditions: [{ kind: "object_type_in", words: "The record is a", params: {} }],
      actions: [{ kind: "po_followup", words: "Draft a chase to the supplier", params: {} }],
      builder: true,
    });
    api.draft.mockResolvedValue({ workflow: "draft_1", mode: "draft", problems: [] });
    api.replay.mockResolvedValue({ workflow: "draft_1", days: 14, judged: 30, caught: 25, missed: 3, false_alarms: 2, items: [] });
    render(<AutopilotView area={area()} onReload={() => {}} />);
    fireEvent.click(screen.getByRole("tab", { name: "Builder" }));
    const builder = await screen.findByTestId("autopilot-builder");
    const keep = within(builder).getByRole("button", { name: "Keep as draft" });
    expect(keep).toBeDisabled();
    fireEvent.change(within(builder).getByPlaceholderText(/Chase a supplier PO/), { target: { value: "PO chase" } });
    fireEvent.change(within(builder).getByLabelText("Trigger"), { target: { value: "record.due" } });
    fireEvent.click(within(builder).getByLabelText("Draft a chase to the supplier"));
    expect(within(builder).getByRole("button", { name: "Replay" })).toBeDisabled();
    fireEvent.click(keep);
    await waitFor(() => expect(api.draft).toHaveBeenCalled());
    expect(api.draft.mock.calls[0][0]).toMatchObject({ label: "PO chase", trigger: { kind: "record.due" }, actions: [{ kind: "po_followup" }] });
    await waitFor(() => expect(within(builder).getByRole("button", { name: "Replay" })).toBeEnabled());
    fireEvent.click(within(builder).getByRole("button", { name: "Replay" }));
    expect(await within(builder).findByText(/Of 30 judged over 14 days/)).toBeInTheDocument();
    expect(api.replay).toHaveBeenCalledWith("draft_1", 14);
    expect(within(builder).queryByRole("button", { name: /live/i })).toBeNull();
  });
});

describe("Draft mode and the fresh-day countdown", () => {
  const draftRow = {
    role: "person:ana@plx.example", person: ME, workflow: "internal_approval", dev: { judged: 33, matched: 29, percent: 87.9 }, fresh: null, n: 33,
    state: "dry_run" as const, on: true, live_flag: false, missing: ["gate_not_passed"], last_run: null, scored_at: null, open_items: 2, draft: true,
    countdown: { judged: 12, matched: 11, need_judged: 15, need_percent: 90, percent: 91.7, passes: false, window: ["2026-06-08T07:00:00Z", "2026-11-12T07:00:00Z"], data_end: null,
      parts: [{ window: ["2026-06-08T07:00:00Z", "2026-06-15T07:00:00Z"], kind: "historical" as const, judged: 8, matched: 8 },
              { window: ["2026-11-06T07:00:00Z", "2026-11-12T07:00:00Z"], kind: "fresh" as const, judged: 4, matched: 3 }] },
  };

  it("a draft row says Draft with its countdown; the workflow page shows the countdown toward the gate", async () => {
    api.detail.mockResolvedValue({
      workflow: "internal_approval", label: "Approval requested", trigger: null, conditions: [], actions: [], autopilots: ["approval_request"],
      mode: "draft", modes: ["off", "draft", "auto"], gate: { percent: 87.9, judged: 33, matched: 29, fresh: false, passed: false, rule: "90% right on at least 15 held-out items" },
      rows: [draftRow, { ...draftRow, role: "owner", person: null, draft: false, countdown: null }], caught: 29, missed: 4, false_alarms: null, hours_saved: null, hours_basis: null, open_items: 2, history: [], notes: [],
    } satisfies AutopilotWorkflowDetail);
    render(<AutopilotView area={area({ workflows: [draftRow] })} onReload={() => {}} />);
    fireEvent.click(screen.getByRole("tab", { name: "Workflows" }));
    const [row] = screen.getAllByTestId("autopilot-workflow");
    expect(row).toHaveTextContent("Draft");
    expect(within(row).getByTestId("gate-countdown")).toHaveTextContent("12/15 items");
    fireEvent.click(within(row).getByRole("button", { name: /Internal approval/i }));
    const counts = await screen.findAllByTestId("workflow-countdown");
    expect(counts).toHaveLength(1);
    expect(counts[0]).toHaveTextContent("12 of 15 items at ≥90% · 91.7% right so far");
    expect(counts[0]).toHaveTextContent("not yet");
    // the past days never tuned on and the fresh days, each with its own count
    expect(within(counts[0]).getAllByTestId("countdown-part").map((e) => e.textContent?.replace(/ to .*:/, ":"))).toEqual([
      expect.stringMatching(/^Past days never tuned on, .*: 8 of 8 right$/), expect.stringMatching(/^Fresh days .*: 3 of 4 right$/)]);
    expect(screen.getByTestId("workflow-sheet")).toHaveTextContent("No held-out items judged yet");
    expect(screen.getByTestId("workflow-sheet")).toHaveTextContent("nothing goes out without accept and confirm");
  });
});

describe("what an accept did (WEB-036)", () => {
  it("never says nothing was sent or written unless nothing ran", () => {
    const said = (o: string, r: string[] = []) => outcomeWords(o, r, "Business Central");
    for (const unsure of ["unknown", "pending"]) {
      expect(said(unsure)).toContain("Could not confirm whether it went through in Business Central");
      expect(said(unsure)).not.toMatch(/nothing was sent|nothing was performed/);
    }
    // without steps (never stored by this version) it names no system to check
    expect(said("partly_done", ["http_500"])).toBe(". Partly done: a step went through, another did not (http 500).");
    // with steps: exactly which step ran and which did not; check only an unconfirmed one
    const bc = { kind: "bc_write", system: "bc", status: "done" };
    expect(outcomeWords("partly_done", [], "Business Central", [bc, { kind: "send_mail", system: "mail", status: "unavailable", reasons: ["no_adapter"] }]))
      .toBe(". Partly done: the Business Central write went through; the email never ran (unavailable, no adapter), so it was not sent.");
    expect(outcomeWords("partly_done", [], "Business Central", [bc, { kind: "send_mail", system: "mail", status: "failed", reasons: ["http_500"] }]))
      .toBe(". Partly done: the Business Central write went through; the email failed (http 500).");
    expect(outcomeWords("unknown", [], "Business Central", [bc, { kind: "send_mail", system: "mail", status: "unknown" }]))
      .toBe(". The Business Central write went through; could not confirm whether the email went through. Check Mail before trying again.");
    // an outcome or step this page does not know is never "nothing was sent"
    expect(said("weird")).not.toMatch(/nothing was sent/);
    expect(outcomeWords("unknown", [], "Business Central", [{ kind: "bc_write", system: "bc", status: "weird" }])).toContain("Check Business Central");
    expect(said("failed", ["http_400"])).not.toMatch(/nothing was sent/);
    expect(said("done")).toBe(". Done live through the write path.");
    expect(said("dry_run")).toContain("nothing was sent or written");
    expect(said("refused", ["approver_not_owner"])).toBe(". Recorded; the write path would not perform it (refused, approver not owner), so nothing was sent or written.");
  });
});
