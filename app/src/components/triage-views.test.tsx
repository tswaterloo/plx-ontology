import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { LabelChanges, ReplyTriage, Workload } from "@/lib/types";
import { ReplyList } from "./reply-list";
import { WorkloadView } from "./workload-view";
import { LabelChangesView } from "./label-changes";

const draft = { channel: "email" as const, to: [{ address: "buyer@synth.example" }], subject: "Re: Order 1",
  body: "Hello, I am looking into it.", status: "draft_not_sent" as const, sent: false as const };

describe("my replies, workload and label changes", () => {
  it("lists the reader's lines with drafts and marks a new inquiry", () => {
    const triage: ReplyTriage = { as_of: "2026-11-05", total: 2, sent: 0, writes: 0, lines: [
      { id: "pe-1", kind: "reply_owed", channel: "mail", days: 3, chases: 2, subject: "Order 1", draft, rank: [1, 0, 2, 3], new_inquiry: false },
      { id: "pe-2", kind: "reply_owed", channel: "mail", days: 1, chases: 0, subject: "Bottles", draft, rank: [0, 0, 0, 1], new_inquiry: true },
    ] };
    render(<ReplyList triage={triage} />);
    expect(screen.getAllByTestId("reply-line")).toHaveLength(2);
    expect(screen.getByText(/asked 3 times/)).toBeTruthy();
    expect(screen.getByText("New inquiry")).toBeTruthy();
    expect(screen.getByText(/Nothing is sent until you confirm it/)).toBeTruthy();
  });

  it("shows team counts and per-person rows only when served", () => {
    const data: Workload = { teams: [{ team: "SALES", awaiting_reply: 5, tasks: 1, alerts: 2, suppressed: false },
      { team: "OPS", awaiting_reply: null, tasks: 0, alerts: 1, suppressed: true }], people: [], granted_teams: [] };
    const { rerender } = render(<WorkloadView data={data} />);
    expect(screen.getByTestId("workload-teams")).toBeTruthy();
    expect(screen.queryByTestId("workload-people")).toBeNull();
    expect(screen.getByText("Not shown (under 3 people)")).toBeTruthy();
    rerender(<WorkloadView data={{ ...data, people: [{ person: "oid-1", team: "SALES", self: true, awaiting_reply: 3, tasks: 0, alerts: 0 }] }} />);
    expect(screen.getByTestId("workload-people")).toBeTruthy();
    expect(within(screen.getByTestId("workload-people")).getByText("You")).toBeTruthy();
    expect(screen.getByText("Your counts")).toBeTruthy();
  });

  it("lists an item's label changes with stale and no-approval marks, and hides none", () => {
    const data: LabelChanges = { object_id: "Product:x", total: 2, stale: 1, no_approval: 1, by_state: {}, changes: [
      { id: "a", kind: "label_change_stale", state: "requested", requested_on: "2026-10-01", stale: true, no_approval: false },
      { id: "b", kind: "label_change_unapproved", state: "done", requested_on: "2026-10-02", done_on: "2026-10-05", done_by: "file", stale: false, no_approval: true },
    ] };
    const { container, rerender } = render(<LabelChangesView data={data} />);
    expect(screen.getAllByTestId("label-change")).toHaveLength(2);
    expect(screen.getByText(/no approval recorded/)).toBeTruthy();
    rerender(<LabelChangesView data={{ ...data, total: 0, changes: [] }} />);
    expect(container.querySelector("[data-testid=label-changes]")).toBeNull();
  });
});
