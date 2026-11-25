import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const reads = vi.hoisted(() => ({ home: null as unknown, late: vi.fn() }));
vi.mock("@/components/shell", async (orig) => ({
  ...(await orig<typeof import("@/components/shell")>()),
  loadLate: reads.late,
  loadHome: () => (reads.home ? Promise.resolve(reads.home) : Promise.reject(new Error("forced failure"))),
}));
vi.mock("@/components/ask-box", () => ({ AskBox: () => <div data-testid="ask-box" /> }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(), useRouter: () => ({ replace: () => undefined, push: () => undefined }) }));
vi.mock("@/lib/thread", () => ({
  askUrlKey: () => "",
  conversationOf: () => undefined,
  useThread: () => ({ turns: [], run: vi.fn(), retry: vi.fn(), followUp: vi.fn(), stop: vi.fn(), reset: vi.fn(), busy: false, seed: vi.fn() }),
}));

import AskRoute from "./page";

const hoursAgo = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString();
const HOME = {
  greeting_name: "A", examples: ["Which purchase orders are late?"],
  recent_questions: [
    { question: "Why is PO 90412 late?", asked_at: hoursAgo(30), conversation_id: "c1" },
    { question: "Landed cost of PO 90388", asked_at: hoursAgo(2), conversation_id: null },
    { question: "why is po 90412 late?", asked_at: hoursAgo(40), conversation_id: "c0" },
  ],
  freshness: { sources: [{ system: "Email", stale: false, capture: "partial", as_of: hoursAgo(1) }] },
};

describe("Ask's start page", () => {
  it("greets by name, offers starters from the reader's own work, and lists their last chats", async () => {
    reads.home = {
      ...HOME,
      needs_attention: { late_pos: [{ label: "PO 90412", object_id: "po:90412" }], at_risk: [] },
    };
    render(<AskRoute />);
    expect(screen.getByTestId("ask-box")).toBeInTheDocument();
    expect(await screen.findByText(/^Good (morning|afternoon|evening), A$/)).toBeInTheDocument();
    const starters = within(await screen.findByTestId("ask-starters")).getAllByRole("button");
    expect(starters.map((b) => b.textContent)).toEqual([
      "What do I need to do today?",
      "Why is PO 90412 late?",
      "Landed cost of PO 90388",
    ]);
    const recent = await screen.findByTestId("ask-recent");
    // chats only (those with a conversation), each once, newest first
    const rows = within(recent).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByRole("link")).toHaveAttribute("href", "/ask?c=c1");
    expect(reads.late).not.toHaveBeenCalled();
  });

  it("shows the greeting and the day's starter when Home can't be read", async () => {
    reads.home = null;
    render(<AskRoute />);
    expect(screen.getByTestId("ask-box")).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByRole("button", { name: "What do I need to do today?" })).toBeInTheDocument();
    expect(screen.queryByTestId("ask-recent")).toBeNull();
    expect(screen.queryByText(/didn.t load|Couldn.t read/)).toBeNull();
  });
});
