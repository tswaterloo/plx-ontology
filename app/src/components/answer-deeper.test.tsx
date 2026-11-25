import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Answer } from "@/lib/types";
import { AnswerCard, type Turn } from "./answer-card";

const answer: Answer = {
  answer_id: "a1",
  question: "Why did CAD sales drop in October?",
  conversation_id: "c1",
  outcome: "answered",
  latency_ms: 9000,
  access_label: null,
  direct_answer: "CAD sales fell 32% against October last year.",
  facts: [],
  not_found: [],
  hidden: { count: 0 },
  conflicts: [],
  freshness: { as_of: "2026-11-17T22:00:00Z" },
  cited: [],
};

const done = (extra: Turn["request"] = { files: [], extra: {} }): Turn => ({ id: "t1", question: answer.question, phase: "done", streamed: "", answer, request: extra });

describe("Deep research", () => {
  it("asks a finished answer's question again, thought through", async () => {
    const deeper = vi.fn();
    render(<AnswerCard turn={done()} onDeeper={deeper} />);
    await userEvent.click(screen.getByRole("button", { name: "Deep research" }));
    expect(deeper).toHaveBeenCalledTimes(1);
  });
  it("is not offered on an answer that was already thought through, while answering, or when the page is busy", () => {
    const { rerender } = render(<AnswerCard turn={done({ files: [], extra: { deep: true } })} onDeeper={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Deep research" })).toBeNull();
    rerender(<AnswerCard turn={{ ...done(), phase: "streaming", answer: undefined, streamed: "CAD sales" }} onDeeper={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Deep research" })).toBeNull();
    rerender(<AnswerCard turn={done()} />);
    expect(screen.queryByRole("button", { name: "Deep research" })).toBeNull();
  });
});
