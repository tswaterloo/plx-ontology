import { afterEach, describe, expect, it, vi } from "vitest";
import { getHome, getMe, getViewAsPeople, logout, markBriefingSeen } from "./api";
import { setViewingAs, viewingAs } from "./view-as";

const sent: { path: string; method: string; headers: Record<string, string> }[] = [];
vi.stubGlobal("fetch", (path: string, init: RequestInit) => {
  sent.push({ path, method: (init.method ?? "GET").toUpperCase(), headers: init.headers as Record<string, string> });
  return Promise.resolve(new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }));
});

describe("view as, in the browser", () => {
  afterEach(() => {
    setViewingAs(null);
    sent.length = 0;
  });

  it("reads carry the person's key; the owner's own paths do not; writes never leave the browser", async () => {
    setViewingAs({ key: "0123456789abcdef0123", name: "Vince" });
    await getHome();
    await getMe();
    await getViewAsPeople();
    expect(sent.map((s) => [s.path, s.headers["X-PLX-View-As"]])).toEqual([
      ["/api/home", "0123456789abcdef0123"],
      ["/api/me", undefined],
      ["/api/view-as/people", undefined],
    ]);
    await expect(markBriefingSeen()).rejects.toMatchObject({ status: 403, code: "view_as_refused" });
    expect(sent).toHaveLength(3);
    await logout();
    expect(viewingAs()).toBeNull();
    // signing out while viewing writes the stop mark, with no view-as header on it
    expect(sent.slice(3).map((s) => [s.method, s.path, s.headers["X-PLX-View-As"]])).toEqual([
      ["POST", "/api/view-as/stop", undefined],
      ["POST", "/api/auth/logout", undefined],
    ]);
  });

  it("stays off once stopped, whatever the tab storage holds", async () => {
    sessionStorage.setItem("plx-view-as", JSON.stringify({ key: "not-a-key", name: "x" }));
    expect(viewingAs()).toBeNull();
    await getHome();
    expect(sent[0].headers["X-PLX-View-As"]).toBeUndefined();
  });
});
