import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/shell", () => ({
  loadHome: vi.fn().mockResolvedValue({ needs_attention: { late_pos: [], at_risk: [] } }),
}));

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<object>()),
  getAskChats: vi.fn().mockResolvedValue({
    available: true,
    chats: [
      {
        conversation_id: "chat-rev-1",
        title: "please show me monthly revenue by customer",
        named: false,
        pinned: false,
        questions: 2,
        last_at: new Date(Date.now() - 2 * 3_600_000).toISOString(),
        running: false,
      },
    ],
  }),
}));

import { ASK_SPACES, AskStart, spaceById, type AskSpaceId } from "./ask-start";

function Harness({ onAsk, draftQuery = "" }: { onAsk: (q: string, objectId?: string) => void; draftQuery?: string }) {
  const [spaceId, setSpaceId] = useState<AskSpaceId>("all");
  return (
    <AskStart onAsk={onAsk} spaceId={spaceId} onSpaceChange={setSpaceId} draftQuery={draftQuery}>
      <div data-testid="composer-slot" />
    </AskStart>
  );
}

describe("Genie Spaces switcher in AskStart", () => {
  it("renders all 6 Petra spaces with Liquid Glass styling, updates starters and scope hint on switch, and uses zero em dashes", async () => {
    const onAsk = vi.fn();
    render(<Harness onAsk={onAsk} />);

    const switcher = screen.getByTestId("genie-spaces");
    expect(switcher.className).toContain("liquid-glass-pill");
    expect(screen.getByTestId("space-pill-all").className).toContain("liquid-glass-active");
    for (const space of ASK_SPACES) {
      expect(screen.getByTestId(`space-pill-${space.id}`)).toHaveTextContent(space.label);
    }

    await userEvent.click(screen.getByTestId("space-pill-finance"));
    expect(screen.getByTestId("space-pill-finance").className).toContain("liquid-glass-active");
    expect(screen.getByTestId("ask-greeting")).toHaveTextContent("Ask Finance");
    expect(screen.getByTestId("space-scope-hint")).toHaveTextContent("General ledger, margins, AR aging, and AP payables");
    expect(screen.getByTestId("ask-starters")).toHaveTextContent("Which customers have overdue AR invoices?");

    await userEvent.click(screen.getByRole("button", { name: "Which customers have overdue AR invoices?" }));
    expect(onAsk).toHaveBeenCalledWith("Which customers have overdue AR invoices?", undefined);

    await userEvent.click(screen.getByTestId("space-pill-quality"));
    expect(screen.getByTestId("ask-starters")).toHaveTextContent("Which open shipments are waiting on a COA?");
    expect(screen.getAllByTestId("ask-starter-card")).toHaveLength(4);
    expect(screen.getAllByTestId("starter-tag").every((el) => el.textContent === "Quality")).toBe(true);
    expect(spaceById("quality").promptPrefix).toContain("Quality");
    expect(switcher.textContent).not.toContain("\u2014");
  });

  it("shows live quick-jump matches for recent chats and workflow skills as the user types", async () => {
    const onAsk = vi.fn();
    render(<Harness onAsk={onAsk} draftQuery="revenue" />);

    const strip = await screen.findByTestId("ask-quick-jump");
    expect(strip).toHaveTextContent("/revenue-why");
    expect(strip).toHaveTextContent("Monthly revenue by customer");

    await userEvent.click(screen.getByRole("button", { name: /\/revenue-why/ }));
    expect(onAsk).toHaveBeenCalledWith(expect.stringContaining("Explain the monthly revenue change"));
  });
});
