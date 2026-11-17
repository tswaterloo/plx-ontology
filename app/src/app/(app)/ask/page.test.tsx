import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// the late list and the freshness read both fail: the starting points say so, never
// "No late POs." or "No source has been read yet."
const down = vi.hoisted(() => () => Promise.reject(new Error("forced failure")));
const reads = vi.hoisted(() => ({ late: null as unknown[] | null, home: null as unknown }));
vi.mock("@/components/shell", async (orig) => ({
  ...(await orig<typeof import("@/components/shell")>()),
  loadLate: () => (reads.late ? Promise.resolve(reads.late) : down()),
  loadHome: () => (reads.home ? Promise.resolve(reads.home) : down()),
}));
vi.mock("@/components/ask-box", () => ({ AskBox: () => <div data-testid="ask-box" /> }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(), useRouter: () => ({ replace: () => undefined, push: () => undefined }) }));
vi.mock("@/lib/thread", () => ({
  askUrlKey: () => "",
  useThread: () => ({ turns: [], run: vi.fn(), retry: vi.fn(), followUp: vi.fn(), stop: vi.fn(), reset: vi.fn(), busy: false, seed: vi.fn() }),
}));

import AskRoute from "./page";

describe("Ask's starting points when their reads fail", () => {
  it("says the read failed instead of showing an empty list", async () => {
    render(<AskRoute />);
    expect(await screen.findByText(/The late list didn.t load just now/)).toBeInTheDocument();
    expect(await screen.findByText(/Couldn.t read how current each system is just now/)).toBeInTheDocument();
    expect(screen.queryByText("No late POs.")).toBeNull();
    expect(screen.queryByText("No source has been read yet.")).toBeNull();
  });
});

describe("Ask's starting points with data", () => {
  it("lists the reader's recent questions newest first with their age, and each late order with its date and value", async () => {
    const hoursAgo = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString();
    reads.late = Array.from({ length: 8 }, (_, i) => ({
      object_id: `PurchaseOrder:demo:${i}`, label: String(90400 + i), open_url: "", reason: "late", supplier: "Maple Leaf", days_late: 20 - i,
      due_date: "2026-10-17", amount: i === 0 ? { amount: "18400.00", currency: "CAD" } : null,
    }));
    reads.home = {
      greeting_name: "A", examples: ["Which purchase orders are late?"],
      recent_questions: [
        { question: "Why is PO 90412 late?", asked_at: hoursAgo(30), conversation_id: "c1" },
        { question: "Landed cost of PO 90388", asked_at: hoursAgo(2), conversation_id: null },
        { question: "why is po 90412 late?", asked_at: hoursAgo(40), conversation_id: "c0" },
      ],
      freshness: { sources: [{ system: "Email", stale: false, as_of: hoursAgo(1), searchable_to: "2026-11-06T15:00:00Z" }] },
    };
    render(<AskRoute />);
    const recent = await screen.findByTestId("ask-recent");
    const rows = within(recent).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent(/^Landed cost of PO 90388/);
    expect(within(rows[1]).getByRole("link")).toHaveAttribute("href", "/ask?c=c1");
    const lines = await screen.findAllByTestId("ask-late-line");
    expect(lines).toHaveLength(6);
    expect(lines[0]).toHaveTextContent("needed 17 Oct · $18,400.00 CAD");
    expect(screen.getByRole("link", { name: /All 8 late orders/ })).toHaveAttribute("href", "/late");
    expect(screen.getByTestId("ask-reads")).toHaveTextContent("to 6 Nov");
  });
});
