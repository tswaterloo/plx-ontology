import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { STALE_REFRESH_MS, STALE_REFRESH_TRIES, useStaleRefresh } from "./stale-refresh";

describe("useStaleRefresh", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("asks for a page served stale again in place, a few times at most", () => {
    const refresh = vi.fn();
    const { rerender } = renderHook(({ since }) => useStaleRefresh(since, refresh), {
      initialProps: { since: "2026-11-11T14:00:00Z" as string | null },
    });
    vi.advanceTimersByTime(STALE_REFRESH_MS - 1);
    expect(refresh).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(refresh).toHaveBeenCalledTimes(1);
    // still stale after each try (a new copy each time): it stops after the last try
    for (let n = 0; n < STALE_REFRESH_TRIES + 2; n++) {
      rerender({ since: `2026-11-11T14:0${n}:30Z` });
      vi.advanceTimersByTime(STALE_REFRESH_MS);
    }
    expect(refresh).toHaveBeenCalledTimes(STALE_REFRESH_TRIES);
  });

  it("leaves a fresh page alone", () => {
    const refresh = vi.fn();
    renderHook(() => useStaleRefresh(null, refresh));
    vi.advanceTimersByTime(STALE_REFRESH_MS * 3);
    expect(refresh).not.toHaveBeenCalled();
  });
});
