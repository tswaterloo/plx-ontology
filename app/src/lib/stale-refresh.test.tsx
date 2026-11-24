import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { STALE_REFRESH_MS, STALE_REFRESH_TRIES, staleRefreshDelay, useStaleRefresh, useStaleRefreshOnReturn } from "./stale-refresh";

describe("useStaleRefresh", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("asks for a page served stale again in place, a few times at most", () => {
    const refresh = vi.fn();
    const { rerender } = renderHook(({ since }) => useStaleRefresh(since, refresh), {
      initialProps: { since: "2026-11-11T14:00:00Z" as string | null },
    });
    // the first ask comes soon (the server's read again is usually done in 1-2 s)
    expect(STALE_REFRESH_MS).toBeLessThanOrEqual(2000);
    act(() => { vi.advanceTimersByTime(STALE_REFRESH_MS - 1); });
    expect(refresh).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(1); });
    expect(refresh).toHaveBeenCalledTimes(1);
    // still stale after each try (a new copy each time): each waits longer, then it stops
    for (let n = 1; n < STALE_REFRESH_TRIES + 2; n++) {
      rerender({ since: `2026-11-11T14:0${n}:30Z` });
      act(() => { vi.advanceTimersByTime(staleRefreshDelay(n) - 1); });
      expect(refresh).toHaveBeenCalledTimes(Math.min(n, STALE_REFRESH_TRIES));
      act(() => { vi.advanceTimersByTime(1); });
    }
    expect(refresh).toHaveBeenCalledTimes(STALE_REFRESH_TRIES);
  });

  it("keeps trying when the server sends the same stale_since each time, then stops once fresh", () => {
    const refresh = vi.fn();
    const { rerender } = renderHook(({ since }) => useStaleRefresh(since, refresh), {
      initialProps: { since: "2026-11-11T14:00:00Z" as string | null },
    });
    for (let n = 1; n <= 3; n++) {
      act(() => { vi.advanceTimersByTime(staleRefreshDelay(n - 1)); });
      expect(refresh).toHaveBeenCalledTimes(n);
    }
    // the answer is fresh at last: that is the fourth ask and nothing after
    act(() => { vi.advanceTimersByTime(staleRefreshDelay(3)); });
    expect(refresh).toHaveBeenCalledTimes(4);
    rerender({ since: null });
    act(() => { vi.advanceTimersByTime(staleRefreshDelay(STALE_REFRESH_TRIES) * 2); });
    expect(refresh).toHaveBeenCalledTimes(4);
    // a later stale episode gets a new budget
    rerender({ since: "2026-11-11T15:00:00Z" });
    act(() => { vi.advanceTimersByTime(STALE_REFRESH_MS); });
    expect(refresh).toHaveBeenCalledTimes(5);
  });

  it("stops at the budget when it never comes fresh, and not after unmount", () => {
    const refresh = vi.fn();
    const { unmount } = renderHook(() => useStaleRefresh("2026-11-11T14:00:00Z", refresh));
    for (let n = 0; n < STALE_REFRESH_TRIES + 3; n++) act(() => { vi.advanceTimersByTime(staleRefreshDelay(n)); });
    expect(refresh).toHaveBeenCalledTimes(STALE_REFRESH_TRIES);
    unmount();
    for (let n = 0; n < 3; n++) act(() => { vi.advanceTimersByTime(staleRefreshDelay(STALE_REFRESH_TRIES)); });
    expect(refresh).toHaveBeenCalledTimes(STALE_REFRESH_TRIES);
  });

  it("leaves a fresh page alone", () => {
    const refresh = vi.fn();
    renderHook(() => useStaleRefresh(null, refresh));
    act(() => { vi.advanceTimersByTime(STALE_REFRESH_MS * 3); });
    expect(refresh).not.toHaveBeenCalled();
  });
});

describe("useStaleRefreshOnReturn", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("sends no read on a timer, asks when the window is returned to, and stops once fresh", () => {
    const refresh = vi.fn();
    const { rerender } = renderHook(({ since }) => useStaleRefreshOnReturn(since, refresh), {
      initialProps: { since: "2026-11-11T14:00:00Z" as string | null },
    });
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(refresh).not.toHaveBeenCalled();
    act(() => { window.dispatchEvent(new Event("focus")); document.dispatchEvent(new Event("visibilitychange")); });
    expect(refresh).toHaveBeenCalledTimes(1);   // two events together count once
    rerender({ since: null });
    act(() => { vi.advanceTimersByTime(5000); window.dispatchEvent(new Event("focus")); });
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("asks at most STALE_REFRESH_TRIES times while the page stays stale", () => {
    const refresh = vi.fn();
    renderHook(() => useStaleRefreshOnReturn("2026-11-11T14:00:00Z", refresh));
    for (let n = 0; n < STALE_REFRESH_TRIES + 3; n++) {
      act(() => { vi.advanceTimersByTime(1500); window.dispatchEvent(new Event("focus")); });
    }
    expect(refresh).toHaveBeenCalledTimes(STALE_REFRESH_TRIES);
  });
});
