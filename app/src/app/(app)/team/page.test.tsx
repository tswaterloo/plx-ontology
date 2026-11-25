import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ replace: vi.fn() }));
const who = vi.hoisted(() => ({ me: { access_label: null, feedback_admin: false } as Record<string, unknown> }));
vi.mock("@/components/auth", () => ({
  useAuth: () => ({ state: { status: "signed-in", me: who.me } }),
}));
vi.mock("next/navigation", () => ({
  usePathname: () => "/team",
  useRouter: () => ({ replace: nav.replace, push: () => undefined }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getLabour: () => Promise.resolve({ state: "not_granted" }),
  getWorkload: () => Promise.resolve({ teams: [{ team: "not_assigned", awaiting_reply: 4, tasks: 1, alerts: 0, suppressed: false }], people: [], granted_teams: [] }),
  getApprovals: () => new Promise(() => {}),
  getWorkloadGrants: () => Promise.reject(new Error("a reader who is not the owner never asks")),
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
    // /workload lands on this section; no grant editor for a reader who is not the owner
    expect(screen.getByRole("heading", { name: "Workload" }).closest("section")?.id).toBe("workload");
    expect(await screen.findByTestId("workload-teams")).toHaveTextContent("No team yet");
    expect(screen.queryByTestId("grant-editor")).toBeNull();
    // no word about labour for a reader who did not come for it
    expect(screen.queryByTestId("team-labour-refused")).toBeNull();
  });

  it("tells a reader sent by /labour that the hours need a grant they don't have", async () => {
    window.location.hash = "#labour";
    render(<TeamPage />);
    const note = await screen.findByTestId("team-labour-refused");
    expect(note.id).toBe("labour");
    expect(note).toHaveTextContent("HR or operations-management grant");
    expect(screen.queryByTestId("team-labour")).toBeNull();
    window.location.hash = "";
  });
});

describe("Team opened from the pinned Approvals", () => {
  it("shows the approvals first, above the people table; plain /team keeps them below", async () => {
    who.me = { access_label: null, feedback_admin: true, owner: true };
    const order = () => [...document.querySelectorAll("main section, section")].map((s) => s.id).filter((id) => id === "people" || id === "approvals");
    window.location.hash = "#approvals";
    const pinned = render(<TeamPage />);
    expect(await screen.findByTestId("team-approvals")).toBeInTheDocument();
    expect(order()).toEqual(["approvals", "people"]);
    expect(screen.getAllByTestId("team-approvals")).toHaveLength(1);
    pinned.unmount();
    window.location.hash = "";
    render(<TeamPage />);
    expect(await screen.findByTestId("team-approvals")).toBeInTheDocument();
    expect(order()).toEqual(["people", "approvals"]);
    who.me = { access_label: null, feedback_admin: false };
  });
});
