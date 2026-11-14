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
    expect(screen.getByTestId("role-guide")).toHaveTextContent("Lists R&D and sample projects");
    unmount();
    nav.params = new URLSearchParams("role=owner");
    render(<HelpPage />);
    expect(screen.getByTestId("help-pick")).toBeInTheDocument();
  });
});
