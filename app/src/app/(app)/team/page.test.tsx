import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("@/components/auth", () => ({
  useAuth: () => ({ state: { status: "signed-in", me: { access_label: null, feedback_admin: false } } }),
}));
vi.mock("next/navigation", () => ({ usePathname: () => "/team", useRouter: () => ({ replace: nav.replace, push: () => undefined }) }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getLabour: () => Promise.resolve({ state: "not_granted" }),
  getWorkload: () => new Promise(() => undefined),
  getOwnerWorkload: () => new Promise(() => undefined),
}));

import TeamPage from "./page";

describe("Team for a reader with no team or labour grant", () => {
  it("says it is opening My work while it leaves, never a blank page", async () => {
    render(<TeamPage />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/"));
    expect(screen.getByTestId("team-leaving")).toHaveTextContent("Opening My work");
  });
});
