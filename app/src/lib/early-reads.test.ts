import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getActionsLog, getAuthConfig, getMe, getObject, getObjectCore, getRecordViews, getSidebar, getTrust, getUpcomingMeetings, setTokenProvider } from "./api";
import { resetSessionCaches } from "./session-cache";
import { EARLY_READS_SCRIPT, TICKET_BOOT_SCRIPT } from "./ticket-boot";

type W = { __plxEarly?: unknown; __plxTicket?: string };
const w = window as unknown as W;

function answer(status: number, body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

/** fetch answering /api/auth/config and /api/me; records every path asked. */
function serve(me: { status: number; body: unknown }) {
  const asked: string[] = [];
  const f = vi.fn((input: RequestInfo | URL) => {
    const path = String(input);
    asked.push(path);
    if (path === "/api/auth/config") return answer(200, { mode: "dev", dev_login: true });
    if (path === "/api/me") return answer(me.status, me.body);
    return answer(404, {});
  });
  vi.stubGlobal("fetch", f);
  return asked;
}

function runHead() {
  new Function(TICKET_BOOT_SCRIPT)();
  new Function(EARLY_READS_SCRIPT)();
}

beforeEach(() => {
  delete w.__plxEarly;
  delete w.__plxTicket;
  setTokenProvider(null);
  window.history.replaceState(null, "", "/help");
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("the sign-in check's reads started by the head script", () => {
  it("are sent before the app loads and answer the app's first ask without a second request", async () => {
    const asked = serve({ status: 200, body: { principal: "plx:staff:ana" } });
    runHead();
    expect(asked).toEqual(["/api/auth/config", "/api/me", "/api/sidebar"]);
    expect((await getAuthConfig()).mode).toBe("dev");
    expect((await getMe()).principal).toBe("plx:staff:ana");
    expect(asked).toHaveLength(3);
    // used once: the next ask reads again
    await getMe();
    expect(asked).toEqual(["/api/auth/config", "/api/me", "/api/sidebar", "/api/me"]);
  });

  it("are never sent while an access link's ticket is handed over (the sign-in may change who reads)", () => {
    const asked = serve({ status: 200, body: { principal: "plx:staff:old" } });
    window.history.replaceState(null, "", "/records#ticket=abc");
    runHead();
    expect(asked).toEqual([]);
    expect(w.__plxEarly).toBeUndefined();
  });

  it("a refused early read is not the answer: the app reads again and gets the server's own error", async () => {
    const asked = serve({ status: 401, body: { error: { code: "sign_in_required" } } });
    runHead();
    await expect(getMe()).rejects.toMatchObject({ status: 401 });
    expect(asked.filter((p) => p === "/api/me")).toHaveLength(2);
  });

  it("with a Microsoft token (Entra mode) the cookie-only early answer is thrown away", async () => {
    const asked = serve({ status: 200, body: { principal: "plx:staff:cookie" } });
    runHead();
    setTokenProvider(async () => "tok");
    await getMe();
    expect(asked.filter((p) => p === "/api/me")).toHaveLength(2);
  });

  it("an old early answer, or one from before a sign-out, is never used", async () => {
    const asked = serve({ status: 200, body: { principal: "plx:staff:ana" } });
    const now = Date.now();
    runHead();
    vi.spyOn(Date, "now").mockReturnValue(now + 11_000);
    await getAuthConfig();
    expect(asked.filter((p) => p === "/api/auth/config")).toHaveLength(2);
    vi.restoreAllMocks();
    serve({ status: 200, body: { principal: "plx:staff:ana" } });
    runHead();
    resetSessionCaches();
    expect(w.__plxEarly).toBeUndefined();
  });
});

describe("the page's main read started by the head script (W1)", () => {
  it("is sent with the sign-in reads, taken once by the page's own read with the same URL, and the next ask reads again", async () => {
    const asked: string[] = [];
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      asked.push(String(input));
      return answer(200, String(input) === "/api/me" ? { principal: "p" } : { configured: true });
    }));
    window.history.replaceState(null, "", "/data-health");
    runHead();
    expect(asked).toEqual(["/api/auth/config", "/api/me", "/api/sidebar", "/api/trust", "/api/data-health"]);
    await getTrust();
    await getSidebar();
    expect(asked).toHaveLength(5);
    await getTrust();
    expect(asked.filter((p) => p === "/api/trust")).toHaveLength(2);
  });

  it("an unmapped page starts only the sidebar; a refused early read is read again", async () => {
    const asked: string[] = [];
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      asked.push(String(input));
      return String(input) === "/api/trust" && asked.filter((p) => p === "/api/trust").length === 1 ? answer(503, {}) : answer(200, { ok: 1 });
    }));
    window.history.replaceState(null, "", "/data-health");
    runHead();
    await getTrust();
    expect(asked.filter((p) => p === "/api/trust")).toHaveLength(2);
  });
});

describe("every mapped page's and a record's reads start with the HTML and are taken by the page's own fetch", () => {
  function recorder() {
    const asked: string[] = [];
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      asked.push(String(input));
      return answer(200, { ok: 1 });
    }));
    return asked;
  }
  it.each([
    ["/records", "/api/records/views", () => getRecordViews()],
    ["/activity", "/api/actions/log", () => getActionsLog()],
    ["/meetings", "/api/brief/meetings", () => getUpcomingMeetings()],
  ])("%s starts %s once", async (path, url, read) => {
    const asked = recorder();
    window.history.replaceState(null, "", path);
    runHead();
    expect(asked).toContain(url);
    await read();
    expect(asked.filter((p) => p === url)).toHaveLength(1);
  });

  it("a record page starts its core and quick reads with the HTML; the page's reads send nothing more", async () => {
    const asked = recorder();
    window.history.replaceState(null, "", "/object/PurchaseOrder/PurchaseOrder%3Aportal%20x");
    runHead();
    const base = "/api/object/PurchaseOrder/" + encodeURIComponent("PurchaseOrder:portal x");
    expect(asked).toEqual(expect.arrayContaining([`${base}?quick=1&core=1`, `${base}?quick=1`]));
    const before = asked.length;
    await getObjectCore("PurchaseOrder", "PurchaseOrder:portal x");
    await getObject("PurchaseOrder", "PurchaseOrder:portal x", undefined, true);
    expect(asked).toHaveLength(before);
  });

  it("a record route with a malformed type or a long id starts nothing for the record", () => {
    const asked = recorder();
    window.history.replaceState(null, "", "/object/bad%20type/x");
    runHead();
    expect(asked.some((p) => p.startsWith("/api/object/"))).toBe(false);
    window.history.replaceState(null, "", "/object/Customer/" + "a".repeat(701));
    runHead();
    expect(asked.some((p) => p.startsWith("/api/object/"))).toBe(false);
  });

  it("Data health starts its records read beside the trust read", () => {
    const asked = recorder();
    window.history.replaceState(null, "", "/data-health");
    runHead();
    expect(asked).toEqual(expect.arrayContaining(["/api/trust", "/api/data-health"]));
  });
});
