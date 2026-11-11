import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { groupActions, MeetingFollowUpView, MyActionItemsView, statusText, type Action, type FollowUp } from "./follow-up";

const cite = (t: string) => [`transcript://granola/m1@abc#t=${t}`];
const actions: Action[] = [
  { id: "a1", kind: "action", text: "I'll email Northfield Bakery today.", at: "00:03:15", citations: cite("00:03:15"), status: "overdue", due: "2099-01-02",
    owner: { name: "Priya Nair" }, records: [{ value: "Northfield Bakery", status: "linked", object_id: "Customer:C1", type: "Customer", label: "Northfield Bakery" }],
    prepared: { kind: "message", sent: false, writes: 0, citations: cite("00:03:15"), card_id: "meeting:a1",
      card: { id: "meeting:a1", subject: { label: "Email Northfield" }, draft: { to: { address: "orders@northfield.example" }, subject: "Follow-up: Ops weekly" } },
      offers: [{ kind: "send_email", label: "Send email", outbound: true }] } },
  { id: "a2", kind: "action", text: "Check stock on item PPC-80.", citations: cite("00:05:00"), status: "open", owner: { name: "Priya Nair" },
    prepared: { kind: "answer", sent: false, writes: 0, citations: [], answers: [{ object_id: "Item:PPC-80", label: "PPC-80", values: [{ property: "on_hand_kg", value: "800", citation: "c" }] }] } },
  { id: "a3", kind: "action", text: "Place a PO with Gulf Coast Starch.", citations: cite("00:06:00"), status: "done", owner: { name: "Marco Silva" },
    evidence: [{ what: "purchase order created" }],
    prepared: { kind: "write_intent", sent: false, writes: 0, citations: [], label: "New purchase order", needs: ["company_id"] } },
];
const followUp: FollowUp = {
  meeting: { meeting_id: "m1", title: "Ops weekly", start: "2099-01-01T14:00:00Z", organizer: { name: "Dana Price" } },
  summary: "Ops weekly on 2099-01-01; 1 decision(s).",
  decisions: [{ id: "d1", kind: "decision", text: "We decided to go with Gulf Coast Starch.", citations: cite("00:04:00"), at: "00:04:00" }],
  actions, questions: [], risks: [],
  status: { actions: 3, done: 1, overdue: 1, open: 2 },
};

describe("meeting follow-up", () => {
  it("shows decisions, each action's owner, status and prepared work, and no work for a done item", () => {
    render(<MeetingFollowUpView followUp={followUp} />);
    expect(screen.getByText("We decided to go with Gulf Coast Starch.")).toBeTruthy();
    expect(screen.getByTestId("follow-up-status").textContent).toBe("1 of 3 action items done, 1 overdue");
    expect(screen.getByTestId("prepared-message").textContent).toContain("orders@northfield.example");
    expect(screen.getByTestId("prepared-answer").textContent).toContain("on hand kg 800");
    expect(screen.queryByTestId("prepared-write")).toBeNull();
    expect(screen.getAllByTestId("follow-up-action")).toHaveLength(3);
    expect(screen.getByText("Northfield Bakery").getAttribute("href")).toBe("/object/Customer/Customer%3AC1");
  });

  it("lists my items with their meeting and status", () => {
    render(<MyActionItemsView items={[{ ...actions[0], meeting: "Ops weekly", meeting_id: "m1" }]} />);
    // the row and the "open by meeting" rail both link the meeting
    const links = screen.getAllByText("Ops weekly").map((e) => e.closest("a")?.getAttribute("href"));
    expect(links).toEqual(["/meetings/m1", "/meetings/m1"]);
    expect(statusText(actions[0])).toBe("Overdue, due 2099-01-02");
    expect(statusText(actions[2])).toBe("Done: purchase order created");
  });

  it("groups items overdue first, done last, by due date against today; counts agree", () => {
    const today = new Date(2099, 0, 5);
    const items: Action[] = [
      { ...actions[2] },
      { ...actions[1], id: "later", due: "2099-01-20" },
      { ...actions[1], id: "undated" },
      { ...actions[1], id: "week", due: "2099-01-08" },
      { ...actions[1], id: "past", due: "2099-01-04" },
      { ...actions[0] },
    ];
    expect(groupActions(items, today).map((g) => [g.key, g.items.map((a) => a.id)])).toEqual([
      ["overdue", ["past", "a1"]],
      ["week", ["week"]],
      ["later", ["later"]],
      ["undated", ["undated"]],
      ["done", ["a3"]],
    ]);
    render(<MyActionItemsView items={items} today={today} />);
    expect(screen.getAllByTestId("action-item")).toHaveLength(6);
    const figures = Object.fromEntries([...document.querySelectorAll("dl div")].map((d) => [d.querySelector("dt")?.textContent, d.querySelector("dd")?.textContent]));
    expect(figures).toMatchObject({ Open: "5", Overdue: "2", "Due this week": "1", Done: "1" });
  });
});
