import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Turn } from "@/components/answer-card";

const reads = vi.hoisted(() => ({ home: null as unknown, late: vi.fn(), turns: [] as Turn[] }));
vi.mock("@/components/shell", async (orig) => ({
  ...(await orig<typeof import("@/components/shell")>()),
  loadLate: reads.late,
  loadHome: () => (reads.home ? Promise.resolve(reads.home) : Promise.reject(new Error("forced failure"))),
}));
vi.mock("@/components/ask-box", async (orig) => ({
  ...(await orig<typeof import("@/components/ask-box")>()),
  AskBox: () => <div data-testid="ask-box" />,
}));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(), useRouter: () => ({ replace: () => undefined, push: () => undefined }) }));
vi.mock("@/lib/thread", () => ({
  askUrlKey: () => "",
  conversationOf: () => undefined,
  lastActiveAskId: () => null,
  useAskRunning: () => ({ anyRunning: false, runningIds: new Set<string>(), runningChats: [], currentId: null }),
  useThread: () => ({ turns: reads.turns, run: vi.fn(), retry: vi.fn(), followUp: vi.fn(), stop: vi.fn(), reset: vi.fn(), busy: false, seed: vi.fn(), select: () => false, claimUrlKey: () => true, conversationId: null, runningIds: new Set<string>() }),
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
  it("shows a minimal header, offers starters from the reader's live work without stuffing past questions, and keeps recent chats in the rail alone", async () => {
    reads.turns = [];
    reads.home = {
      ...HOME,
      needs_attention: { late_pos: [{ label: "PO 90412", object_id: "po:90412" }], at_risk: [] },
    };
    render(<AskRoute />);
    expect(screen.getByTestId("ask-box")).toBeInTheDocument();
    expect(await screen.findByTestId("ask-greeting")).toHaveTextContent("Ask Workbench");
    const starters = within(await screen.findByTestId("ask-starters")).getAllByRole("button");
    expect(starters.map((b) => b.textContent)).toEqual([
      "What do I need to do today?",
      "Why is PO 90412 late?",
      "Which purchase orders are late?",
      "Show monthly revenue this year",
    ]);
    expect(screen.queryByTestId("ask-recent")).toBeNull();
    expect(reads.late).not.toHaveBeenCalled();
  });

  it("shows the greeting and 4 operational prompt chips when Home can't be read", async () => {
    reads.turns = [];
    reads.home = null;
    render(<AskRoute />);
    expect(screen.getByTestId("ask-box")).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 0));
    const starters = within(screen.getByTestId("ask-starters")).getAllByRole("button");
    expect(starters.map((b) => b.textContent)).toEqual([
      "What do I need to do today?",
      "Which purchase orders are late?",
      "Show monthly revenue this year",
      "Which customers have overdue invoices?",
    ]);
    expect(screen.queryByTestId("ask-recent")).toBeNull();
    expect(screen.queryByText(/didn.t load|Couldn.t read/)).toBeNull();
  });

  it("renders compact Liquid Glass SpaceSwitcher in header and opens the Split Side-Panel Canvas from an Artifact Card", async () => {
    reads.turns = [
      {
        id: "t-split",
        question: "Show monthly revenue this year",
        phase: "done",
        streamed: "",
        simple: true,
        steps: ["Queried invoiced sales by month"],
        answer: {
          answer_id: "a-split",
          question: "Show monthly revenue this year",
          conversation_id: "c-split",
          outcome: "answered",
          latency_ms: 3200,
          access_label: null,
          direct_answer: "Monthly revenue totaled 335,000 CAD.",
          facts: [],
          not_found: [],
          hidden: { count: 0 },
          conflicts: [],
          freshness: { as_of: "2026-11-26T00:00:00Z" },
          cited: [
            {
              about: "sales invoices",
              columns: ["month", "revenue"],
              rows: [["Jan", "100000"], ["Feb", "120000"]],
              records: 2,
              query: "SELECT month, revenue FROM sales_invoices",
              as_of: "2026-11-26",
            },
          ],
          charts: [
            {
              kind: "bar",
              title: "Monthly revenue (CAD)",
              x: "month",
              labels: ["Jan", "Feb", "Mar"],
              series: [{ name: "revenue", values: [100000, 120000, 115000] }],
              rows: 3,
              shown: 3,
              currency: "CAD",
              sql: "SELECT month, revenue FROM sales_invoices",
            },
          ],
        },
      },
    ];
    render(<AskRoute />);
    expect(screen.getByTestId("genie-spaces").className).toContain("liquid-glass-pill");
    expect(screen.getByTestId("artifact-card")).toHaveTextContent("Monthly revenue (CAD)");
    expect(screen.queryByTestId("ask-side-panel")).toBeNull();

    await userEvent.click(screen.getByTestId("open-side-panel"));
    const panel = screen.getByTestId("ask-side-panel");
    expect(panel).toHaveTextContent("Monthly revenue (CAD)");
    expect(within(panel).getByTestId("side-panel-sql-btn")).toHaveTextContent("Show SQL");
    expect(within(panel).getByTestId("side-panel-pin-btn")).toHaveTextContent("Pin to Dashboard");
    expect(within(panel).getByTestId("excel-export-btn")).toHaveTextContent("Export Excel");

    await userEvent.click(within(panel).getByTestId("side-panel-sql-btn"));
    expect(within(panel).getByTestId("side-panel-sql")).toHaveTextContent("SELECT month, revenue FROM sales_invoices");

    await userEvent.click(within(panel).getByTestId("close-side-panel"));
    expect(screen.queryByTestId("ask-side-panel")).toBeNull();
    reads.turns = [];
  });
});
