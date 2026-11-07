import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ state: {} as Record<string, unknown> }));
vi.mock("./auth", () => ({
  useAuth: () => ({ state: auth.state }),
  RequireAuth: ({ children }: { children: unknown }) => children,
}));
vi.mock("next/navigation", () => ({ usePathname: () => "/", useRouter: () => ({ push: () => undefined }) }));

const api = vi.hoisted(() => ({ ops: null as unknown }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getOpsHealth: () => Promise.resolve(api.ops),
}));

import { AccessBanner, DevBanner, OpsStatus } from "./shell";

const me = (over: Record<string, unknown>) => ({
  status: "signed-in",
  config: { mode: "dev" },
  me: { principal: "p", display_name: "Vince", auth: "dev", dev_mode: true, data_label: "FIXTURE", access_label: null, ...over },
});

function Banners() {
  return (
    <>
      <DevBanner />
      <AccessBanner />
    </>
  );
}

describe("page banners", () => {
  afterEach(() => (auth.state = {}));

  it("DEV mode is one short line, the full-read label said once beside it", () => {
    auth.state = me({ access_label: "Viewing with full-read access" });
    render(<Banners />);
    const dev = screen.getByTestId("dev-banner");
    expect(dev).toHaveTextContent("Demo sign-in");
    expect(dev).not.toHaveTextContent("Not for staff use");
    expect(dev).toHaveAttribute("title", expect.stringContaining("FIXTURE"));
    expect(screen.getAllByTestId("access-banner")).toHaveLength(1);
    expect(screen.getAllByText("Viewing with full-read access")).toHaveLength(1);
  });

  it("Microsoft sign-in shows the full-read label once, and no DEV line", () => {
    auth.state = { ...me({ access_label: "Viewing with full-read access", auth: "entra", dev_mode: false }), config: { mode: "entra" } };
    render(<Banners />);
    expect(screen.queryByTestId("dev-banner")).toBeNull();
    expect(screen.getAllByTestId("access-banner")).toHaveLength(1);
    expect(screen.getAllByText("Viewing with full-read access")).toHaveLength(1);
  });

  it("no grant, no label", () => {
    auth.state = { ...me({ auth: "entra", dev_mode: false }), config: { mode: "entra" } };
    render(<Banners />);
    expect(screen.queryByTestId("access-banner")).toBeNull();
    expect(screen.queryByTestId("dev-banner")).toBeNull();
  });
});

describe("ops status line", () => {
  afterEach(() => {
    auth.state = {};
    api.ops = null;
  });
  const breach = {
    status: "breach",
    checked_at: "2030-01-02T12:00:00Z",
    breaches: ["delta_last_cycle", "truth_contradicted_share"],
    checks: [
      { name: "delta_last_cycle", value: 107, target: 15, unit: "min", state: "breach", detail: "" },
      { name: "truth_contradicted_share", value: 0.75, target: 0.25, unit: "share", state: "breach", detail: "" },
      { name: "capture:mail", value: 30, target: 360, unit: "min", state: "ok", detail: "" },
    ],
  };

  it("names each missed target to the full-read owner", async () => {
    auth.state = me({ access_label: "Viewing with full-read access" });
    api.ops = breach;
    render(<OpsStatus />);
    const line = await screen.findByTestId("ops-status");
    expect(line).toHaveTextContent("Ops: 2 behind");
    expect(line).toHaveTextContent("Last update cycle, Alerts contradicted");
    expect(line).not.toHaveTextContent("Capture");
  });

  it("says nothing to other readers, or when every target holds", async () => {
    auth.state = me({});
    api.ops = breach;
    render(<OpsStatus />);
    auth.state = me({ access_label: "Viewing with full-read access" });
    api.ops = { ...breach, status: "ok", breaches: [], checks: [] };
    render(<OpsStatus />);
    await waitFor(() => expect(screen.queryByTestId("ops-status")).toBeNull());
  });
});
