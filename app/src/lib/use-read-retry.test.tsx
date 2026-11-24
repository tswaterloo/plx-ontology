import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { forgetKeptReads, useRead } from "./use-read";

describe("useRead retry", () => {
  it("a retry while a read is running neither aborts it nor starts a second one; after it lands a retry reads again", async () => {
    forgetKeptReads();
    let finish: (v: string) => void = () => undefined;
    const signals: AbortSignal[] = [];
    const load = vi.fn((signal: AbortSignal) => {
      signals.push(signal);
      return new Promise<string>((done) => (finish = done));
    });
    const { result } = renderHook(() => useRead<string>("slow-read", load));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(1));
    act(() => result.current.retry());
    act(() => result.current.retry());
    expect(load).toHaveBeenCalledTimes(1);
    expect(signals[0].aborted).toBe(false);
    await act(async () => finish("done"));
    await waitFor(() => expect(result.current.data).toBe("done"));
    act(() => result.current.retry());
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
  });
});
