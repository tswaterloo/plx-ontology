import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getTrust } from "./api";
import { ApiError } from "./errors";
import { noteAnswerHeaders, RELEASE, startReadStore, storeMayDraw, wipeStoreNow, resetReadStoreForTests, keptPath } from "./read-store";
import { resetSessionCaches } from "./session-cache";
import { keepRead, keptRead, MOST_KEPT_BYTES, useRead } from "./use-read";
import { setViewingAs } from "./view-as";

type Entry = { key: string; body: string; fetchedAt: number; release: string; door: string };
function bridge(open: { durable?: boolean; door: string | null; keys: string[]; entries: Entry[] }, more: Entry[] = []) {
  const store = {
    open: vi.fn(async () => ({ durable: true, ...open })),
    get: vi.fn(async (k: string) => more.find((e) => e.key === k) ?? null),
    put: vi.fn(async () => true),
    drop: vi.fn(async () => true),
    wipe: vi.fn(async () => true),
    onWiped: vi.fn((cb: () => void) => { wiped = cb; return () => undefined; }),
  };
  (window as unknown as { plxDesktop: unknown }).plxDesktop = { app: true, version: "t", notify: () => undefined, onMenu: () => () => undefined, store };
  return store;
}
let wiped: () => void = () => undefined;
const entry = (key: string, v: unknown, over: Partial<Entry> = {}): Entry => ({ key, body: JSON.stringify(v), fetchedAt: Date.now() - 600_000, release: RELEASE, door: "d1", ...over });
const headers = (h: Record<string, string>) => new Headers(h);

beforeEach(() => resetReadStoreForTests());
afterEach(() => {
  delete (window as unknown as { plxDesktop?: unknown }).plxDesktop;
  setViewingAs(null);
  resetSessionCaches();
  vi.unstubAllGlobals();
});

function Page() {
  const { data, at } = useRead<{ n: number }>("/api/trust", () => new Promise(() => undefined));
  return <p>{data ? `n=${data.n}` : "reading"}{at !== null ? " (earlier)" : ""}</p>;
}

describe("the page store, page side", () => {
  it("draws a stored answer in the first render, says it is an earlier one, and ignores another build's", async () => {
    bridge({ door: "d1", keys: ["/api/trust", "/api/ops/status"], entries: [entry("/api/trust", { n: 7 }), entry("/api/ops/status", { n: 1 }, { release: "other-build" })] });
    startReadStore();
    noteAnswerHeaders(headers({ "X-PLX-Door": "d1" })); // /api/me answered under the stored door
    storeMayDraw();
    await act(async () => undefined);
    render(<Page />);
    expect(screen.getByText("n=7 (earlier)")).toBeInTheDocument(); // same render: no wait for a read
    expect(keptRead("/api/ops/status")).toBeNull();
  });

  it("draws nothing until /api/me has answered, and not when its door differs from the stored one", async () => {
    bridge({ door: "d1", keys: ["/api/trust"], entries: [entry("/api/trust", { n: 7 })] });
    startReadStore();
    await act(async () => undefined);
    expect(keptRead("/api/trust")).toBeNull(); // the person is not known yet
    noteAnswerHeaders(headers({ "X-PLX-Door": "d2" })); // /api/me answered under another door
    storeMayDraw();
    await act(async () => undefined);
    expect(keptRead("/api/trust")).toBeNull();
  });

  it("a /api/me answer with no door value draws nothing (the door is unknown: fail closed)", async () => {
    bridge({ door: "d1", keys: ["/api/trust"], entries: [entry("/api/trust", { n: 7 })] });
    startReadStore();
    noteAnswerHeaders(headers({})); // /api/me answered, naming no door
    storeMayDraw();
    await act(async () => undefined);
    expect(keptRead("/api/trust")).toBeNull();
  });

  it("sign-out wipes the shell's store before the server is asked", async () => {
    const st = bridge({ door: "d1", keys: [], entries: [] });
    wipeStoreNow();
    expect(st.wipe).toHaveBeenCalledTimes(1);
  });

  it("fills the rest by recent use in idle time, four at a time", async () => {
    const more = Array.from({ length: 9 }, (_, i) => entry(`/api/x${i}`, { i }));
    const st = bridge({ door: "d1", keys: more.map((e) => e.key), entries: [] }, more);
    startReadStore();
    noteAnswerHeaders(headers({ "X-PLX-Door": "d1" })); // /api/me answered under the stored door
    storeMayDraw();
    await vi.waitFor(() => expect(st.get).toHaveBeenCalledTimes(9), { timeout: 4000 });
    expect(keptRead("/api/x8")).toEqual({ i: 8 });
  });

  it("a moved door value forgets the page's copy and the shell's; the same value changes nothing", async () => {
    const st = bridge({ door: "d1", keys: ["/api/trust"], entries: [entry("/api/trust", { n: 7 })] });
    startReadStore();
    noteAnswerHeaders(headers({ "X-PLX-Door": "d1" })); // /api/me answered under the stored door
    storeMayDraw();
    await act(async () => undefined);
    noteAnswerHeaders(headers({ "X-PLX-Door": "d1" }));
    expect(keptRead("/api/trust")).toEqual({ n: 7 });
    noteAnswerHeaders(headers({}));
    expect(keptRead("/api/trust")).toEqual({ n: 7 });
    noteAnswerHeaders(headers({ "X-PLX-Door": "d2" }));
    expect(keptRead("/api/trust")).toBeNull();
    expect(st.wipe).toHaveBeenCalledTimes(1);
  });

  it("a refused session forgets everything before any body is used", async () => {
    const st = bridge({ door: "d1", keys: [], entries: [] });
    startReadStore();
    noteAnswerHeaders(headers({ "X-PLX-Door": "d1" })); // /api/me answered under the stored door
    storeMayDraw();
    await act(async () => undefined);
    keepRead("/api/a", { a: 1 });
    noteAnswerHeaders(headers({ "X-PLX-Session": "refused" }));
    expect(keptRead("/api/a")).toBeNull();
    expect(st.wipe).toHaveBeenCalled();
  });

  it("when the shell wiped itself the page forgets its copy without asking it to wipe again", async () => {
    const st = bridge({ door: "d1", keys: ["/api/trust"], entries: [entry("/api/trust", { n: 7 })] });
    startReadStore();
    noteAnswerHeaders(headers({ "X-PLX-Door": "d1" })); // /api/me answered under the stored door
    storeMayDraw();
    await act(async () => undefined);
    wiped();
    expect(keptRead("/api/trust")).toBeNull();
    expect(st.wipe).not.toHaveBeenCalled();
  });

  it("sign-out (the session reset) wipes the shell's store", async () => {
    const st = bridge({ door: "d1", keys: [], entries: [] });
    startReadStore();
    noteAnswerHeaders(headers({ "X-PLX-Door": "d1" })); // /api/me answered under the stored door
    storeMayDraw();
    await act(async () => undefined);
    resetSessionCaches();
    expect(st.wipe).toHaveBeenCalledTimes(1);
  });

  it("never draws a stored answer under a view-as, and keeps no view-as answer in the shell", async () => {
    const st = bridge({ door: "d1", keys: [], entries: [] });
    setViewingAs({ key: "0123456789abcdef0123", name: "Someone" });
    bridge({ door: "d1", keys: ["/api/trust"], entries: [entry("/api/trust", { n: 7 })] });
    resetReadStoreForTests();
    startReadStore();
    noteAnswerHeaders(headers({ "X-PLX-Door": "d1" })); // /api/me answered under the stored door
    storeMayDraw();
    await act(async () => undefined);
    expect(keptRead("/api/trust")).toBeNull();
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{\"a\":1}", { status: 200, headers: { "X-PLX-Door": "d1" } })));
    await getTrust();
    await new Promise((r) => setTimeout(r, 30));
    expect(st.put).not.toHaveBeenCalled();
  });

  it("an answer read from the server is handed to the shell with its door, text as sent; not when it is still reading, has no door, or is an identity read", async () => {
    const st = bridge({ door: null, keys: [], entries: [] });
    const answer = (body: string, door?: string) => vi.fn(async () => new Response(body, { status: 200, headers: door ? { "X-PLX-Door": door } : {} }));
    vi.stubGlobal("fetch", answer('{"a": 1}', "d9"));
    await getTrust();
    await vi.waitFor(() => expect(st.put).toHaveBeenCalledTimes(1));
    expect(st.put).toHaveBeenCalledWith(expect.objectContaining({ key: "/api/trust", body: '{"a": 1}', door: "d9", release: RELEASE }));
    vi.stubGlobal("fetch", answer('{"reading": true}', "d9"));
    await getTrust();
    vi.stubGlobal("fetch", answer('{"a": 2}'));
    await getTrust();
    await new Promise((r) => setTimeout(r, 50));
    expect(st.put).toHaveBeenCalledTimes(1);
  });

  it("a 404 drops the kept answer", async () => {
    const st = bridge({ door: null, keys: [], entries: [] });
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"error":{"code":"not_found"}}', { status: 404 })));
    await expect(getTrust()).rejects.toBeInstanceOf(ApiError);
    expect(st.drop).toHaveBeenCalledWith("/api/trust");
  });

  it("without a desktop bridge nothing is stored and nothing breaks", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"a":1}', { status: 200, headers: { "X-PLX-Door": "d" } })));
    await expect(getTrust()).resolves.toEqual({ a: 1 });
    startReadStore();
  });

  it("only the Workbench's reads are kept: never sign-in, identity, Ask or the event stream", () => {
    for (const p of ["/api/trust", "/api/object/PurchaseOrder/PO1?quick=1", "/api/meta/me-too"]) expect(keptPath(p), p).toBe(true);
    for (const p of ["/api/me", "/api/me?x=1", "/api/auth/config", "/api/ask", "/api/ask/stream", "/api/events", "/api/health", "/api/view-as/people", "/other"]) expect(keptPath(p), p).toBe(false);
  });

  it("the hot tier is bounded by bytes, oldest first, and one answer is never lost to the bound", () => {
    const big = Math.floor(MOST_KEPT_BYTES / 3);
    for (let i = 0; i < 5; i++) keepRead(`/api/b${i}`, { i }, true, big);
    expect(keptRead("/api/b0")).toBeNull();
    expect(keptRead("/api/b1")).toBeNull();
    expect(keptRead("/api/b4")).toEqual({ i: 4 });
    keepRead("/api/huge", { h: 1 }, true, MOST_KEPT_BYTES * 2);
    expect(keptRead("/api/huge")).toEqual({ h: 1 });
  });

  it("N = 1, 10, 60 kept answers: every one comes back and a lookup stays flat", () => {
    for (const n of [1, 10, 60, 5000]) {
      resetSessionCaches();
      for (let i = 0; i < n; i++) keepRead(`/api/k${i}`, { i }, true, 1000);
      let lost = 0;
      const t0 = performance.now();
      for (let i = 0; i < n; i++) if ((keptRead<{ i: number }>(`/api/k${i}`)?.i ?? -1) !== i) lost++;
      expect(lost).toBe(0);
      expect(performance.now() - t0).toBeLessThan(50 + n / 10);
    }
  });
});
