import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect, useState } from "react";
import { describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  getAuthConfig: vi.fn(),
  getMe: vi.fn(),
  getHome: vi.fn(),
  logout: vi.fn(),
  setTokenProvider: vi.fn(),
}));
vi.mock("@/lib/api", async (original) => ({ ...(await original<object>()), ...api }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn() }), usePathname: () => "/", useSearchParams: () => new URLSearchParams() }));

import { AuthProvider, useAuth } from "./auth";
import { loadHome } from "./shell";

/** Today's /api/home as the page shows it: read once through the shared cache. */
function HomeProbe() {
  const { state, refresh, signOut } = useAuth();
  const [home, setHome] = useState<string>("none");
  useEffect(() => {
    if (state.status !== "signed-in") return;
    let live = true;
    loadHome().then((h) => live && setHome((h as unknown as { who: string }).who), () => undefined);
    return () => {
      live = false;
    };
  }, [state.status]);
  return (
    <div>
      <span data-testid="home">{home}</span>
      <button onClick={refresh}>refresh</button>
      <button onClick={() => void signOut()}>sign out</button>
    </div>
  );
}

const me = (who: string) => ({ principal: `plx:staff:${who}`, display_name: who });

describe("a different person in the same tab (WEB-001)", () => {
  it("never sees what was read for the last one, even a read still in flight", async () => {
    api.getAuthConfig.mockResolvedValue({ mode: "dev", dev_login: true });
    api.logout.mockResolvedValue(undefined);
    let finishAna: (v: unknown) => void = () => undefined;
    api.getHome
      .mockImplementationOnce(() => Promise.resolve({ who: "ana" }))
      .mockImplementationOnce(() => new Promise((r) => (finishAna = r)))   // Ana's second read, slow
      .mockImplementation(() => Promise.resolve({ who: "ben" }));
    api.getMe.mockResolvedValueOnce(me("ana")).mockResolvedValueOnce(me("ana"));
    render(<AuthProvider><HomeProbe /></AuthProvider>);
    await waitFor(() => expect(screen.getByTestId("home").textContent).toBe("ana"));
    expect(api.getHome).toHaveBeenCalledTimes(1);
    // the same person again: the page load's read is kept
    fireEvent.click(screen.getByText("refresh"));
    await waitFor(() => expect(api.getMe).toHaveBeenCalledTimes(2));
    expect(api.getHome).toHaveBeenCalledTimes(1);
    // Ana signs out; a fresh read starts for her and is still in flight when Ben signs in
    fireEvent.click(screen.getByText("sign out"));
    await waitFor(() => expect(api.logout).toHaveBeenCalled());
    api.getMe.mockResolvedValueOnce(me("ana"));
    fireEvent.click(screen.getByText("refresh"));
    await waitFor(() => expect(api.getHome).toHaveBeenCalledTimes(2));
    api.getMe.mockResolvedValueOnce(me("ben"));
    fireEvent.click(screen.getByText("refresh"));
    await waitFor(() => expect(screen.getByTestId("home").textContent).toBe("ben"));
    await act(async () => finishAna({ who: "ana" }));
    expect(screen.getByTestId("home").textContent).toBe("ben");
    expect(api.getHome).toHaveBeenCalledTimes(3);
  });
});

describe("sign-out that the server did not complete (WEB-014)", () => {
  it("stays signed in and says so; a retry that works signs out", async () => {
    api.getAuthConfig.mockResolvedValue({ mode: "dev", dev_login: true });
    api.getHome.mockResolvedValue({ who: "ana" });
    api.getMe.mockResolvedValue(me("ana"));
    function Probe() {
      const { state, signOut, signOutError } = useAuth();
      return (
        <div>
          <span data-testid="status">{state.status}</span>
          <span data-testid="err">{signOutError ?? ""}</span>
          <button onClick={() => void signOut()}>out</button>
        </div>
      );
    }
    render(<AuthProvider><Probe /></AuthProvider>);
    await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("signed-in"));
    api.logout.mockRejectedValueOnce(new TypeError("network"));
    fireEvent.click(screen.getByText("out"));
    await waitFor(() => expect(screen.getByTestId("err").textContent).toContain("still signed in"));
    expect(screen.getByTestId("status").textContent).toBe("signed-in");
    api.logout.mockResolvedValueOnce(undefined);
    fireEvent.click(screen.getByText("out"));
    await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("signed-out"));
    expect(screen.getByTestId("err").textContent).toBe("");
  });
});
