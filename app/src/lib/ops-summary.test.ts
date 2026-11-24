import { afterEach, describe, expect, it, vi } from "vitest";
import { getOpsProblemOpen } from "./api";
import { resetSessionCaches } from "./session-cache";

function serve(body: unknown) {
  const seen: Array<{ url: string; ahead: string | null }> = [];
  vi.stubGlobal("fetch", vi.fn((u: string, init?: RequestInit) => {
    seen.push({ url: String(u), ahead: (init?.headers as Record<string, string>)["X-PLX-Ahead"] ?? null });
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
  }));
  return seen;
}
afterEach(() => {
  vi.unstubAllGlobals();
  resetSessionCaches();
});

describe("the nav dot's small read", () => {
  it("asks the summary as a read-ahead and reads its count", async () => {
    const seen = serve({ configured: true, open: 2 });
    await expect(getOpsProblemOpen()).resolves.toBe(true);
    expect(seen).toEqual([{ url: "/api/ops/status?summary=1", ahead: "1" }]);
  });
  it("zero open, or not configured, is no problem", async () => {
    serve({ configured: true, open: 0 });
    await expect(getOpsProblemOpen()).resolves.toBe(false);
    serve({ configured: false, open: 3 });
    await expect(getOpsProblemOpen()).resolves.toBe(false);
  });
  it("the full list's open array answers the same; any other shape fails, it is never 'no problem'", async () => {
    serve({ configured: true, open: [{ key: "a" }] });
    await expect(getOpsProblemOpen()).resolves.toBe(true);
    serve({ configured: true, open: "many" });
    await expect(getOpsProblemOpen()).rejects.toMatchObject({ status: 502 });
  });
});
