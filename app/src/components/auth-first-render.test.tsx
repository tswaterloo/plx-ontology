import { render, screen, waitFor, act } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn() }), usePathname: () => "/", useSearchParams: () => new URLSearchParams() }));

import { AuthProvider, RequireAuth } from "./auth";
import { beginAuthCheck, setTokenProvider, settleAuth } from "@/lib/api";
import { resetSessionCaches } from "@/lib/session-cache";
import { getRecurring } from "@/lib/api";

const mounts = vi.hoisted(() => ({ n: 0 }));
function Page() {
  useEffect(() => {
    mounts.n += 1;
    void getRecurring().catch(() => undefined);   // the page's own read, at its first render
  }, []);
  return <div>page</div>;
}

afterEach(() => {
  vi.unstubAllGlobals();
  setTokenProvider(null);
  resetSessionCaches();
  mounts.n = 0;
});

function json(status: number, body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

describe("W1: the page's reads do not wait for the sign-in check", () => {
  it("the page is drawn and reads while the check is open, and is not mounted a second time when it answers", async () => {
    const asked: string[] = [];
    let answerMe: (r: Response) => void = () => undefined;
    vi.stubGlobal("fetch", vi.fn((u: string) => {
      asked.push(String(u));
      if (String(u) === "/api/auth/config") return json(200, { mode: "dev", dev_login: true });
      if (String(u) === "/api/me") return new Promise<Response>((r) => (answerMe = r));
      return json(200, { items: [] });
    }));
    render(<AuthProvider><RequireAuth fallback={<div>skeleton</div>}><Page /></RequireAuth></AuthProvider>);
    expect(screen.getByText("page")).toBeTruthy();
    await waitFor(() => expect(asked).toContain("/api/instructions/recurring"));
    expect(asked.indexOf("/api/me")).toBeGreaterThan(-1);
    await act(async () => answerMe(await json(200, { principal: "plx:staff:ana" })));
    await waitFor(() => expect(screen.getByText("page")).toBeTruthy());
    expect(mounts.n).toBe(1);
    expect(asked.filter((u) => u === "/api/instructions/recurring")).toHaveLength(1);
  });

  it("a refused sign-in check replaces the page with the fallback", async () => {
    vi.stubGlobal("fetch", vi.fn((u: string) => {
      if (String(u) === "/api/auth/config") return json(200, { mode: "dev", dev_login: true });
      if (String(u) === "/api/me") return json(401, { error: { code: "sign_in_required" } });
      return json(401, { error: { code: "sign_in_required" } });
    }));
    render(<AuthProvider><RequireAuth fallback={<div>skeleton</div>}><Page /></RequireAuth></AuthProvider>);
    await waitFor(() => expect(screen.getByText("skeleton")).toBeTruthy());
  });

  it("Microsoft mode: a read sent before the token is installed is sent once more with it; cookie mode keeps its 401", async () => {
    const sent: Array<string | null> = [];
    vi.stubGlobal("fetch", vi.fn((u: string, init?: RequestInit) => {
      const auth = (init?.headers as Record<string, string> | undefined)?.Authorization ?? null;
      sent.push(auth);
      return auth ? json(200, { items: [] }) : json(401, { error: { code: "sign_in_required" } });
    }));
    beginAuthCheck();
    const read = getRecurring();
    await waitFor(() => expect(sent).toHaveLength(1));
    setTokenProvider(async () => "tok");
    settleAuth();
    await expect(read).resolves.toEqual({ items: [] });
    expect(sent).toEqual([null, "Bearer tok"]);
    // cookie mode: the check settles with no provider, the 401 is the answer
    setTokenProvider(null);
    resetSessionCaches();
    beginAuthCheck();
    const plain = getRecurring().catch((e) => e);
    await waitFor(() => expect(sent).toHaveLength(3));
    settleAuth();
    expect(await plain).toMatchObject({ status: 401 });
    expect(sent).toHaveLength(3);
  });
});
