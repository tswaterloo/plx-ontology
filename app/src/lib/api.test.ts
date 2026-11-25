import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setBundlePrincipal, ask, ASK_STALL_MS, getApprovals, onHomeBundleFresh, startHomeBundle, IDLE_STALL_MS, getLateList, getMe, getOpsAlarms, getObject, getSuggestions, prefetchRecord, streamInsights, getInsight, saveInsight, logout, getObjectSection, getSource, bizPage, mergeSection, READING_MOST_MS, READING_POLL_MS, readingDelay, setTokenProvider, PANE_REUSE_MS } from "./api";
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

  it("sends a record's handle, chain and id whole: a cut identifier would name another record", async () => {
    const f = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", f);
    const h = "sage://customer/" + "x".repeat(900);
    await bizPage("customer", { h, chain: "c".repeat(300) });
    await bizPage("purchase_order", { record: "PurchaseOrder:" + "y".repeat(900) });
    const urls = (f.mock.calls as unknown as [string][]).map((c) => new URL(c[0], "http://x"));
    expect(urls[0].searchParams.get("h")).toBe(h);
    expect(urls[0].searchParams.get("chain")).toHaveLength(300);
    expect(urls[1].searchParams.get("record")).toHaveLength(914);
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
    expect(readingDelay(20, 20_000)).toBe(READING_POLL_MS * 2);
    expect(readingDelay(20, 60_000)).toBe(READING_POLL_MS * 4);   // a long read: asked seldom
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

  it("a plain read asked twice at once is one request; asked again after it answered, a new one", async () => {
    let n = 0;
    const fetch = vi.fn(async () => new Response(JSON.stringify({ n: ++n }), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    const [a, b] = await Promise.all([getOpsAlarms(), getOpsAlarms()]);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(a).toEqual({ n: 1 });
    expect(b).toEqual({ n: 1 });
    // another path is its own read; the same path after the answer is read again (never stale)
    await getMe();
    await expect(getOpsAlarms()).resolves.toEqual({ n: 3 });
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it("a plain read: one caller's abort leaves the other's answer, the last one's stops the request", async () => {
    let seen: AbortSignal | undefined;
    let answer: (r: Response) => void = () => undefined;
    const fetch = vi.fn((_url: string, init?: RequestInit) => {
      seen = init?.signal ?? undefined;
      return new Promise<Response>((done) => (answer = done));
    });
    vi.stubGlobal("fetch", fetch);
    const left = new AbortController();
    const first = getOpsAlarms(left.signal).catch((e) => e);
    const second = getOpsAlarms();
    await Promise.resolve();
    left.abort();
    expect((await first as DOMException).name).toBe("AbortError");
    expect(seen?.aborted).toBe(false);
    answer(new Response(JSON.stringify({ ok: 1 }), { status: 200 }));
    await expect(second).resolves.toEqual({ ok: 1 });
    expect(fetch).toHaveBeenCalledTimes(1);
    const gone = new AbortController();
    const third = getOpsAlarms(gone.signal).catch((e) => e);
    await Promise.resolve();
    gone.abort();
    expect((await third as DOMException).name).toBe("AbortError");
    expect(seen?.aborted).toBe(true);
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

describe("a read the server never answers", () => {
  /** A fetch that only ends when its signal aborts (a frozen or restarting server). */
  const hanging = vi.fn((_path: string, init?: RequestInit) =>
    new Promise<Response>((_ok, fail) => {
      const s = init?.signal;
      if (s?.aborted) return fail(s.reason);
      s?.addEventListener("abort", () => fail(s.reason));
    }),
  );

  afterEach(() => vi.restoreAllMocks());

  it("a request nothing arrives for in IDLE_STALL_MS fails as a typed 'connection stalled', never as empty data; a slow answer is never cut", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", hanging);
    const got = getSuggestions().catch((e) => e);
    await vi.advanceTimersByTimeAsync(IDLE_STALL_MS - 1000);   // far past the old 90 s: still waiting
    vi.stubGlobal("fetch", hanging);
    await vi.advanceTimersByTimeAsync(2000);
    const err = await got;
    expect(err).toBeInstanceOf(ApiError);
    expect([err.status, err.code]).toEqual([504, "connection_stalled"]);
    vi.useRealTimers();
  });

  it("a body that stops arriving stalls the same way; chunks keep it alive past the limit in total", async () => {
    vi.useFakeTimers();
    const enc = new TextEncoder();
    let push: (s: string) => void = () => undefined;
    let fail: (e: unknown) => void = () => undefined;
    vi.stubGlobal("fetch", vi.fn((_p: string, init?: RequestInit) => {
      const body = new ReadableStream<Uint8Array>({
        start(c) {
          push = (t) => c.enqueue(enc.encode(t));
          fail = (e) => c.error(e);
          init?.signal?.addEventListener("abort", () => c.error(init.signal!.reason));
        },
      });
      return Promise.resolve(new Response(body, { status: 200 }));
    }));
    const got = getSuggestions().catch((e) => e);
    push('{"items":');
    await vi.advanceTimersByTimeAsync(IDLE_STALL_MS - 5000);
    push('[],');
    await vi.advanceTimersByTimeAsync(IDLE_STALL_MS - 5000);   // 230 s in total, never 120 s idle
    push('"status":"read"}');
    fail = () => undefined;
    const done = await Promise.race([got, vi.advanceTimersByTimeAsync(1).then(() => "waiting")]);
    expect(done).toBe("waiting");   // body not closed yet: still open, not cut
    await vi.advanceTimersByTimeAsync(IDLE_STALL_MS + 1000);
    const err = await got;
    expect([err.status, err.code]).toEqual([504, "connection_stalled"]);
    vi.useRealTimers();
  });

  it("leaves the caller's own cancel as a cancel, and never cuts a write", async () => {
    vi.stubGlobal("fetch", hanging);
    const own = new AbortController();
    const pending = getSuggestions(own.signal).catch((e) => e);
    own.abort();
    const err = await pending;
    expect(err).not.toBeInstanceOf(ApiError);
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
    await expect(logout()).resolves.toBeUndefined();
  });
});

describe("an Ask stream the server stops feeding", () => {
  afterEach(() => vi.useRealTimers());

  it("is cut off after a minute of silence, keep-alives keep it open", async () => {
    vi.useFakeTimers();
    const enc = new TextEncoder();
    let push: ((s: string) => void) | undefined;
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        push = (t) => c.enqueue(enc.encode(t));
        push('event: status\ndata: {"step":"understanding","message":"Reading","elapsed_ms":0}\n\n');
      },
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body, { status: 200 })));
    const events: AskEvent[] = [];
    const run = (async () => {
      for await (const e of ask({ question: "q" })) events.push(e);
    })();
    await vi.advanceTimersByTimeAsync(ASK_STALL_MS - 1000);
    push!(": keep-alive\n\n");          // still being worked on: the wait starts again
    await vi.advanceTimersByTimeAsync(ASK_STALL_MS - 1000);
    expect(events.map((e) => e.event)).toEqual(["status"]);
    await vi.advanceTimersByTimeAsync(2000);
    await run;
    expect(events.map((e) => e.event)).toEqual(["status", "error"]);
    expect((events[1].data as { code: string }).code).toBe("stream_interrupted");
  });

  it("yields early charts SSE events before the final answer arrives", async () => {
    const chart = { kind: "bar", title: "Monthly revenue", x: "month", labels: ["Jan"], series: [{ name: "rev", values: [100] }], rows: 1, shown: 1 };
    const events = await collect([
      `event: charts\ndata: ${JSON.stringify({ charts: [chart] })}\n\n`,
      `event: answer\ndata: ${JSON.stringify({ answer_id: "a1", direct_answer: "Done." })}\n\n`,
    ]);
    expect(events.map((e) => e.event)).toEqual(["charts", "answer"]);
    expect(events[0].event === "charts" ? events[0].data.charts : []).toHaveLength(1);
  });
});


describe("a record pane's reads (Home's detail)", () => {
  afterEach(() => {
    resetSessionCaches();
    vi.useRealTimers();
  });
  const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });

  it("reuses an answer read moments ago, never a stale one, nothing after a write, and reads again later", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn(async (path: string) =>
      String(path).includes("stale") ? json({ section: "trail", stale_since: "2026-11-21T10:00:00Z" }) : json({ section: "trail", n: fetch.mock.calls.length }));
    vi.stubGlobal("fetch", fetch);
    const a = await getObjectSection("trail", "PurchaseOrder", "PurchaseOrder:x:1", undefined, true);
    const b = await getObjectSection("trail", "PurchaseOrder", "PurchaseOrder:x:1", undefined, true);
    expect(b).toEqual(a);
    expect(fetch).toHaveBeenCalledTimes(1);
    // a record page's own section read is never served from the pane's copy
    await getObjectSection("trail", "PurchaseOrder", "PurchaseOrder:x:1");
    expect(fetch).toHaveBeenCalledTimes(2);
    // a stale answer is asked for again
    await getObjectSection("trail", "PurchaseOrder", "PurchaseOrder:stale:1", undefined, true);
    await getObjectSection("trail", "PurchaseOrder", "PurchaseOrder:stale:1", undefined, true);
    expect(fetch).toHaveBeenCalledTimes(4);
    // any write drops what was kept
    await logout().catch(() => undefined);
    await getObjectSection("trail", "PurchaseOrder", "PurchaseOrder:x:1", undefined, true);
    const afterWrite = fetch.mock.calls.length;
    await getObjectSection("trail", "PurchaseOrder", "PurchaseOrder:x:1", undefined, true);
    expect(fetch.mock.calls.length).toBe(afterWrite);
    vi.advanceTimersByTime(PANE_REUSE_MS + 1);
    await getObjectSection("trail", "PurchaseOrder", "PurchaseOrder:x:1", undefined, true);
    expect(fetch.mock.calls.length).toBe(afterWrite + 1);
  });
});

const settle = () => new Promise((r) => setTimeout(r, 20));
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("a record read ahead on hover", () => {
  it("is read once, taken by the page's own read, and a second open reads again", async () => {
    resetSessionCaches();
    const f = vi.fn(async () => json({ type: "PurchaseOrder", object_id: "1", complete: true }));
    vi.stubGlobal("fetch", f);
    prefetchRecord("PurchaseOrder", "po 1");
    prefetchRecord("PurchaseOrder", "po 1");   // hovering again asks nothing more
    await settle();
    expect(f).toHaveBeenCalledTimes(2);        // the core read and the quick read, once each
    await getObject("PurchaseOrder", "po 1", undefined, true);
    expect(f).toHaveBeenCalledTimes(2);        // the page took the read already made
    await getObject("PurchaseOrder", "po 1", undefined, true);
    expect(f).toHaveBeenCalledTimes(3);        // a read ahead stands for one open only
  });

  it("is dropped by a write and by a sign-out, and a malformed type asks nothing", async () => {
    const f = vi.fn(async () => json({ complete: true, id: "x" }));
    vi.stubGlobal("fetch", f);
    prefetchRecord("../x", "1");
    prefetchRecord("PurchaseOrder", "");
    expect(f).not.toHaveBeenCalled();
    prefetchRecord("PurchaseOrder", "a");
    await settle();
    await saveInsight({ title: "t", question: "q", sql: "SELECT 1" } as never).catch(() => undefined);
    const before = f.mock.calls.length;
    await getObject("PurchaseOrder", "a", undefined, true);
    expect(f.mock.calls.length).toBe(before + 1);   // read again: nothing read before a write is reused
    await settle();
    prefetchRecord("PurchaseOrder", "b");
    await settle();
    resetSessionCaches();
    const mid = f.mock.calls.length;
    await getObject("PurchaseOrder", "b", undefined, true);
    expect(f.mock.calls.length).toBe(mid + 1);
  });
});

describe("the one-request dashboard", () => {
  it("hands each line over as it arrives, across chunk boundaries", async () => {
    const lines = [{ list: { scope: "self", items: [], measures: [] } }, { id: "a", result: { id: "a" } }, { id: "b", error: { code: "insight_not_ready", status: 503 } }];
    const text = lines.map((l) => JSON.stringify(l)).join("\n") + "\n";
    const cut = Math.floor(text.length / 2);
    const enc = new TextEncoder();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new ReadableStream<Uint8Array>({ start(c) { c.enqueue(enc.encode(text.slice(0, cut))); c.enqueue(enc.encode(text.slice(cut))); c.close(); } }), { status: 200 })));
    const got: unknown[] = [];
    await streamInsights((l) => got.push(l));
    expect(got).toEqual(lines);
  });
});

describe("Home's one-round-trip bundle", () => {
  const ok = (body: unknown) => ({ status: 200, body });
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  beforeEach(() => setBundlePrincipal("alice"));
  afterEach(() => {
    resetSessionCaches();
    setBundlePrincipal(null);
    window.sessionStorage.clear();
  });
  const kept = (principal: string, as = "", parts: unknown = { suggestions: ok({ status: "read", items: [{ old: true }] }) }) =>
    JSON.stringify({ at: Date.now(), principal, as, parts });
  const neverReaches = () => vi.fn(async () => new Response(JSON.stringify({ parts: { suggestions: ok({ status: "read", items: [{ fresh: true }] }) } }), { status: 200, headers: { "Content-Type": "application/json" } }));

  it("answers the reads it lists from one request, each with its own status", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url !== "/api/home/bundle") throw new Error("a read made on its own: " + url);
      return json({ parts: {
        suggestions: ok({ status: "read", items: [{ x: 1 }] }),
        late: ok({ items: [], total: 0 }),
        approvals: { status: 503, body: { error: { code: "busy", message: "The Workbench is busy right now. Try again in a moment." } } },
      } });
    });
    vi.stubGlobal("fetch", fetchMock);
    startHomeBundle();
    startHomeBundle();   // once
    const [sug, late, appr] = await Promise.allSettled([getSuggestions(), getLateList(0, undefined, 100), getApprovals()]);
    expect(sug).toMatchObject({ status: "fulfilled", value: { items: [{ x: 1 }] } });
    expect(late.status).toBe("fulfilled");
    expect(appr.status).toBe("rejected");
    expect((appr as PromiseRejectedResult).reason).toBeInstanceOf(ApiError);
    expect((appr as PromiseRejectedResult).reason).toMatchObject({ status: 503, code: "busy" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("reads each part on its own when the bundle fails or lacks it, and never for a part that is still being read", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      calls.push(url);
      if (url === "/api/home/bundle") return json({ parts: { suggestions: ok({ reading: true }) } });
      return json({ status: "read", items: [] });
    }));
    startHomeBundle();
    await getSuggestions();   // reading in the bundle: asks for itself
    await getApprovals();     // not in the bundle: asks for itself
    expect(calls).toEqual(["/api/home/bundle", "/api/suggestions", "/api/approvals"]);
    resetSessionCaches();
    calls.length = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      calls.push(url);
      if (url === "/api/home/bundle") return json({ error: { code: "internal" } }, 500);
      return json({ status: "read", items: [] });
    }));
    startHomeBundle();
    await getSuggestions();
    expect(calls).toEqual(["/api/home/bundle", "/api/suggestions"]);
  });

  it("shows the last kept bundle at once, then the fresh one, and never one read twice from the network", async () => {
    window.sessionStorage.setItem("plx.home-bundle ", JSON.stringify({ at: Date.now(), principal: "alice", as: "", parts: { suggestions: ok({ status: "read", items: [{ old: true }] }) } }));
    let release: () => void = () => undefined;
    const gate = new Promise<void>((r) => (release = r));
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      calls.push(url);
      await gate;
      return json({ parts: { suggestions: ok({ status: "read", items: [{ fresh: true }] }) } });
    }));
    const fresh = vi.fn();
    onHomeBundleFresh(fresh);
    startHomeBundle();
    expect(await getSuggestions()).toMatchObject({ items: [{ old: true }] });   // before the answer
    release();
    await vi.waitFor(() => expect(fresh).toHaveBeenCalledTimes(1));
    expect(await getSuggestions()).toMatchObject({ items: [{ fresh: true }] });
    expect(calls).toEqual(["/api/home/bundle"]);
    // kept for the next page load: the fresh one
    expect(JSON.parse(window.sessionStorage.getItem("plx.home-bundle ") ?? "{}").parts.suggestions.body.items).toEqual([{ fresh: true }]);
  });

  it("never shows a bundle kept for someone else or too long ago, and keeps no error", async () => {
    window.sessionStorage.setItem("plx.home-bundle ", JSON.stringify({ at: Date.now() - 31 * 60 * 1000, principal: "alice", as: "", parts: { suggestions: ok({ items: [{ old: true }] }) } }));
    vi.stubGlobal("fetch", vi.fn(async () => json({ parts: { suggestions: { status: 503, body: { error: { code: "busy" } } } } })));
    startHomeBundle();
    await expect(getSuggestions()).rejects.toMatchObject({ status: 503 });
    expect(JSON.parse(window.sessionStorage.getItem("plx.home-bundle ") ?? "{}").parts).toEqual({});
    resetSessionCaches();   // sign-out / another person: nothing kept
    expect(window.sessionStorage.getItem("plx.home-bundle ")).toBeNull();
  });

  it("never shows person A's kept bundle to person B on one tab, nor after a reload that finds nobody signed in", async () => {
    window.sessionStorage.setItem("plx.home-bundle ", kept("alice"));
    // a reload: the auth layer learns the principal first. B signs in on the same tab:
    setBundlePrincipal("bob");
    expect(window.sessionStorage.getItem("plx.home-bundle ")).toBeNull();   // dropped at once
    vi.stubGlobal("fetch", neverReaches());
    startHomeBundle();
    expect(await getSuggestions()).toMatchObject({ items: [{ fresh: true }] });   // never { old: true }
    // the session expired on the reload (401 on /api/me): nobody known, nothing kept stays
    window.sessionStorage.setItem("plx.home-bundle ", kept("bob"));
    setBundlePrincipal(null);
    expect(window.sessionStorage.getItem("plx.home-bundle ")).toBeNull();
    resetSessionCaches();
    vi.stubGlobal("fetch", neverReaches());
    startHomeBundle();   // no principal: never shown, never written
    await getSuggestions();
    expect(window.sessionStorage.getItem("plx.home-bundle ")).toBeNull();
  });

  it("keeps nothing from A's request that is still in flight when B signs in", async () => {
    let answer: (r: Response) => void = () => {};
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((res) => { answer = res; })));
    startHomeBundle();                 // A's request goes out
    setBundlePrincipal("bob");         // B signs in on the tab before it answers
    answer(json({ parts: { suggestions: ok({ status: "read", items: [{ aliceOnly: true }] }) } }));
    await Promise.resolve(); await new Promise((r) => setTimeout(r, 0));
    expect(window.sessionStorage.getItem("plx.home-bundle ")).toBeNull();   // not stored under B
    vi.stubGlobal("fetch", neverReaches());
    startHomeBundle();                 // B's own request: only B's answer is shown
    expect(await getSuggestions()).toMatchObject({ items: [{ fresh: true }] });
  });

  it("keeps a record that names another principal or view-as target out even if it is in the storage", async () => {
    window.sessionStorage.setItem("plx.home-bundle ", kept("bob"));        // a leftover written by someone else
    vi.stubGlobal("fetch", neverReaches());
    startHomeBundle();   // alice: the kept one is bob's, never shown
    expect(await getSuggestions()).toMatchObject({ items: [{ fresh: true }] });
    resetSessionCaches();
    setBundlePrincipal("alice");
    window.sessionStorage.setItem("plx.home-bundle ", kept("alice", "someone-else"));   // read while viewing as another
    vi.stubGlobal("fetch", neverReaches());
    startHomeBundle();
    expect(await getSuggestions()).toMatchObject({ items: [{ fresh: true }] });
    // and the kept record Alice's own fresh read leaves names her and no view-as target
    const own = JSON.parse(window.sessionStorage.getItem("plx.home-bundle ") ?? "{}");
    expect([own.principal, own.as]).toEqual(["alice", ""]);
  });
});


describe("getInsight polls a tile that is not stored yet", () => {
  const stored = (read_at: string) => new Response(JSON.stringify({ id: "t", read_at }), { status: 200, headers: { "Content-Type": "application/json" } });
  const filling = () => new Response(JSON.stringify({ error: { code: "insight_filling", message: "being prepared" } }), { status: 425, headers: { "Content-Type": "application/json" } });
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("asks again every few seconds until stored, and not at all while the page is hidden", async () => {
    const calls: number[] = [];
    vi.stubGlobal("fetch", vi.fn(async () => { calls.push(1); return calls.length < 3 ? filling() : stored("2026-11-24T10:00:00Z"); }));
    let hidden = true;
    Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
    const got = getInsight("t");
    await vi.advanceTimersByTimeAsync(0);
    expect(calls.length).toBe(1);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(calls.length).toBe(1);               // hidden: no poll at all
    hidden = false;
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(10_000);
    expect(calls.length).toBe(3);
    expect((await got).read_at).toBe("2026-11-24T10:00:00Z");
  });

  it("a Refresh draws the stored value, then waits for a newer one", async () => {
    const reads = ["2026-11-24T10:00:00Z", "2026-11-24T10:00:00Z", "2026-11-24T10:05:00Z"];
    vi.stubGlobal("fetch", vi.fn(async () => stored(reads.shift() as string)));
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    const seen: string[] = [];
    const got = getInsight("t", undefined, true, (old) => seen.push(old.read_at));
    await vi.advanceTimersByTimeAsync(10_000);
    expect(seen).toEqual(["2026-11-24T10:00:00Z"]);
    expect((await got).read_at).toBe("2026-11-24T10:05:00Z");
  });
});
