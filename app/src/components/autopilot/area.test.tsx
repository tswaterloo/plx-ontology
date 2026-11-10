import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AutopilotArea, AutopilotItem } from "@/lib/types";

const api = vi.hoisted(() => ({
  decide: vi.fn(),
  workflow: vi.fn(),
  global: vi.fn(),
  audit: vi.fn(),
  undo: vi.fn(),
}));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  decideAutopilotItem: api.decide,
  setAutopilotWorkflow: api.workflow,
  setAutopilotGlobal: api.global,
  getAutopilotAudit: api.audit,
  undoAutopilotAction: api.undo,
}));

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

beforeEach(() => Object.values(api).forEach((f) => f.mockReset()));

describe("autopilot area", () => {
  it("views by person, role, workflow and system, and splits by workflow", () => {
    render(<AutopilotView area={area()} onReload={() => {}} />);
    // today's open items only: the not-today Teams item waits under "Every open item"
    expect(screen.getAllByTestId("autopilot-item").map((li) => li.getAttribute("aria-label"))).toEqual(["Customer PO received: PO P-1", "Payment question from a customer"]);
    fireEvent.click(screen.getByRole("button", { name: /^Receivables colleague/ }));
    const [only] = screen.getAllByTestId("autopilot-item");
    expect(screen.getAllByTestId("autopilot-item")).toHaveLength(1);
    expect(only).toHaveTextContent("Receivables colleague · AR collections · Mail");
    expect(only).toHaveTextContent("view only");
    expect(within(only).queryByRole("button", { name: "Accept" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Everyone/ }));
    fireEvent.change(screen.getByLabelText("When"), { target: { value: "all" } });
    fireEvent.change(screen.getByLabelText("System"), { target: { value: "teams" } });
    expect(screen.getAllByTestId("autopilot-item")).toHaveLength(1);
    fireEvent.change(screen.getByLabelText("System"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Role"), { target: { value: "customer_service" } });
    expect(screen.getAllByTestId("autopilot-item")).toHaveLength(1);
    fireEvent.change(screen.getByLabelText("Role"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Split by"), { target: { value: "workflow" } });
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["AR collections · 1", "Customer order intake · 1", "Teams chat · 1"]);
    fireEvent.change(screen.getByLabelText("State"), { target: { value: "rejected" } });
    expect(screen.getByTestId("empty-state")).toHaveTextContent("Nothing matches this view.");
    // the evidence is cited and opens the source
    fireEvent.change(screen.getByLabelText("State"), { target: { value: "open" } });
    expect(screen.getAllByRole("link", { name: "Source 2" })[0]).toHaveAttribute("href", "/source?handle=bc%3A%2F%2Fco%2FsalesOrders%2F1");
  });

  it("accept in dry run records the decision only; an edit is sent with it; keys act on the focused item", async () => {
    api.decide.mockImplementation((id: string, body: { decision: string; text?: string }) =>
      Promise.resolve(item({ item_id: id, state: "accepted", decision: { decision: "accept", at: "2026-11-10T16:00:00Z", by_you: true, text: body.text ?? null, outcome: "dry_run", reasons: ["gate_not_passed"] } })));
    render(<AutopilotView area={area()} onReload={() => {}} />);
    const first = screen.getAllByTestId("autopilot-item")[0];
    expect(first).toHaveTextContent("Dry run: accept records your decision only. Nothing is sent or written.");
    fireEvent.click(within(first).getByRole("button", { name: "Edit" }));
    fireEvent.change(within(first).getByRole("textbox"), { target: { value: "Thanks, PO P-1 is in." } });
    fireEvent.click(within(first).getByRole("button", { name: "Accept with edit" }));
    expect(api.decide).toHaveBeenCalledWith("i1", { decision: "accept", text: "Thanks, PO P-1 is in." });
    expect(await screen.findByTestId("autopilot-decision")).toHaveTextContent("Dry run: your decision is recorded, nothing was sent or written.");
    // keyboard: the item takes focus, r rejects it; nothing acts on a view-only item
    const second = screen.getAllByTestId("autopilot-item")[1];
    second.focus();
    fireEvent.keyDown(second, { key: "r" });
    expect(api.decide).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByLabelText("When"), { target: { value: "all" } });
    const [, sam] = screen.getAllByTestId("autopilot-item");
    sam.focus();
    fireEvent.keyDown(sam, { key: "j" });
    expect(document.activeElement).toHaveAttribute("aria-label", "Teams question");
    fireEvent.keyDown(document.activeElement as Element, { key: "r" });
    expect(api.decide).toHaveBeenLastCalledWith("i3", { decision: "reject" });
  });

  it("a live workflow asks for a confirm before performing", async () => {
    api.decide.mockResolvedValue(item({ state: "accepted", decision: { decision: "accept", at: "2026-11-10T16:00:00Z", by_you: true, outcome: "done", reasons: [] } }));
    const live = area();
    live.workflows![0] = { ...live.workflows![0], state: "live", live_flag: true };
    render(<AutopilotView area={live} onReload={() => {}} />);
    const first = screen.getAllByTestId("autopilot-item")[0];
    fireEvent.click(within(first).getByRole("button", { name: "Accept" }));
    expect(api.decide).not.toHaveBeenCalled();
    expect(first).toHaveTextContent("This workflow is live: accepting performs it in Business Central.");
    fireEvent.click(within(first).getByRole("button", { name: "Confirm and perform" }));
    expect(api.decide).toHaveBeenCalledWith("i1", { decision: "accept" });
    expect(await within(first).findByTestId("autopilot-decision")).toHaveTextContent("Done live through the write path.");
  });

  it("the owner sees scores, state and switches; anyone else sees no controls and no audit log", () => {
    api.workflow.mockResolvedValue(area());
    const { unmount } = render(<AutopilotView area={area()} onReload={() => {}} />);
    fireEvent.click(screen.getByRole("tab", { name: "Workflows" }));
    const [passed, dry] = screen.getAllByTestId("autopilot-workflow");
    expect(passed).toHaveTextContent("Customer order intake");
    expect(passed).toHaveTextContent("92.5%");
    expect(passed).toHaveTextContent("93.8%");
    expect(passed).toHaveTextContent("Passed, awaiting you");
    expect(dry).toHaveTextContent("—");
    expect(within(dry).getByRole("switch", { name: /live/ })).toBeDisabled();
    fireEvent.click(within(passed).getByRole("switch", { name: /live for You/ }));
    expect(api.workflow).toHaveBeenCalledWith("customer_order_intake", { live: true, role: "person:ana@plx.example" });
    fireEvent.click(within(dry).getByRole("switch", { name: "AR collections on" }));
    expect(api.workflow).toHaveBeenCalledWith("ar_collections", { on: false });
    expect(screen.getByRole("tab", { name: "Audit log" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop all writes" }));
    expect(api.global).not.toHaveBeenCalled();
    unmount();
    render(<AutopilotView area={area({ owner: false })} onReload={() => {}} />);
    expect(screen.queryByRole("tab", { name: "Audit log" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Stop all writes" })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "Workflows" }));
    expect(screen.queryAllByRole("switch")).toHaveLength(0);
  });
});
