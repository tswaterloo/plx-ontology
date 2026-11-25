import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const sent: Record<string, unknown>[] = [];
let fail = true;
vi.mock("./api", () => ({
  ask: (req: Record<string, unknown>) => {
    sent.push(req);
    return (async function* () {
      if (fail) throw new TypeError("network");
      yield { event: "answer", data: { direct_answer: "ok", conversation_id: "c1" } };
    })();
  },
}));

import { askUrlKey, resetThreadStoreForTests, useThread } from "./thread";

const file = { id: "f1", name: "po.pdf", size: 10, image: false, upload: "b".repeat(32) };

describe("Ask requests (WEB-021, WEB-022, WEB-024)", () => {
  beforeEach(() => {
    resetThreadStoreForTests();
  });
  it("Retry resends the whole request: object, source, page context and the attachment bytes", async () => {
    sent.length = 0;
    fail = true;
    const { result } = renderHook(() => useThread());
    await act(async () => result.current.run("what is the price?", "PurchaseOrder:90412", "graph-mail://x/m1@1", [file], { context: { kind: "meeting", meeting_id: "m9" } }));
    expect(result.current.turns[0].phase).toBe("error");
    fail = false;
    await act(async () => result.current.retry(result.current.turns[0]));
    expect(sent).toHaveLength(2);
    for (const key of ["object_id", "source", "context", "attachments"]) expect(sent[1][key]).toEqual(sent[0][key]);
    expect(sent[1].attachments).toEqual([{ name: "po.pdf", upload: "b".repeat(32) }]);
    expect(sent[1].question).toBe("what is the price?");
  });

  it("a suggested follow-up keeps the turn's scope and context, but not its files", async () => {
    sent.length = 0;
    fail = false;
    const { result } = renderHook(() => useThread());
    await act(async () => result.current.run("first", "Supplier:S1", undefined, [file], { context: { kind: "page", page: "/team" } }));
    await act(async () => result.current.followUp("and the other one?", result.current.turns[0]));
    expect(sent[1]).toMatchObject({ question: "and the other one?", object_id: "Supplier:S1", context: { kind: "page", page: "/team" } });
    expect(sent[1].attachments).toBeUndefined();
  });

  it("a long question is sent whole: no length limit", async () => {
    sent.length = 0;
    const long = "a".repeat(120000) + " but not for customer X";
    const { result } = renderHook(() => useThread());
    await act(async () => result.current.run(long));
    expect(sent).toHaveLength(1);
    expect(sent[0].question).toBe(long);
  });

  it("the same question for another meeting, item or page is another request", () => {
    const base = { q: "what is open?", object: null, source: undefined, meeting: null, item: null, page: null };
    const keys = [
      askUrlKey(base),
      askUrlKey({ ...base, meeting: "A" }),
      askUrlKey({ ...base, meeting: "B" }),
      askUrlKey({ ...base, item: "A" }),
      askUrlKey({ ...base, item: "B" }),
      askUrlKey({ ...base, page: "/late" }),
      askUrlKey({ ...base, page: "/team" }),
    ];
    expect(new Set(keys).size).toBe(keys.length);
  });
});
