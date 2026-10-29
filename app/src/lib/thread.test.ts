import { describe, expect, it } from "vitest";
import type { Turn } from "@/components/answer-card";
import { applyEvent, conversationOf, historyOf, MAX_HISTORY } from "./thread";
import type { Answer } from "./types";

const answer: Answer = {
  answer_id: "a1",
  question: "why late",
  conversation_id: "conv_1",
  outcome: "answered",
  latency_ms: 10,
  direct_answer: "PO 5280 is late.",
  facts: [],
  not_found: [],
  hidden: { count: 0, by_owner: [] },
  conflicts: [],
  freshness: { as_of: "2026-10-29T10:00:00Z" },
};
const base: Turn = { id: "t", question: "why late", phase: "thinking", streamed: "" };

describe("applyEvent", () => {
  it("streams tokens, then replaces them with the final answer", () => {
    let t = applyEvent(base, { event: "token", data: { text: "PO " } });
    t = applyEvent(t, { event: "token", data: { text: "5280" } });
    expect(t).toMatchObject({ phase: "streaming", streamed: "PO 5280" });
    t = applyEvent(t, { event: "answer", data: answer });
    expect(t).toMatchObject({ phase: "done", answer });
  });

  it("shows a plain sentence for a stream error, never the payload", () => {
    const t = applyEvent(base, { event: "error", data: { code: "boom", message: "psycopg.OperationalError" } });
    const internal = applyEvent(base, { event: "error", data: { code: "internal", message: "Something went wrong; quote incident 9f3a01bc." } });
    expect(internal.error).toContain("incident 9f3a01bc");
    const busy = applyEvent(base, { event: "error", data: { code: "busy", message: "x" } });
    expect(busy).toMatchObject({ phase: "error", notice: "busy" });
    expect(applyEvent(base, { event: "error", data: { code: "ask_not_served", message: "x" } }).notice).toBe("off");
    expect(t.notice).toBeUndefined();
    expect(t.phase).toBe("error");
    expect(t.error).not.toContain("psycopg");
  });
});

describe("historyOf", () => {
  it("sends prior questions and finished answers so follow-ups keep context", () => {
    const turns: Turn[] = [
      { ...base, id: "1", phase: "done", answer },
      { ...base, id: "2", question: "and the other one?", phase: "error", error: "x" },
    ];
    expect(historyOf(turns)).toEqual([
      { role: "user", text: "why late" },
      { role: "assistant", text: "PO 5280 is late." },
      { role: "user", text: "and the other one?" },
    ]);
    expect(conversationOf(turns)).toBe("conv_1");
  });

  it("stays within the API's limits: 12 turns, 4,000 characters each", () => {
    const many = Array.from({ length: 20 }, (_, i) => ({ ...base, id: String(i), question: `q${i}`, phase: "done" as const, answer }));
    const h = historyOf(many);
    expect(h).toHaveLength(MAX_HISTORY);
    expect(h[h.length - 1]).toEqual({ role: "assistant", text: "PO 5280 is late." });
    expect(historyOf([{ ...base, question: "x".repeat(5000) }])[0].text).toHaveLength(4000);
  });

  it("ignores a malformed conversation id", () => {
    expect(conversationOf([{ ...base, phase: "done", answer: { ...answer, conversation_id: "../../x y" } }])).toBeUndefined();
  });
});
