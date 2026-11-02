import { act, renderHook } from "@testing-library/react";
import { StrictMode, useEffect, useRef } from "react";
import { describe, expect, it, vi } from "vitest";

const seen: AbortSignal[] = [];
vi.mock("./api", () => ({
  ask: (_req: unknown, signal: AbortSignal) => {
    seen.push(signal);
    // a stream that stays open until aborted
    return (async function* () {
      await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve()));
    })();
  },
}));

import { useThread } from "./thread";

describe("useThread lifecycle", () => {
  it("keeps a started answer through React's development double mount, and stops it when the page is left", async () => {
    seen.length = 0;
    // as the Ask page does: the question from the URL starts in an effect on mount, once
    const { result, unmount } = renderHook(
      () => {
        const t = useThread();
        const started = useRef(false);
        useEffect(() => {
          if (started.current) return;
          started.current = true;
          void t.run("which POs are late?");
        }, [t]);
        return t;
      },
      { wrapper: StrictMode },
    );
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(seen).toHaveLength(1);
    expect(seen[0].aborted).toBe(false);
    expect(result.current.busy).toBe(true);
    unmount();
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(seen[0].aborted).toBe(true);
  });
});
