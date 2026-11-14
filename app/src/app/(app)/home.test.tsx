import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// every read My work makes fails the way a server failure does
const { fail } = vi.hoisted(() => ({ fail: () => Promise.reject(Object.assign(new Error("forced"), { code: "internal", message: "forced" })) }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getBriefing: fail, getBriefingItems: fail, getSuggestions: fail, getActionStates: fail, getReplies: fail, markBriefingSeen: fail,
}));
vi.mock("@/components/shell", async (orig) => ({ ...(await orig<typeof import("@/components/shell")>()), loadHome: fail, loadLateRead: fail }));
vi.mock("@/components/needs-me", () => ({ NeedsMe: () => null }));

import HomePage from "./page";

describe("My work when nothing can be read", () => {
  it("shows the failure once and is no longer marked as still loading", async () => {
    render(<HomePage />);
    const list = await screen.findByTestId("needs-you");
    await waitFor(() => expect(list).toHaveTextContent("Couldn't load your work."));
    await waitFor(() => expect(list.hasAttribute("data-pending")).toBe(false));
    expect(screen.getAllByRole("button", { name: "Try again" })).toHaveLength(1);
  });
});
