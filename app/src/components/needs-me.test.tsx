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
    { waiting_on: "not recorded", count: 1, oldest_days: 4, items: [{ id: "a9", kind: "x", summary: "Discount above 15%", days: 4, aging: "", channel: "mail", nudge: { body: "Hi" } as never }] },
  ],
} as unknown as ApprovalsQueue;
const alarms = { configured: true, running: true, open: [{ key: "unit:x", line: "plx-proactive.service: killed: out of memory", opened_at: h(-1) }], recovered: [], checks: [] } as OpsAlarms;
const all: NeedsSources = { me, autopilot: area, writes, approvals };

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
      "blocked:ag:not recorded",
      "blocked:w:w2",
    ]);
    const by = Object.fromEntries(needs.map((n) => [n.key, n]));
    expect(by["ap:soon"].href).toBe("/autopilot?item=soon");
    expect(by["w:w1"].href).toBe("/object/PurchaseOrder/PurchaseOrder%3Ademo%3A90412");
    expect(by["w:w2"].href).toBeNull();
    expect(by["ag:Sam"].title).toBe("2 approvals waiting on Sam");
    expect(by["ag:Sam"].detail).toBe("oldest 6 days; no nudge drafted yet");
    expect(by["ag:Sam"].href).toBe("/team#approvals");
    expect(by["ag:not recorded"].title).toBe("1 approval waiting on an approver not on record");
    expect(by["ag:not recorded"].detail).toBe("oldest 4 days; a nudge is drafted on Team");
  });

  it("names a source it couldn't read instead of counting it as nothing", () => {
    render(<NeedsMeList sources={{ ...all, writes: null, approvals: null }} />);
    expect(screen.getByTestId("needs-me-unread")).toHaveTextContent("Couldn't read prepared write entries, the approvals queue");
    expect(screen.queryByText("No decision, approval or blocked item waits on you.")).toBeNull();
  });

  it("draws nothing when every source was read and nothing waits, no heading and no zero counts", () => {
    const { container } = render(<NeedsMeList sources={{ me, autopilot: { ...area, items: [] }, writes: [], approvals: { ...approvals, groups: [] } }} />);
    expect(container).toBeEmptyDOMElement();
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

  it("never lists a reliability alarm, even for an owner who can read them", async () => {
    api.owner = true;
    render(<NeedsMe />);
    const needs = await screen.findByTestId("needs-me");
    expect(needs).not.toHaveTextContent("out of memory");
    expect(needs.querySelector('a[href="/ops"]')).toBeNull();
  });
});
