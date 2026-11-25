import { act, renderHook } from "@testing-library/react";
import { StrictMode, useEffect, useRef } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AskEvent } from "./types";

const seen: AbortSignal[] = [];
const stoppedIds: string[] = [];
const attachedIds: string[] = [];
let pushEvent: ((ev: AskEvent) => void) | null = null;
let finishStream: (() => void) | null = null;

vi.mock("./api", () => ({
  ask: (_req: unknown, signal: AbortSignal) => {
    seen.push(signal);
    return (async function* () {
      const queue: AskEvent[] = [];
      let wake: (() => void) | null = null;
      let done = false;
      pushEvent = (ev) => {
        queue.push(ev);
        wake?.();
      };
      finishStream = () => {
        done = true;
        wake?.();
      };
      signal.addEventListener("abort", () => {
        done = true;
        wake?.();
      });
      while (!done || queue.length > 0) {
        if (queue.length > 0) {
          yield queue.shift()!;
        } else {
          await new Promise<void>((r) => {
            wake = r;
          });
          wake = null;
        }
      }
    })();
  },
  attachAskRun: (conversationId: string) => {
    attachedIds.push(conversationId);
    return (async function* () {
      yield { event: "status" as const, data: { step: "reading", message: "Still reading", elapsed_ms: 5 } };
      yield {
        event: "answer" as const,
        data: {
          answer_id: "a_resumed",
          question: "Soapbox shipments",
          conversation_id: conversationId,
          outcome: "answered",
          latency_ms: 10,
          access_label: null,
          direct_answer: "Resumed answer after reload.",
          facts: [],
          not_found: [],
          hidden: { count: 0, by_owner: [] },
          conflicts: [],
          freshness: {},
        },
      };
    })();
  },
  stopAskRun: (conversationId: string) => {
    stoppedIds.push(conversationId);
    return Promise.resolve({ stopped: true, conversation_id: conversationId });
  },
}));

import { resetThreadStoreForTests, useAskRunning, useThread } from "./thread";

describe("useThread persistent lifecycle", () => {
  beforeEach(() => {
    seen.length = 0;
    stoppedIds.length = 0;
    attachedIds.length = 0;
    pushEvent = null;
    finishStream = null;
    resetThreadStoreForTests();
  });

  it("keeps an in-flight answer running across route unmount/remount and only stops on explicit Stop", async () => {
    const first = renderHook(
      () => {
        const t = useThread();
        const started = useRef(false);
        useEffect(() => {
          if (started.current) return;
          started.current = true;
          void t.run("Soapbox shipments?");
        }, [t]);
        return t;
      },
      { wrapper: StrictMode },
    );
    const nav = renderHook(() => useAskRunning());

    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(seen).toHaveLength(1);
    expect(seen[0].aborted).toBe(false);
    expect(first.result.current.busy).toBe(true);
    expect(nav.result.current.anyRunning).toBe(true);
    const cid = first.result.current.conversationId;
    expect(cid).toBeTruthy();

    // Clicking away from /ask unmounts the page: MUST NOT abort the stream!
    first.unmount();
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(seen[0].aborted).toBe(false);
    expect(nav.result.current.anyRunning).toBe(true);

    // Events arriving while away still update the persistent store.
    await act(async () => {
      pushEvent?.({ event: "status", data: { step: "reading", message: "Checking mail", elapsed_ms: 20 } });
      await new Promise((r) => setTimeout(r, 10));
    });

    // Clicking back to /ask immediately sees the live streaming turn and progress.
    const second = renderHook(() => useThread());
    expect(second.result.current.busy).toBe(true);
    expect(second.result.current.turns).toHaveLength(1);
    expect(second.result.current.turns[0].status).toBe("Checking mail");

    // Clicking New chat (reset) opens a blank composer without cancelling the background run!
    act(() => second.result.current.reset());
    expect(second.result.current.turns).toHaveLength(0);
    expect(seen[0].aborted).toBe(false);
    expect(nav.result.current.anyRunning).toBe(true);

    // Selecting the running chat again and pressing explicit Stop cancels it and notifies server.
    act(() => {
      expect(second.result.current.select(cid!)).toBe(true);
    });
    act(() => second.result.current.stop());
    expect(seen[0].aborted).toBe(true);
    expect(stoppedIds).toEqual([cid]);
    expect(nav.result.current.anyRunning).toBe(false);
    finishStream?.();
    second.unmount();
    nav.unmount();
  });

  it("reattaches to an active server run when seeding after a browser reload", async () => {
    const { result } = renderHook(() => useThread());
    await act(async () => {
      result.current.seed([], {
        conversationId: "reload_c1",
        activeRun: {
          run_id: "run_1",
          conversation_id: "reload_c1",
          question: "Soapbox shipments",
          started_at: new Date().toISOString(),
          status: "Reading mail",
          steps: ["Reading mail"],
          streamed: "",
          simple: false,
        },
      });
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(attachedIds).toEqual(["reload_c1"]);
    expect(result.current.turns).toHaveLength(1);
    expect(result.current.turns[0].phase).toBe("done");
    expect(result.current.turns[0].answer?.direct_answer).toBe("Resumed answer after reload.");
  });
});
