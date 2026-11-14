import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// the late list and the freshness read both fail: the starting points say so, never
// "No late POs." or "No source has been read yet."
const down = vi.hoisted(() => () => Promise.reject(new Error("forced failure")));
vi.mock("@/components/shell", async (orig) => ({ ...(await orig<typeof import("@/components/shell")>()), loadLate: down, loadHome: down }));
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
