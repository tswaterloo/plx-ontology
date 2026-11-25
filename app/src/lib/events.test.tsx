import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readPath } from "./api";
import { handlePageEvent, refreshStoredReads, resetEventsForTests } from "./events";
import { noteAnswerHeaders, resetReadStoreForTests } from "./read-store";
import { resetSessionCaches } from "./session-cache";
import { keepRead, keptRead, useRead } from "./use-read";
import { setViewingAs } from "./view-as";

vi.mock("./api", () => ({ readPath: vi.fn(async () => ({ reloaded: true })), ownerStreamHeaders: vi.fn(async (h: Record<string, string>) => h) }));

beforeEach(() => {
  resetReadStoreForTests();
  resetEventsForTests();
  vi.clearAllMocks();
});

afterEach(() => {
  resetEventsForTests();
  resetSessionCaches();
  vi.useRealTimers();
});

describe("the page event stream's handling", () => {
  it("a cycle revalidates the open page at once without a click", async () => {
    let n = 0;
    const load = vi.fn(async () => ({ n: ++n }));
    const { result } = renderHook(() => useRead("/api/records/views", load));
    await waitFor(() => expect(result.current.data).toEqual({ n: 1 }));
    handlePageEvent("cycle", { cycle: "cycle-2" });
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.data).toEqual({ n: 2 }));
  });

  it("older kept reads catch up off the click path through read-ahead", async () => {
    vi.useFakeTimers();
    keepRead("/api/old-screen", { old: true }, true, undefined, Date.now() - 20_000);
    refreshStoredReads(false);
    await vi.runAllTimersAsync();
    expect(readPath).toHaveBeenCalledWith("/api/old-screen", expect.any(AbortSignal), true);
  });

  it("does not refresh owner keptKeys when viewingAs is active", async () => {
    vi.useFakeTimers();
    keepRead("/api/owner-screen", { owner: true }, true, undefined, Date.now() - 20_000);
    setViewingAs({ key: "other", name: "Other" });
    try {
      refreshStoredReads(false);
      await vi.runAllTimersAsync();
      expect(readPath).not.toHaveBeenCalled();
    } finally {
      setViewingAs(null);
    }
  });

  it("a reconnect catch-up is spread over the minute rather than fired in one herd", () => {
    vi.useFakeTimers();
    keepRead("/api/a", { a: 1 }, true, undefined, Date.now() - 20_000);
    keepRead("/api/b", { b: 1 }, true, undefined, Date.now() - 20_000);
    refreshStoredReads(true);
    expect(readPath).not.toHaveBeenCalled();
    vi.advanceTimersByTime(500);
    expect(vi.getTimerCount()).toBeGreaterThan(0); // at least one read is still waiting its turn
  });

  it("a moved door event wipes kept reads through the same fail-closed rule as an API answer", () => {
    noteAnswerHeaders(new Headers({ "X-PLX-Door": "door-a" }));
    keepRead("/api/trust", { n: 1 });
    handlePageEvent("door", { door: "door-b" });
    expect(keptRead("/api/trust")).toBeNull();
  });

  it("the events path itself is never a stored answer", async () => {
    const { keptPath } = await import("./read-store");
    expect(keptPath("/api/events")).toBe(false);
  });
});
