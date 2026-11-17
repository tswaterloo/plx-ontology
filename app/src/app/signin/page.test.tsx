import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ state: {} as Record<string, unknown> }));
vi.mock("@/components/auth", () => ({
  useAuth: () => ({ state: auth.state, refresh: () => undefined, signOut: () => Promise.resolve(true), signOutError: null, redirectNext: null }),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: () => undefined, push: () => undefined }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/signin",
}));

import SignInPage from "./page";

const signedOut = (config: Record<string, unknown>) => ({ status: "signed-out", config: { mode: "dev", dev_login: true, sign_in_available: true, ...config } });

describe("sign-in page", () => {
  afterEach(() => {
    auth.state = {};
  });

  it("on the live server that takes access links it never says demo and asks for the link at once", () => {
    auth.state = signedOut({ sign_in: "access_link", message: "Paste the access link you were given." });
    render(<SignInPage />);
    expect(document.body.textContent ?? "").not.toMatch(/demo/i);
    expect(screen.getByText("Microsoft sign-in is not on here yet.")).toBeInTheDocument();
    expect(screen.getByLabelText("Access link or code")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue with your access link" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Use an access link" })).toBeNull();
  });

  it("a demo server still says it is one", () => {
    auth.state = signedOut({ sign_in: "demo" });
    render(<SignInPage />);
    expect(screen.getByText("Microsoft sign-in is off on this demo server.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue with demo sign-in" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Use an access link" })).toBeInTheDocument();
  });
});
