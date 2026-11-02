import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ state: {} as Record<string, unknown> }));
vi.mock("./auth", () => ({
  useAuth: () => ({ state: auth.state }),
  RequireAuth: ({ children }: { children: unknown }) => children,
}));
vi.mock("next/navigation", () => ({ usePathname: () => "/", useRouter: () => ({ push: () => undefined }) }));

import { AccessBanner, DevBanner } from "./shell";

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
