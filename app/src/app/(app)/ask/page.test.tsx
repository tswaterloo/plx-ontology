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
  it("is the box and the reader's recent questions, newest first, each once", async () => {
    reads.home = HOME;
    render(<AskRoute />);
    expect(screen.getByTestId("ask-box")).toBeInTheDocument();
    const recent = await screen.findByTestId("ask-recent");
    const rows = within(recent).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent(/^Landed cost of PO 90388/);
    expect(within(rows[1]).getByRole("link")).toHaveAttribute("href", "/ask?c=c1");
    // nothing else: no late list, no source freshness, no examples
    expect(reads.late).not.toHaveBeenCalled();
    expect(screen.queryByText(/Late orders|No late POs|Sources, last read|partly read|last read failed|Try asking/)).toBeNull();
    expect(screen.queryByTestId("ask-reads")).toBeNull();
  });

  it("shows just the box when the recent questions can't be read", async () => {
    reads.home = null;
    render(<AskRoute />);
    expect(screen.getByTestId("ask-box")).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByTestId("ask-recent")).toBeNull();
    expect(screen.queryByText(/didn.t load|Couldn.t read/)).toBeNull();
  });
});
