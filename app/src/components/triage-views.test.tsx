import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { RdBoard, LabelChanges, ReplyTriage } from "@/lib/types";
import { ReplyList } from "./reply-list";
import { RdBoardView } from "./rd-board";
import { LabelChangesView } from "./label-changes";

const draft = { channel: "email" as const, to: [{ address: "buyer@synth.example" }], subject: "Re: Order 1",
  body: "Hello, I am looking into it.", status: "draft_not_sent" as const, sent: false as const };

describe("my replies and label changes", () => {
  it("lists the reader's lines with drafts and marks a new inquiry", () => {
    const triage: ReplyTriage = { as_of: "2026-11-05", total: 2, sent: 0, writes: 0, lines: [
      { id: "pe-1", kind: "reply_owed", channel: "mail", days: 3, chases: 2, subject: "Order 1", draft, rank: [1, 0, 2, 3], new_inquiry: false },
      { id: "pe-2", kind: "reply_owed", channel: "mail", days: 1, chases: 0, subject: "Bottles", draft, rank: [0, 0, 0, 1], new_inquiry: true },
    ] };
    render(<ReplyList triage={triage} />);
    expect(screen.getAllByTestId("reply-line")).toHaveLength(2);
    expect(screen.getByText(/asked 3 times/)).toBeTruthy();
    expect(screen.getByText("New inquiry")).toBeTruthy();
    // the confirm step is said once, on the send preview, not on every list
    expect(screen.queryByText(/Nothing is sent until you confirm it/)).toBeNull();
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

  it("lists R&D projects with status, idle mark and a draft that is not sent", () => {
    const board: RdBoard = { as_of: "2026-11-08", idle_after_days: 5, total: 1, idle: 1, sent: 0, writes: 0, projects: [
      { id: "p1", customer: "Customer:1", kind: "sample_request", status: "waiting_on_us", last_on: "2026-11-01", days: 5,
        idle: true, owner: "oid-1", owner_is_you: false, waiting_on: "us", items: [], lines: [
          { id: "g1", state: "waiting_on_us", last_on: "2026-11-01", messages_n: 2, subject: "Sample 1", citations: [] }],
        citations: [], draft, offers: [] }] };
    render(<RdBoardView board={board} />);
    expect(screen.getAllByTestId("rd-project")).toHaveLength(1);
    expect(screen.getByTestId("rd-idle")).toBeTruthy();
    expect(screen.getByText(/Waiting on us/, { selector: "div" })).toBeTruthy();
    expect(screen.getByText(/Nothing is sent until you confirm it/)).toBeTruthy();
  });
});
