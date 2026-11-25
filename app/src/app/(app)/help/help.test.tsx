import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { roleKey } from "@/lib/onboarding";

const nav = vi.hoisted(() => ({ params: new URLSearchParams(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: nav.replace }), useSearchParams: () => nav.params, usePathname: () => "/help" }));
vi.mock("@/components/auth", () => ({
  useAuth: () => ({ state: { status: "signed-in", me: { principal: "employee:p1", display_name: "Pat Reader" } } }),
}));

import HelpPage from "./page";

describe("the help page", () => {
  beforeEach(() => {
    window.localStorage.clear();
    nav.params = new URLSearchParams();
    nav.replace.mockClear();
  });

  it("asks for a role first, then shows that role's guide and remembers it", () => {
    render(<HelpPage />);
    expect(screen.getByTestId("help-pick")).toBeInTheDocument();
    expect(screen.getByTestId("onboarding-checklist")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("help-role-production_planning"));
    expect(screen.getByTestId("role-guide")).toHaveTextContent("Shows which orders are short of material");
    expect(screen.getByTestId("help-role-production_planning")).toHaveAttribute("aria-checked", "true");
    expect(window.localStorage.getItem(roleKey("employee:p1"))).toBe("production_planning");
    expect(nav.replace).toHaveBeenCalledWith("/help?role=production_planning", { scroll: false });
  });

  it("opens on the role in the link, and ignores a role it has no guide for", () => {
    nav.params = new URLSearchParams("role=rnd");
    const { unmount } = render(<HelpPage />);
    expect(screen.getByTestId("role-guide")).toHaveTextContent("Finds the mail, specs and test results");
    unmount();
    // the owner has a guide of their own
    nav.params = new URLSearchParams("role=owner");
    const { unmount: again } = render(<HelpPage />);
    expect(screen.getByTestId("role-guide")).toHaveTextContent("Puts what needs you first on Home");
    again();
    nav.params = new URLSearchParams("role=janitor");
    render(<HelpPage />);
    expect(screen.getByTestId("help-pick")).toBeInTheDocument();
  });

  it("offers the desktop download in a browser, and not inside the desktop app", () => {
    const { unmount } = render(<HelpPage />);
    expect(screen.getByTestId("help-desktop-download")).toHaveAttribute("href", "/desktop/");
    unmount();
    (window as unknown as { plxDesktop?: unknown }).plxDesktop = { app: true, version: "0.1.0", notify: () => undefined, onMenu: () => () => undefined };
    try {
      render(<HelpPage />);
      expect(screen.queryByTestId("help-desktop")).toBeNull();
    } finally {
      delete (window as unknown as { plxDesktop?: unknown }).plxDesktop;
    }
  });

  it("includes Owner cleanly, gives truthful IT desktop guidance, and does not claim Microsoft sign-in is active", () => {
    render(<HelpPage />);
    const ownerBtn = screen.getByTestId("help-role-owner");
    expect(ownerBtn).toHaveTextContent("Owner");
    fireEvent.click(ownerBtn);
    const guide = screen.getByTestId("role-guide");
    expect(guide).toHaveTextContent("Puts what needs you first on Home");
    // Owner has no autopilot workflows listed, so no empty "Workflows that prepare routine items" heading
    expect(guide).not.toHaveTextContent("Workflows that prepare routine items for this role:");
    const desktop = screen.getByTestId("help-desktop");
    expect(desktop).toHaveTextContent("Ask IT to install it for you");
    expect(desktop.textContent).not.toMatch(/SmartScreen|Gatekeeper|Run anyway|More info|right-click|unsigned/i);
    expect(document.body.textContent).not.toMatch(/Microsoft/i);
  });
});
