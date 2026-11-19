import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { LabelChanges, ReplyTriage } from "@/lib/types";
import { ReplyList } from "./reply-list";
import { LabelChangesView } from "./label-changes";

const draft = { channel: "email" as const, to: [{ address: "buyer@synth.example" }], subject: "Re: Order 1",
  body: "Hello, I am looking into it.", status: "draft_not_sent" as const, sent: false as const };

describe("my replies and label changes", () => {
  it("lists the reader's lines with drafts and marks a new inquiry", () => {
    const triage: ReplyTriage = { as_of: "2026-11-05", total: 2, sent: 0, writes: 0, lines: [
      { id: "pe-1", kind: "reply_owed", channel: "mail", days: 3, chases: 2, subject: "Order 1", draft, draft_state: "ready", rank: [1, 0, 2, 3], new_inquiry: false },
      { id: "pe-2", kind: "reply_owed", channel: "mail", days: 1, chases: 0, subject: "Bottles", draft, draft_state: "ready", rank: [0, 0, 0, 1], new_inquiry: true },
    ] };
    render(<ReplyList triage={triage} />);
    expect(screen.getAllByTestId("reply-line")).toHaveLength(2);
    expect(screen.getByText(/asked 3 times/)).toBeTruthy();
    expect(screen.getByText("New inquiry")).toBeTruthy();
    // the confirm step is said once, on the send preview, not on every list
    expect(screen.queryByText(/Nothing is sent until you confirm it/)).toBeNull();
  });

  it("draws only a model-written draft, with its note and warning", () => {
    const line = { id: "pe-1", kind: "reply_owed", channel: "mail", days: 1, chases: 0, subject: "Order 1", draft, draft_state: "ready" as const, rank: [0, 0, 0, 1], new_inquiry: false };
    const { rerender } = render(<ReplyList triage={{ as_of: "2026-11-05", total: 1, sent: 0, writes: 0, lines: [line] }} />);
    expect(screen.queryByTestId("reply-draft")).toBeNull();
    const model = { ...line, drafted_by: "model" as const, draft: { ...draft, body: "Order 1 ships [day]." }, draft_note: "Which day?", draft_warning: "Check before sending: x" };
    rerender(<ReplyList triage={{ as_of: "2026-11-05", total: 1, sent: 0, writes: 0, lines: [model] }} />);
    const shown = screen.getByTestId("reply-draft");
    expect(within(shown).getByText("Order 1 ships [day].")).toBeTruthy();
    expect(within(shown).getByText("To fill in: Which day?")).toBeTruthy();
    expect(within(shown).getByText("Check before sending: x")).toBeTruthy();
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

describe("a reply card", () => {
  const draft2 = { channel: "email" as const, to: [], subject: "Re: x", body: "I'm looking into your message of Wed 21 Oct and will come back to you by Thu 19 Nov.", status: "draft_not_sent" as const, sent: false as const };
  const offers = ["send_email", "teams_message", "create_follow_up", "mark_done", "snooze", "dismiss", "assign"].map((kind) => ({ kind, label: kind, outbound: false }));

  it("draws no canned holding reply and offers Reply, Done, Snooze, Not for me and More", () => {
    const triage = { as_of: "2026-11-05", total: 1, sent: 0, writes: 0, lines: [
      { id: "a", kind: "reply_owed", channel: "mail", days: 3, chases: 0, subject: "Order 1", draft: draft2, rank: [1], new_inquiry: false, card_id: "c1", offers },
    ] } as unknown as ReplyTriage;
    const { container } = render(<ReplyList triage={triage} />);
    expect(screen.queryByText(/looking into your message/)).toBeNull();
    expect(container.querySelector("pre")).toBeNull();
    expect(container.querySelector(".font-mono")).toBeNull();
    const row = screen.getByTestId("card-actions");
    expect(within(row).getAllByRole("button").map((b) => b.textContent?.trim())).toEqual(["Reply", "Done", "Snooze", "Not for me", "More"]);
  });
});
