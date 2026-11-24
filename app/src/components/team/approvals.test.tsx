import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { forgetKeptReads, keepRead } from "@/lib/use-read";
import type { ApprovalsQueue } from "@/lib/types";

const queue = vi.hoisted(() => ({ value: null as unknown }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getApprovals: () => Promise.resolve(queue.value),
}));

import { TeamApprovals } from "./approvals";

const item = (over: Record<string, unknown>) => ({
  id: "a1", kind: "approval_waiting", channel: "mission_control", summary: "Release PO 90412 for payment", family: "purchase_approval",
  asked_by: "Dana", waiting_on: [], days: 1, aging: "0-2 business days", object_id: null, citations: [],
  nudge: { body: "Hi, could you look at PO 90412?\nThanks", channel: "email" as const, subject: "PO 90412", sent: false as const, status: "draft_not_sent" as const },
  card_id: null, offers: [], ...over,
});

beforeEach(() => forgetKeptReads());

describe("approvals waiting on others", () => {
  it("a queue drawn from the store says how old it is while the read behind it returns", async () => {
    const old = { as_of: "2026-11-09", total: 1, sent: 0, writes: 0, by_aging: {}, groups: [{ waiting_on: "Vince", count: 1, oldest_days: 7, items: [item({})] }] } satisfies ApprovalsQueue;
    keepRead("/api/approvals", old, true, 100, Date.now() - 600_000);
    queue.value = old;
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const orig = queue.value;
    queue.value = gate.then(() => orig);
    render(<TeamApprovals />);
    expect(screen.getByTestId("approvals-queue")).toBeInTheDocument(); // drawn in the first render
    expect(screen.getByText(/reading it again/i)).toBeInTheDocument();
    release();
    await screen.findByTestId("approvals-queue");
    await vi.waitFor(() => expect(screen.queryByText(/reading it again/i)).toBeNull());
  });

  it("reads as words: who it waits on, days in the singular when one, where it was asked, the draft marked unsent", async () => {
    queue.value = {
      as_of: "2026-11-09", total: 2, sent: 0, writes: 0, by_aging: { "0-2 business days": 1, "6-10 business days": 1 },
      groups: [
        { waiting_on: "not recorded", count: 1, oldest_days: 1, items: [item({})] },
        { waiting_on: "Vince", count: 1, oldest_days: 7, items: [item({ id: "a2", channel: "mail", days: 7, aging: "6-10 business days", nudge: { body: "", channel: "email" as const, subject: "", sent: false as const, status: "draft_not_sent" as const } })] },
      ],
    } satisfies ApprovalsQueue;
    render(<TeamApprovals />);
    const first = await screen.findByRole("region", { name: "Waiting on an approver not on record" });
    expect(within(first).getByRole("heading", { level: 3 })).toHaveTextContent("Waiting on an approver not on record · 1 item · oldest 1 business day");
    const [a, b] = screen.getAllByTestId("approval-item");
    expect(a).toHaveTextContent("1 business day · Mission Control · purchase approval · asked by Dana");
    expect(a).toHaveTextContent("Draft nudge, not sent");
    expect(a.textContent).not.toMatch(/mission_control|not recorded/);
    expect(b).toHaveTextContent("7 business days · Email");
    expect(b).toHaveTextContent("No draft nudge.");
    expect(screen.getByRole("tab", { name: /^0-2 business days/ })).toBeInTheDocument();
    expect(document.querySelector("pre")).toBeNull();
  });

  it("opens with the desk at a glance: total, the oldest ask and on whom, how many are a week old, where they were asked", async () => {
    queue.value = {
      as_of: "2026-11-09", total: 3, sent: 0, writes: 0, by_aging: { "0-2 business days": 1, "6-10 business days": 2 },
      groups: [
        { waiting_on: "Vince", count: 2, oldest_days: 7, items: [item({ id: "a2", channel: "mail", days: 7, object_id: "Email:m1", citations: ["c1", "c2"] }), item({ id: "a3", channel: "teams", days: 6, waiting_on: ["Vince", "Stephen"] })] },
        { waiting_on: "not recorded", count: 1, oldest_days: 1, items: [item({})] },
      ],
    } satisfies ApprovalsQueue;
    render(<TeamApprovals />);
    const glance = await screen.findByTestId("approvals-summary");
    expect(glance).toHaveTextContent("Waiting3on 1 person and an unnamed approver");
    expect(glance).toHaveTextContent("Oldest7 dwaiting on Vince");
    expect(glance).toHaveTextContent("5 business days or more2");
    expect(glance).toHaveTextContent("3 nudges drafted, none sent");
    expect(glance).toHaveTextContent(/Email\s*1/);
    expect(glance).toHaveTextContent(/Teams\s*1/);
    expect(glance).toHaveTextContent(/Mission Control\s*1/);
    const [old, both] = screen.getAllByTestId("approval-item");
    expect(within(old).getByTestId("approval-line")).toHaveTextContent("7 business days · Email · purchase approval · asked by Dana · 2 sources ·");
    expect(within(old).getByRole("link", { name: "Open the message" })).toHaveAttribute("href", "/object/Email/Email%3Am1");
    expect(within(both).getByTestId("approval-line")).toHaveTextContent("also waits on Stephen");
  });

  it("says plainly when nothing waits", async () => {
    queue.value = { as_of: "2026-11-09", total: 0, sent: 0, writes: 0, by_aging: {}, groups: [] } satisfies ApprovalsQueue;
    render(<TeamApprovals />);
    expect(await screen.findByText("No approvals waiting.")).toBeInTheDocument();
    expect(screen.queryByTestId("approvals-summary")).toBeNull();
  });
});
