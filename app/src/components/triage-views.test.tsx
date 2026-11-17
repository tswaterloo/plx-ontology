import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { RdBoard, RdProject, LabelChanges, ReplyTriage } from "@/lib/types";
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
    expect(screen.getByText(/nothing is sent until you confirm it/i)).toBeTruthy();
  });

  it("groups projects by customer, says the status once with the date in words, and never shows a project without a customer", () => {
    const p = (id: string, customer: string | null, over: Partial<RdProject> = {}) =>
      ({ id, customer, kind: "coa", status: "waiting_on_customer", last_on: "2026-08-17", days: 64, idle: false, owner: "oid-1", owner_is_you: false, waiting_on: "customer", items: [], lines: [], citations: [], draft, offers: [], ...over }) as RdProject;
    const board: RdBoard = { as_of: "2026-11-08", idle_after_days: 5, total: 4, idle: 1, sent: 0, writes: 0, projects: [
      p("a1", "Customer:C-100"), p("b1", "Customer:C-200", { idle: true, status: "waiting_on_us", waiting_on: "us" }), p("a2", "Customer:C-100", { kind: "sample_request" }), p("x", null) ] };
    render(<RdBoardView board={board} />);
    const groups = screen.getAllByTestId("rd-customer");
    expect(groups).toHaveLength(2);
    expect(groups[0]).toHaveTextContent("C-200"); // the idle customer first
    expect(groups[1].querySelectorAll("[data-testid=rd-project]")).toHaveLength(2);
    expect(screen.getAllByTestId("rd-project")).toHaveLength(3);
    const line = within(groups[1]).getAllByText(/Waiting on the customer/, { selector: "div" })[0].textContent ?? "";
    expect(line.match(/waiting on the customer/gi)).toHaveLength(1);
    expect(line).toContain("17 Aug");
    expect(line).not.toMatch(/\d{4}-\d{2}-\d{2}/);
    expect(screen.queryByText(/Early/)).toBeNull();
    expect(screen.queryByText(/domain/)).toBeNull();
  });

  it("heads each customer's group with its name, links it to the record, and falls back to the handle", () => {
    const p = (id: string, customer: string, name: string | null) =>
      ({ id, customer, customer_name: name, kind: "coa", status: "waiting_on_customer", last_on: "2026-08-17", days: 64, idle: false, owner: null, owner_is_you: false, waiting_on: "customer", items: [], lines: [], citations: [], draft, offers: [] }) as RdProject;
    const board: RdBoard = { as_of: "2026-11-08", idle_after_days: 5, total: 3, idle: 0, sent: 0, writes: 0, projects: [
      p("a", "Customer:bc.customers:C-300", "Zephyr Labs"), p("b", "Customer:bc.customers:C-100", "Acme Foods"), p("c", "Customer:bc.customers:C-900", null) ] };
    render(<RdBoardView board={board} />);
    const heads = screen.getAllByTestId("rd-customer").map((g) => g.querySelector("h2 a"));
    expect(heads.map((a) => a?.textContent)).toEqual(["Acme Foods", "Zephyr Labs", expect.stringContaining("C-900")]);
    expect(heads[0]?.getAttribute("href")).toContain("C-100");
    expect(screen.getByRole("region", { name: "Acme Foods" })).toBeInTheDocument();
  });

});
