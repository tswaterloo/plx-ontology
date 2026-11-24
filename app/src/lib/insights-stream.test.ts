import { afterEach, describe, expect, it, vi } from "vitest";
import { streamInsights, type InsightsLine } from "./api";
import { resetSessionCaches } from "./session-cache";

afterEach(() => {
  vi.unstubAllGlobals();
  resetSessionCaches();
});

function slowStream() {
  let push: (s: string) => void = () => undefined;
  let close: () => void = () => undefined;
  let signalSeen: AbortSignal | undefined;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      const enc = new TextEncoder();
      push = (s) => c.enqueue(enc.encode(s));
      close = () => c.close();
    },
  });
  const f = vi.fn((_u: string, init?: RequestInit) => {
    signalSeen = init?.signal ?? undefined;
    return Promise.resolve(new Response(body, { status: 200 }));
  });
  vi.stubGlobal("fetch", f);
  return { f, push: (s: string) => push(s), close: () => close(), aborted: () => !!signalSeen?.aborted };
}

describe("the insights stream is never cancelled by leaving the page", () => {
  it("leaving stops listening only; a page opened meanwhile gets the lines already read, from the same request", async () => {
    const s = slowStream();
    const a: InsightsLine[] = [];
    const leave = new AbortController();
    const first = streamInsights((l) => a.push(l), leave.signal).catch((e) => e);
    await vi.waitFor(() => expect(s.f).toHaveBeenCalledTimes(1));
    s.push('{"list":{"items":[]}}\n');
    await vi.waitFor(() => expect(a).toHaveLength(1));
    leave.abort();
    await first;
    expect(s.aborted()).toBe(false);
    s.push('{"id":"x","result":{"id":"x"}}\n');
    const b: InsightsLine[] = [];
    const second = streamInsights((l) => b.push(l));
    expect(b).toHaveLength(1);   // the list, replayed
    await vi.waitFor(() => expect(b).toHaveLength(2));
    s.close();
    await second;
    expect(s.f).toHaveBeenCalledTimes(1);
    expect(a).toHaveLength(1);
  });
});
