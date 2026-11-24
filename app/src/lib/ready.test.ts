import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { resetReady, usePageReady, watchReady } from "./ready";
import { getRecurring } from "./api";
import { resetSessionCaches } from "./session-cache";

const tick = (ms = 15) => new Promise((r) => setTimeout(r, ms));
afterEach(() => {
  vi.unstubAllGlobals();
  resetSessionCaches();
});

describe("body data-ready (W6)", () => {
  it("is set only when no own read is in flight and no section is unread, and cleared as soon as one starts", async () => {
    let answer: (r: Response) => void = () => undefined;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((r) => (answer = r))));
    const stop = watchReady();
    await tick();
    expect(document.body.dataset.ready).toBe("1");   // nothing to wait for
    const read = getRecurring();
    expect(document.body.dataset.ready).toBeUndefined();
    await tick();
    expect(document.body.dataset.ready).toBeUndefined();   // still reading
    answer(new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }));
    await read;
    await tick();
    expect(document.body.dataset.ready).toBe("1");
    const hook = renderHook(({ d }) => usePageReady(d), { initialProps: { d: false } });
    expect(document.body.dataset.ready).toBeUndefined();   // a declared section is unread
    hook.rerender({ d: true });
    await tick();
    expect(document.body.dataset.ready).toBe("1");
    resetReady();   // a new page
    expect(document.body.dataset.ready).toBeUndefined();
    stop();
  });
});
