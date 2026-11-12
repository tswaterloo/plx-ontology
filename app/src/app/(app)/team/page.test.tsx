import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("@/components/auth", () => ({
  useAuth: () => ({ state: { status: "signed-in", me: { access_label: null, feedback_admin: false } } }),
}));
vi.mock("next/navigation", () => ({ usePathname: () => "/team", useRouter: () => ({ replace: nav.replace, push: () => undefined }) }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getLabour: () => Promise.resolve({ state: "not_granted" }),
  getTeam: () => Promise.resolve({
    scope: "self", state: "not_ready", as_of: null, window_to: null, note: "", people: [], totals: {}, systems: [],
  }),
}));

import TeamPage from "./page";

describe("Team for a reader with no team or labour grant", () => {
  it("stays on Team with the reader's own activity, never a blank page or a redirect", async () => {
    render(<TeamPage />);
    expect(await screen.findByRole("heading", { name: "Your activity" })).toBeInTheDocument();
    expect(screen.getByTestId("team-not-ready")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Approvals waiting on others" })).toBeNull();
    expect(nav.replace).not.toHaveBeenCalled();
  });
});
