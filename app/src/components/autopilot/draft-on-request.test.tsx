import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const calls = vi.hoisted(() => ({ draft: [] as string[], lists: [] as boolean[] }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(""), useRouter: () => ({ push: vi.fn(), replace: vi.fn() }), usePathname: () => "/" }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  requestDraft: (id: string) => (calls.draft.push(id), Promise.resolve({ status: "working" })),
  getAutopilotDrafts: (_signal?: AbortSignal, allAges = false) => (
    calls.lists.push(allAges),
    Promise.resolve({ items: [matter("m1")], never_senders: [], older: allAges ? 0 : 3, status: "ok", reason: "" })
  ),
  getAutopilot: () => Promise.reject(new Error("not read here")),
}));

import { TodoCard } from "@/components/autopilot/todo";
import { AssistQueue } from "@/components/home/assist-queue";

function matter(id: string, over: Record<string, unknown> = {}) {
  return { item_id: id, sender: "buyer@customer.example", channel: "mail", subject: "PO P-1", message: "can you confirm", kind: "todo", to: [], cc: [], body: "", edited: false, ask: "", facts: [], summary: "Confirm PO P-1", note: "Draft on request.", plan_status: "deferred", state: "open", created_at: "2026-11-23T14:00:00Z", ...over } as never;
}

describe("A matter with no draft yet", () => {
  it("shows its note and asks for the draft on a click, then says it is being prepared", async () => {
    render(<ul><TodoCard item={matter("m1")} onGone={() => undefined} /></ul>);
    expect(screen.getByTestId("prepared-note")).toHaveTextContent("Draft on request.");
    fireEvent.click(screen.getByRole("button", { name: "Draft it" }));
    await waitFor(() => expect(screen.getByTestId("draft-asked")).toBeInTheDocument());
    expect(calls.draft).toEqual(["m1"]);
    expect(screen.queryByRole("button", { name: "Draft it" })).toBeNull();
  });
  it("offers 'Ask again' when the draft failed, and nothing when there is nothing to ask", () => {
    const failed = render(<ul><TodoCard item={matter("m2", { plan_status: "failed" })} onGone={() => undefined} /></ul>);
    expect(screen.getByRole("button", { name: "Ask again" })).toBeInTheDocument();
    failed.unmount();
    render(<ul><TodoCard item={matter("m3", { plan_status: "drafted" })} onGone={() => undefined} /></ul>);
    expect(screen.queryByRole("button", { name: /Draft it|Ask again/ })).toBeNull();
  });
});

describe("The list's older items", () => {
  it("says how many are older than the 14 days and lists them all on a click", async () => {
    render(<AssistQueue />);
    expect(await screen.findByTestId("older-items")).toHaveTextContent("3 older items are not shown");
    fireEvent.click(screen.getByRole("button", { name: "Show them" }));
    await waitFor(() => expect(screen.queryByTestId("older-items")).toBeNull());
    expect(calls.lists).toContain(true);
  });
});
