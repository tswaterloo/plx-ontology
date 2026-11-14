import { afterEach, describe, expect, it, vi } from "vitest";
import { ask, getLateList, getObject, getSuggestions, logout, getObjectSection, getSource, mergeSection, READING_MOST_MS, READING_POLL_MS, readingDelay, setTokenProvider } from "./api";
import { ApiError } from "./errors";
import { resetSessionCaches } from "./session-cache";
import type { AskEvent, ObjectPage, ObjectSection } from "./types";

function streamResponse(chunks: string[], status = 200): Response {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      chunks.forEach((s) => c.enqueue(enc.encode(s)));
      c.close();
    },
  });
  return new Response(body, { status, headers: { "Content-Type": "text/event-stream" } });
}

async function collect(chunks: string[]): Promise<AskEvent[]> {
  vi.stubGlobal("fetch", vi.fn(async () => streamResponse(chunks)));
  const out: AskEvent[] = [];
  for await (const e of ask({ question: "q" })) out.push(e);
  return out;
}

afterEach(() => vi.unstubAllGlobals());

describe("ask stream", () => {
  it("yields typed events in order and stops at the answer", async () => {
    const evs = await collect([
      'event: status\ndata: {"step":"s","message":"Searching"}\n\n',
      'event: token\ndata: {"text":"Hi"}\n\n',
      'event: answer\ndata: {"answer_id":"a1","direct_answer":"Hi","facts":[],"not_found":[],"hidden":{"count":0,"by_owner":[]},"conflicts":[],"freshness":{"as_of":"2026-10-29T10:00:00Z"}}\n\n',
      'event: token\ndata: {"text":"after the answer"}\n\n',
    ]);
    expect(evs.map((e) => e.event)).toEqual(["status", "token", "answer"]);
  });

  it("drops unknown event names instead of rendering them", async () => {
    const evs = await collect([
      'event: debug\ndata: {"sql":"select *"}\n\n',
      'event: token\ndata: {"text":"x"}\n\n',
      'event: error\ndata: {"code":"timeout"}\n\n',
    ]);
    expect(evs.map((e) => e.event)).toEqual(["token", "error"]);
  });

  it("turns malformed data into a plain stream error", async () => {
    const evs = await collect(['event: token\ndata: {"text":"x"}\n\n', "event: answer\ndata: {not json\n\n"]);
    expect(evs[evs.length - 1]).toMatchObject({ event: "error", data: { code: "stream_interrupted" } });
    expect(evs.some((e) => e.event === "answer")).toBe(false);
  });

  it("reports a stream that ends without an answer as interrupted", async () => {
    const evs = await collect(['event: token\ndata: {"text":"half"}\n\n']);
    expect(evs.map((e) => e.event)).toEqual(["token", "error"]);
  });

  it("raises an ApiError with the server code on a non-2xx", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ error: { code: "sign_in_required", message: "Please sign in." } }), { status: 401 })),
    );
    await expect(ask({ question: "q" }).next()).rejects.toMatchObject({ status: 401, code: "sign_in_required" });
  });
});

describe("getObject", () => {
  it("fails closed on a malformed type or id without calling the API", async () => {
    const f = vi.fn();
    vi.stubGlobal("fetch", f);
    for (const t of ["../me", "Purchase Order", "", "1po", "x".repeat(61), "po/../../auth"]) {
      await expect(getObject(t, "1")).rejects.toBeInstanceOf(ApiError);
    }
    await expect(getObject("PurchaseOrder", "")).rejects.toBeInstanceOf(ApiError);
    await expect(getObject("PurchaseOrder", "x".repeat(701))).rejects.toBeInstanceOf(ApiError);
    expect(f).not.toHaveBeenCalled();
  });

  it("encodes the id so it cannot change the path", async () => {
    const f = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", f);
    await getObject("PurchaseOrder", "../../auth/logout");
    expect((f.mock.calls[0] as unknown[])[0]).toBe("/api/object/PurchaseOrder/..%2F..%2Fauth%2Flogout");
  });
});

describe("page sections", () => {
  it("fails closed on an unknown section, type or id without calling the API, and encodes the id", async () => {
    const f = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", f);
    await expect(getObjectSection("hidden" as never, "PurchaseOrder", "1")).rejects.toBeInstanceOf(ApiError);
    await expect(getObjectSection("people", "../me", "1")).rejects.toBeInstanceOf(ApiError);
    await expect(getObjectSection("people", "PurchaseOrder", "")).rejects.toBeInstanceOf(ApiError);
    expect(f).not.toHaveBeenCalled();
    await getObjectSection("trail", "PurchaseOrder", "../../auth/logout");
    expect((f.mock.calls[0] as unknown[])[0]).toBe("/api/object-section/trail/PurchaseOrder/..%2F..%2Fauth%2Flogout");
  });

  it("merges each section into its own record only, as the full page carries it", () => {
    const page = { object_id: "PO:1", status: { state: "open", text: "Open", sources: [] }, late_lines: [], not_found: [{ what: "Receipt" }] } as unknown as ObjectPage;
    const late = { object_id: "PO:1", section: "late", applies: true, status: { state: "late", text: "Late by 3 days", sources: [] }, late_lines: [{ line: "L1", sources: [] }] } as unknown as ObjectSection;
    const merged = mergeSection(page, late);
    expect(merged.status.state).toBe("late");
    expect(merged.late_lines).toHaveLength(1);
    // a late check that established nothing keeps the quick read's status
    expect(mergeSection(page, { ...late, status: null }).status.state).toBe("open");
    // a section of another record changes nothing
    expect(mergeSection(page, { ...late, object_id: "PO:2" })).toBe(page);
    const trail = { object_id: "PO:1", section: "trail", applies: true, late_lines: [], trail: null, not_found: [{ what: "Receipt (2 messages mention it)" }] } as unknown as ObjectSection;
    expect(mergeSection(page, trail).not_found?.[0].what).toMatch(/mention/);
    expect(mergeSection(page, { ...trail, not_found: null }).not_found).toBe(page.not_found);
    // the page's drafts come with its trail section
    const drafts = [{ id: "chase:PO:1", kind: "vendor_chase", title: "Chase", sent: false }];
    expect(mergeSection(page, { ...trail, proposals: drafts } as unknown as ObjectSection).proposals).toEqual(drafts);
    expect(mergeSection(page, trail).proposals).toBe(page.proposals);
  });
});

describe("identity on every call", () => {
  afterEach(() => setTokenProvider(null));

  it("sends the Microsoft access token as Bearer on JSON calls and the ask stream", async () => {
    const f = vi.fn(async (url: string) =>
      url === "/api/ask" ? streamResponse(['event: error\ndata: {"code":"timeout","message":""}\n\n']) : new Response("{}", { status: 200 }),
    );
    vi.stubGlobal("fetch", f);
    setTokenProvider(async () => "tok-123");
    await getSource("bc://x/1");
    for await (const _ of ask({ question: "q" })) void _;
    for (const call of f.mock.calls as unknown as [string, RequestInit][]) {
      expect((call[1].headers as Record<string, string>).Authorization).toBe("Bearer tok-123");
    }
  });

  it("sends no Authorization header in cookie (dev) mode", async () => {
    const f = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", f);
    await getSource("bc://x/1");
    const init = (f.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
    expect(init.credentials).toBe("same-origin");
  });
});

describe("logout (WEB-014)", () => {
  const status = (n: number) => vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { code: "x" } }), { status: n, headers: { "Content-Type": "application/json" } })));
  it("fails when the server could not end the session, and counts an already-ended one as done", async () => {
    status(500);
    await expect(logout()).rejects.toBeInstanceOf(ApiError);
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network"); }));
    await expect(logout()).rejects.toBeInstanceOf(TypeError);
    status(401);
    await expect(logout()).resolves.toBeUndefined();
    status(200);
    await expect(logout()).resolves.toBeUndefined();
  });
});

describe("Today lists still being read", () => {
  afterEach(() => vi.useRealTimers());

  it("asks again until the page is there and never hands back the empty placeholder", async () => {
    vi.useFakeTimers();
    const answers = [
      { status: "unavailable", total: 0, items: [], reading: true },
      { status: "unavailable", total: 0, items: [], reading: true },
      { status: "read", total: 2, items: [{ id: "a" }, { id: "b" }] },
    ];
    const fetch = vi.fn(async () => new Response(JSON.stringify(answers.shift()), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    const got = getSuggestions();
    await vi.advanceTimersByTimeAsync(READING_POLL_MS * 3);
    await expect(got).resolves.toMatchObject({ status: "read", total: 2 });
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it("asks again soon at first, then no more often than READING_POLL_MS (twice that on a long read)", () => {
    expect(readingDelay(0, 0)).toBeLessThan(READING_POLL_MS);
    expect(readingDelay(1, 500)).toBeGreaterThan(readingDelay(0, 0));
    expect(readingDelay(20, 5_000)).toBe(READING_POLL_MS);
    expect(readingDelay(20, 60_000)).toBe(READING_POLL_MS * 2);
  });

  it("shares one request between callers of the same read, and one caller's abort leaves the other's", async () => {
    vi.useFakeTimers();
    const answers = [
      { status: "unavailable", total: 0, items: [], reading: true },
      { status: "read", total: 1, items: [{ id: "a" }] },
    ];
    const fetch = vi.fn(async () => new Response(JSON.stringify(answers.shift()), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    const left = new AbortController();
    const first = getSuggestions(left.signal).catch((e) => e);
    const second = getSuggestions();
    left.abort();
    await vi.advanceTimersByTimeAsync(READING_POLL_MS * 2);
    expect((await first as DOMException).name).toBe("AbortError");
    await expect(second).resolves.toMatchObject({ status: "read", total: 1 });
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("a read under way when the person changes is never shared with the next caller", async () => {
    vi.useFakeTimers();
    const answers = [
      { status: "unavailable", total: 0, items: [], reading: true },
      { status: "read", total: 1, items: [{ id: "theirs" }] },
    ];
    const fetch = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.signal?.aborted) throw new DOMException("aborted", "AbortError");
      return new Response(JSON.stringify(answers.shift()), { status: 200 });
    });
    vi.stubGlobal("fetch", fetch);
    const before = getSuggestions().catch((e) => e);
    await vi.advanceTimersByTimeAsync(0); // the first person's read is under way, waiting to ask again
    resetSessionCaches();
    const after = getSuggestions();
    await vi.advanceTimersByTimeAsync(READING_POLL_MS * 2);
    expect((await before as DOMException).name).toBe("AbortError");
    await expect(after).resolves.toMatchObject({ items: [{ id: "theirs" }] });
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("still reading after the most it waits: the calm busy notice", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ total: 0, offset: 0, items: [], reading: true }), { status: 200 })));
    const got = getLateList(0).catch((e) => e);
    await vi.advanceTimersByTimeAsync(READING_MOST_MS + READING_POLL_MS * 2);
    const err = await got;
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("busy");
  });
});
