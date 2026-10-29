import { afterEach, describe, expect, it, vi } from "vitest";
import { ask, getObject, getSource, setTokenProvider } from "./api";
import { ApiError } from "./errors";
import type { AskEvent } from "./types";

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
