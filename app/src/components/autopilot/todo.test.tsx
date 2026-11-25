import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PreparedView } from "@/components/autopilot/prepared";
import type { PreparedItem } from "@/lib/api";

const todo: PreparedItem = {
  item_id: "ap_9", sender: "", channel: "system", subject: "PO 5416 is late", message: "", kind: "todo", to: [], cc: [], body: "", edited: false,
  ask: "", facts: [], summary: "PO 5416 is 6 days late", created_at: "2026-11-18T14:00:00Z",
  todos: [
    { system: "sage", record: "PO 5416", what: "Move the expected date to Nov 25" },
    { system: "", record: "", what: "Ask Dana whether the lot can ship split" },
  ],
};
const reply: PreparedItem = { ...todo, item_id: "ap_1", kind: "reply", channel: "mail", sender: "Dana Buyer", body: "Yes, Friday works.", todos: [{ system: "filemaker", record: "AO-26042", what: "Note the new ship date" }] };

function fetchMock() {
  const calls: [string, unknown][] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push([new URL(url, "http://x").pathname, init?.body ? JSON.parse(String(init.body)) : undefined]);
    return new Response(JSON.stringify({ item_id: "ap_9" }), { status: 200, headers: { "content-type": "application/json" } });
  }));
  return calls;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("a to-do item", () => {
  it("shows each step with its system and record, and Done records the person's word without a write", async () => {
    const calls = fetchMock();
    const gone = vi.fn();
    render(<PreparedView items={[todo]} empty="none" onGone={gone} onChanged={() => undefined} />);
    const card = screen.getByTestId("prepared-todo");
    expect(within(card).getByText("PO 5416 is 6 days late")).toBeTruthy();
    expect(within(card).getByText("Sage, PO 5416:")).toBeTruthy();
    expect(within(card).getByText("Ask Dana whether the lot can ship split")).toBeTruthy();
    await userEvent.click(within(card).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(gone).toHaveBeenCalledWith("ap_9"));
    expect(calls).toEqual([["/api/autopilot/items/ap_9/decision", { decision: "accept" }]]);
  });

  it("Not needed rejects it", async () => {
    const calls = fetchMock();
    render(<PreparedView items={[todo]} empty="none" onGone={() => undefined} onChanged={() => undefined} />);
    await userEvent.click(screen.getByRole("button", { name: "More" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Not needed" }));
    await waitFor(() => expect(calls).toEqual([["/api/autopilot/items/ap_9/decision", { decision: "reject" }]]));
  });

  it("a reply card carries its to-dos under the reply", () => {
    render(<PreparedView items={[reply]} empty="none" onGone={() => undefined} onChanged={() => undefined} />);
    const card = screen.getByTestId("prepared-item");
    expect(within(card).getByText("FileMaker, AO-26042:")).toBeTruthy();
    expect(within(card).getByText("Note the new ship date")).toBeTruthy();
    expect(screen.queryByTestId("prepared-todo")).toBeNull();
  });

  it("shows owner list labels with a human name or plain fallback and proper sentence spacing", () => {
    const named: PreparedItem = {
      ...todo,
      item_id: "ap_named",
      note: "Draft on request.\nThis is on Pat Mercer's list, not yours. Only you can see this.",
    };
    const fallback: PreparedItem = {
      ...reply,
      item_id: "ap_fallback",
      note: "\nThis is on another team member's list, not yours. Only you can see this.",
    };
    render(<PreparedView items={[named, fallback]} empty="none" onGone={() => undefined} onChanged={() => undefined} />);
    const labels = screen.getAllByTestId("prepared-label").map((el) => el.textContent);
    expect(labels).toEqual([
      "This is on Pat Mercer's list, not yours. Only you can see this.",
      "This is on another team member's list, not yours. Only you can see this.",
    ]);
    for (const text of labels) {
      expect(text).not.toContain("not yours.Only");
    }
  });

  it("repairs legacy compressed scope strings and deduplicates repeated subject/summary lines", () => {
    const legacy: PreparedItem = {
      ...todo,
      item_id: "ap_legacy",
      subject: "Follow up on PO 5416",
      summary: "Follow up on PO 5416",
      message: "Follow up on PO 5416",
      note: "Follow up on PO 5416\nThis is on Sam's list,not yours.Only you can see this.",
    };
    render(<PreparedView items={[legacy]} empty="none" onGone={() => undefined} onChanged={() => undefined} />);
    const card = screen.getByTestId("prepared-todo");
    expect(within(card).getAllByText("Follow up on PO 5416")).toHaveLength(1);
    expect(within(card).getByTestId("prepared-label")).toHaveTextContent(
      "This is on Sam's list, not yours. Only you can see this.",
    );
  });
});
