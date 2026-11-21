import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PreparedView } from "@/components/autopilot/prepared";
import type { PreparedItem } from "@/lib/api";

// The walk's QC-hold card: Pass to a colleague and Not needed both refused (403) showed two
// identical boxes on one card. A card shows one problem at a time, the newest.
const base: PreparedItem = {
  item_id: "ap_7", sender: "", channel: "system", subject: "QC hold or fail", message: "", kind: "alert", to: [], cc: [], body: "", edited: false,
  ask: "", facts: [], summary: "Lot B-1 is on hold", created_at: "2026-11-20T14:00:00Z", links: [{ label: "Open the order", href: "/object/SalesOrder/x" }],
};

function refuseEverything() {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { code: "forbidden", message: "no" } }), { status: 403, headers: { "content-type": "application/json" } })));
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("one problem per card", () => {
  it("two refused actions on an alert card leave one banner", async () => {
    refuseEverything();
    render(<PreparedView items={[base]} empty="none" onGone={() => undefined} onChanged={() => undefined} />);
    const card = screen.getByTestId("prepared-alert");
    await userEvent.click(within(card).getByRole("button", { name: "More" }));
    await userEvent.click(within(card).getByRole("menuitem", { name: "Pass to a colleague" }));
    await waitFor(() => expect(within(card).getAllByRole("alert")).toHaveLength(1));
    await userEvent.click(within(card).getByRole("button", { name: "More" }));
    await userEvent.click(within(card).getByRole("menuitem", { name: "Not needed" }));
    await userEvent.click(within(card).getByRole("button", { name: "Later" }));
    await waitFor(() => expect(within(card).getAllByRole("alert")).toHaveLength(1));
    expect(within(card).getByRole("alert").textContent).toMatch(/don't have access/);
  });

  it("a to-do's own failure and its actions' failure share one banner, and a success clears it", async () => {
    refuseEverything();
    const todo: PreparedItem = { ...base, item_id: "ap_8", kind: "todo", links: [], todos: [{ system: "", record: "", what: "Call Dana" }] };
    render(<PreparedView items={[todo]} empty="none" onGone={() => undefined} onChanged={() => undefined} />);
    const card = screen.getByTestId("prepared-todo");
    await userEvent.click(within(card).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(within(card).getAllByRole("alert")).toHaveLength(1));
    await userEvent.click(within(card).getByRole("button", { name: "Later" }));
    await waitFor(() => expect(within(card).getAllByRole("button", { name: "Later" })[0].hasAttribute("disabled")).toBe(false));
    expect(within(card).getAllByRole("alert")).toHaveLength(1);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ item_id: "ap_8" }), { status: 200, headers: { "content-type": "application/json" } })));
    await userEvent.click(within(card).getByRole("button", { name: "Later" }));
    await waitFor(() => expect(within(card).queryByRole("alert")).toBeNull());
  });
});
