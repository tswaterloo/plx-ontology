import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
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

describe("approvals waiting on others", () => {
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
});
