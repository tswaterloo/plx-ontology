import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AutopilotArea, MyWork, MyWorkItem } from "@/lib/types";

const api = vi.hoisted(() => ({ area: vi.fn(), decide: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getAutopilot: api.area,
  decideAutopilotItem: api.decide,
}));

import { MyWorkList, groupsOf } from "./my-work";

const base: MyWorkItem = {
  item_id: "ap_1", autopilot: "customer_po", label: "Customer PO received", title: "Customer PO received: PO 1 is entered",
  mine: true, happened: { object_id: "Email:x:1" }, citations: ["mail://synth/1"], due_at: "2026-11-08T17:00:00Z",
  created_at: "2026-11-08T15:00:00Z", card_id: "autopilot:ap_1", offers: [], write_ready: true, order_import: false,
  prepared: { summary: "PO 1 is entered as order 7", draft: { body: "Hello, received." }, gaps: ["lines_from_po_document"] },
};
const work = (items: MyWorkItem[]): MyWork => ({ status: "read", sent: 0, writes: 0, digest: { open: items.length, due_today: 0, by_autopilot: {}, nudges: [] }, items });
const apItem = (id: string, workflow: string) => ({
  item_id: id, autopilot: "customer_po", label: "x", title: "x", workflow, system: "bc", roles: [], owner: "me", mine: true, happened: {}, prepared: {}, citations: [],
  due_at: "2026-11-08T17:00:00Z", created_at: "2026-11-08T15:00:00Z", today: true, state: "waiting" as const, decision: null,
});
const area = (liveFlow: string | null): AutopilotArea => ({
  owner: false, switches: { global_off: false, master_switch: true, autopilot_off: false, workflows_off: [] }, people: [], roles: [], notes: [],
  items: [apItem("ap_1", "customer_order_intake"), apItem("ap_3", "supplier_eta_receiving")],
  workflows: liveFlow ? [{ role: "r", person: "me", workflow: liveFlow, dev: null, fresh: { judged: 16, matched: 15, percent: 93.8 }, n: 16, state: "live", on: true, live_flag: true, missing: [], last_run: null, scored_at: null, open_items: 1 }] : [],
});

beforeEach(() => Object.values(api).forEach((f) => f.mockReset()));

describe("my work", () => {
  it("shows the prepared work with approve only on the reader's own items", () => {
    api.area.mockRejectedValue(new Error("offline"));
    render(<MyWorkList work={work([base, { ...base, item_id: "ap_2", mine: false, card_id: null, write_ready: false }])} />);
    expect(screen.getAllByTestId("my-work-item")).toHaveLength(2);
    expect(screen.getAllByTestId("my-work-entry")).toHaveLength(1);
    expect(screen.getByText(/a report's item \(view only\)/)).toBeTruthy();
    expect(screen.getAllByText(/Missing: lines from po document/)).toHaveLength(2);
    // the studio could not be read: no one-click accept at all (fail closed)
    expect(screen.queryByTestId("assist-accept")).toBeNull();
  });

  it("accepts a dry-run item in one click; a live one goes to the review queue for its confirm", async () => {
    api.area.mockResolvedValue(area("supplier_eta_receiving"));
    api.decide.mockResolvedValue({ ...apItem("ap_1", "customer_order_intake"), state: "accepted", decision: { decision: "accept", at: "2026-11-08T16:00:00Z", by_you: true, outcome: "dry_run", reasons: [] } });
    render(<MyWorkList work={work([base, { ...base, item_id: "ap_3", title: "Supplier ETA" }])} />);
    const [dry, live] = screen.getAllByTestId("my-work-item");
    await waitFor(() => expect(within(dry).getByTestId("assist-accept")).toBeInTheDocument());
    expect(within(live).queryByTestId("assist-accept")).toBeNull();
    expect(within(live).getByRole("link", { name: "Review and perform" })).toHaveAttribute("href", "/autopilot");
    fireEvent.click(within(dry).getByTestId("assist-accept"));
    expect(api.decide).toHaveBeenCalledWith("ap_1", { decision: "accept", bulk: true });
    expect(await within(dry).findByRole("status")).toHaveTextContent("Dry run: your yes is recorded, nothing was sent or written.");
  });

  it("offers the order import file only on the reader's own item that has one", () => {
    api.area.mockRejectedValue(new Error("offline"));
    render(<MyWorkList work={work([{ ...base, item_id: "ap/9", order_import: true }, base, { ...base, item_id: "ap_2", mine: false, order_import: true }])} />);
    const links = screen.getAllByTestId("my-work-order-import");
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "/api/my-work/ap%2F9/order-import");
  });

  it("groups past due first, then today, then later", () => {
    const now = new Date("2026-11-08T15:00:00Z");
    const g = groupsOf([{ ...base, item_id: "later", due_at: "2026-11-10T15:00:00Z" }, { ...base, item_id: "past", due_at: "2026-11-08T14:00:00Z" }, { ...base, item_id: "today", due_at: "2026-11-08T20:00:00Z" }], now);
    expect(g.map((x) => [x.label, x.items.map((i) => i.item_id)])).toEqual([["Past due", ["past"]], ["Due today", ["today"]], ["Later", ["later"]]]);
  });

  it("says when the autopilot is switched off", () => {
    api.area.mockRejectedValue(new Error("offline"));
    render(<MyWorkList work={{ status: "off", sent: 0, writes: 0, items: [], digest: { open: 0, due_today: 0, by_autopilot: {}, nudges: [] } }} />);
    expect(screen.getByText(/switched off/)).toBeTruthy();
  });
});
