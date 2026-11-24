import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { STALE_REFRESH_MS, STALE_REFRESH_MAX_MS, staleRefreshDelay, useStaleRefresh, useStaleRefreshOnReturn } from "./stale-refresh";

describe("useStaleRefresh", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("asks for a page served stale again in place, for as long as it stays stale, never faster than every 15 s", () => {
    const refresh = vi.fn();
    renderHook(() => useStaleRefresh("2026-11-11T14:00:00Z", refresh));
    expect(STALE_REFRESH_MS).toBeLessThanOrEqual(2000);
    act(() => { vi.advanceTimersByTime(STALE_REFRESH_MS - 1); });
    expect(refresh).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(1); });
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(staleRefreshDelay(40)).toBe(STALE_REFRESH_MAX_MS);
    for (let n = 1; n < 30; n++) act(() => { vi.advanceTimersByTime(staleRefreshDelay(n)); });
    expect(refresh).toHaveBeenCalledTimes(30);
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
    act(() => { vi.advanceTimersByTime(STALE_REFRESH_MAX_MS * 2); });
    expect(refresh).toHaveBeenCalledTimes(4);
    // a later stale episode gets a new budget
    rerender({ since: "2026-11-11T15:00:00Z" });
    act(() => { vi.advanceTimersByTime(STALE_REFRESH_MS); });
    expect(refresh).toHaveBeenCalledTimes(5);
  });

  it("sends nothing after unmount", () => {
    const refresh = vi.fn();
    const { unmount } = renderHook(() => useStaleRefresh("2026-11-11T14:00:00Z", refresh));
    unmount();
    act(() => { vi.advanceTimersByTime(STALE_REFRESH_MAX_MS * 3); });
    expect(refresh).not.toHaveBeenCalled();
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

  it("asks on every return while the page stays stale", () => {
    const refresh = vi.fn();
    renderHook(() => useStaleRefreshOnReturn("2026-11-11T14:00:00Z", refresh));
    for (let n = 0; n < 8; n++) {
      act(() => { vi.advanceTimersByTime(1500); window.dispatchEvent(new Event("focus")); });
    }
    expect(refresh).toHaveBeenCalledTimes(8);
  });
});
