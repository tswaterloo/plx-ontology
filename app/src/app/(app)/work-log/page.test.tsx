import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ total: null as number | null, asked: [] as { everyone?: boolean }[] }));
const auth = vi.hoisted(() => ({ state: { status: "loading" } as Record<string, unknown> }));
vi.mock("@/components/auth", () => ({ useAuth: () => ({ state: auth.state }) }));
const view = vi.hoisted(() => ({ as: null as { key: string; name: string } | null }));
vi.mock("@/components/view-as", () => ({ useViewingAs: () => view.as }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getWorkLog: (f: { everyone?: boolean }) => {
    api.asked.push(f);
    return f.everyone ? Promise.reject(new Error("refused")) : Promise.resolve({ total: api.total, offset: 0, events: [] });
  },
}));
vi.mock("@/components/time-saved", () => ({ TimeSavedPanel: () => null }));
vi.mock("@/components/autopilot-scorecard", () => ({ AutopilotScorecardPanel: () => null }));

import WorkLogPage from "./page";

describe("Work log closed", () => {
  it("a log that cannot be shown offers no search and no counts that read as zero", async () => {
    api.total = null;
    render(<WorkLogPage />);
    await screen.findByText(/can.t be shown right now/);
    expect(screen.queryByRole("search")).toBeNull();
    expect(screen.queryByRole("group", { name: "Show" })).toBeNull();
  });

  it("an open log keeps its search and filters", async () => {
    api.total = 0;
    render(<WorkLogPage />);
    expect(await screen.findByRole("search")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Show" })).toBeInTheDocument();
  });
});

describe("Work log: whose", () => {
  const signedIn = (me: Record<string, unknown>) => ({ status: "signed-in", config: {}, me: { display_name: "Ana", principal: "p", auth: "dev", proof: "fixture", tenant: "plx", dev_mode: true, data_label: "FIXTURE", ...me } });

  it("a reader who is not the owner never asks for everyone's log and sees no switch", async () => {
    api.total = 0;
    api.asked = [];
    auth.state = signedIn({});
    render(<WorkLogPage />);
    expect(await screen.findByRole("search")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Only mine" })).toBeNull();
    expect(api.asked.some((f) => f.everyone)).toBe(false);
  });

  it("the owner, as /api/me says, may switch to everyone's", async () => {
    api.total = 0;
    api.asked = [];
    auth.state = signedIn({ owner: true });
    render(<WorkLogPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Only mine" }));
    expect(await screen.findByRole("button", { name: "Everyone" })).toHaveAttribute("aria-pressed", "true");
    expect(api.asked.some((f) => f.everyone)).toBe(true);
  });

  it("the owner viewing as someone sees that person's page: no Everyone", async () => {
    api.total = 0;
    api.asked = [];
    auth.state = signedIn({ owner: true });
    view.as = { key: "0123456789abcdef0123", name: "Ana" };
    render(<WorkLogPage />);
    expect(await screen.findByRole("search")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Only mine" })).toBeNull();
    expect(api.asked.some((f) => f.everyone)).toBe(false);
    view.as = null;
  });
});
