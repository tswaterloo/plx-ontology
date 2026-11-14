import { render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApprovalsQueue, AutopilotArea, Me, WriteIntentView } from "@/lib/types";
import type { OpsAlarms } from "@/lib/api";

const api = vi.hoisted(() => ({ owner: true as boolean | null, failWrites: false }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getMe: () => Promise.resolve(me),
  getAutopilot: () => (api.owner === null ? Promise.reject(new Error("503")) : Promise.resolve({ ...area, owner: api.owner })),
  getWrites: () => (api.failWrites ? Promise.reject(new Error("503")) : Promise.resolve({ items: writes })),
  getApprovals: () => Promise.resolve(approvals),
  getOpsAlarms: () => Promise.resolve(alarms),
}));

import { NeedsMe, NeedsMeList, needsOf, type NeedsSources } from "./needs-me";

const now = new Date("2026-11-11T15:00:00Z");
const h = (n: number) => new Date(now.getTime() + n * 3_600_000).toISOString();
const me = { display_name: "Pat Owner", email: "pat@plx.example", principal: "p", auth: "mock", data_label: "", dev_mode: true } as Me;
const item = (id: string, due: number, extra: Partial<AutopilotArea["items"] & object> = {}) =>
  ({ item_id: id, autopilot: "po_followup", label: "PO follow-up", title: `Item ${id}`, mine: true, state: "waiting", due_at: h(due), created_at: h(-5), owner: "o", system: "Mail", ...extra }) as unknown as NonNullable<AutopilotArea["items"]>[number];
const area = {
  owner: true,
  switches: {},
  items: [item("late", 5), item("soon", 1), item("theirs", 0, { mine: false } as never), item("done", 0, { state: "accepted" } as never)],
} as unknown as AutopilotArea;
const writes = [
  { intent: { intent_id: "w1", record_id: "PurchaseOrder:demo:90412" }, status: "prepared", text: "Set PO 90412 expected date to 18 Nov", at: h(-2), replayed: false },
  { intent: { intent_id: "w2", record_id: "x" }, status: "failed", text: "Note on vendor Ridgeway", at: h(-30), replayed: false },
  { intent: { intent_id: "w3" }, status: "written", text: "Done already", at: h(-1), replayed: false },
] as unknown as WriteIntentView[];
const approvals = {
  as_of: "x", sent: 0, writes: 0, total: 3,
  groups: [
    { waiting_on: "pat@plx.example", count: 1, oldest_days: 2, items: [{ id: "a1", kind: "credit_hold", summary: "Release Harbor's credit hold", days: 2, aging: "", channel: "mc", nudge: {} as never, asked_by: "Ana" }] },
    { waiting_on: "Sam", count: 2, oldest_days: 6, items: [] },
    { waiting_on: "Lee", count: 1, oldest_days: 1, items: [] },
  ],
} as unknown as ApprovalsQueue;
const alarms = { configured: true, running: true, open: [{ key: "unit:x", line: "plx-proactive.service: killed: out of memory", opened_at: h(-1) }], recovered: [], checks: [] } as OpsAlarms;
const all: NeedsSources = { me, autopilot: area, writes, approvals, alarms };

describe("Needs you (owner)", () => {
  it("lists decisions, approvals and blocked items in that order, each pointing to where it is done", () => {
    const { needs, unread } = needsOf(all, now);
    expect(unread).toEqual([]);
    expect(needs.map((n) => `${n.kind}:${n.key}`)).toEqual([
      "decide:ap:soon",
      "decide:ap:late",
      "approve:a:a1",
      "approve:w:w1",
      "blocked:ag:Sam",
      "blocked:w:w2",
      "blocked:o:unit:x",
    ]);
    const by = Object.fromEntries(needs.map((n) => [n.key, n]));
    expect(by["ap:soon"].href).toBe("/autopilot?item=soon");
    expect(by["w:w1"].href).toBe("/object/PurchaseOrder/PurchaseOrder%3Ademo%3A90412");
    expect(by["w:w2"].href).toBeNull();
    expect(by["ag:Sam"].title).toBe("2 approvals waiting on Sam");
    expect(by["o:unit:x"].href).toBe("/ops");
  });

  it("names a source it couldn't read instead of counting it as nothing", () => {
    render(<NeedsMeList sources={{ ...all, writes: null, approvals: null }} />);
    expect(screen.getByTestId("needs-me-unread")).toHaveTextContent("Couldn't read prepared write entries, the approvals queue");
    expect(screen.queryByText("No decision, approval or blocked item waits on you.")).toBeNull();
  });

  it("says plainly when nothing waits, without a row of zero counts", () => {
    render(<NeedsMeList sources={{ me, autopilot: { ...area, items: [] }, writes: [], approvals: { ...approvals, groups: [] }, alarms: null }} />);
    expect(screen.queryByTestId("needs-me-counts")).toBeNull();
    expect(screen.getByText("No decision, approval or blocked item waits on you.")).toBeInTheDocument();
  });

  it("caps a kind at four rows with the rest one click away", async () => {
    const many = { ...area, items: Array.from({ length: 6 }, (_, i) => item(`i${i}`, i)) } as AutopilotArea;
    render(<NeedsMeList sources={{ ...all, autopilot: many }} />);
    const decide = screen.getByTestId("needs-me-decide");
    expect(screen.getByTestId("needs-me-counts")).toHaveTextContent(/^6 to decide · \d+ to approve · \d+ to unblock$/);
    expect(within(decide).getAllByRole("listitem")).toHaveLength(4);
    within(decide).getByRole("button", { name: "2 more" }).click();
    await waitFor(() => expect(within(screen.getByTestId("needs-me-decide")).getAllByRole("listitem")).toHaveLength(6));
  });

  it("shows nothing to a reader the server doesn't call the owner, or when that can't be read", async () => {
    for (const owner of [false, null]) {
      api.owner = owner;
      const { container, unmount } = render(<NeedsMe />);
      await new Promise((r) => setTimeout(r, 20));
      expect(container).toBeEmptyDOMElement();
      unmount();
    }
    api.owner = true;
    render(<NeedsMe />);
    expect(await screen.findByTestId("needs-me")).toHaveTextContent("Release Harbor's credit hold");
  });
});
