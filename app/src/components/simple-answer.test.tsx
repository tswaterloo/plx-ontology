import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { applyEvent } from "@/lib/thread";
import type { Answer } from "@/lib/types";
import { AnswerCard, type Turn } from "./answer-card";
import { sourceCount, splitTables } from "./simple-answer";

const simple: Answer = {
  answer_id: "a1",
  question: "What do we owe our top suppliers?",
  conversation_id: "c1",
  outcome: "partial",
  latency_ms: 9000,
  access_label: null,
  direct_answer: "Two suppliers hold most of it, as of Nov 17.\n\n| Supplier | Owed |\n|---|---|\n| North Co | 12,400.00 CAD |\n| South Ltd | 9 (unverified) USD |\n\nThe credit notes could not be checked.",
  facts: [],
  not_found: [{ what: "credit notes", where_it_would_live: "Sage" }],
  hidden: { count: 0 },
  conflicts: [],
  freshness: { as_of: "2026-11-17T22:00:00Z" },
  cited: [
    { about: "supplier balances", columns: ["supplier", "owed", "currency"], rows: [["North Co", "12400.00", "CAD"], ["South Ltd", "9", "USD"]], records: 2, as_of: "2026-11-17" },
    { about: "mail, Teams and documents", columns: ["citation", "snippet"], rows: [["original://m/1", "Statement attached"]], records: 1, as_of: null },
  ],
};

describe("splitTables", () => {
  it("takes Markdown tables out of the prose, keeps the rest in order", () => {
    const parts = splitTables(simple.direct_answer);
    expect(parts.map((p) => p.kind)).toEqual(["text", "table", "text"]);
    expect(parts[1]).toEqual({ kind: "table", head: ["Supplier", "Owed"], rows: [["North Co", "12,400.00 CAD"], ["South Ltd", "9 (unverified) USD"]] });
  });
  it("leaves a lone pipe line without a rule row as prose", () => {
    expect(splitTables("a | b\nnext").map((p) => p.kind)).toEqual(["text"]);
  });
});

describe("simple Ask answer", () => {
  it("shows the answer with its table and one collapsed Sources line, none of the full card's boilerplate", async () => {
    const turn: Turn = { id: "t", question: "q", phase: "done", streamed: "", answer: simple };
    render(<AnswerCard turn={turn} />);
    const card = screen.getByTestId("simple-answer");
    expect(card.querySelector("table")).not.toBeNull();
    expect(card).toHaveTextContent("12,400.00 CAD");
    expect(card).toHaveTextContent("could not be checked");
    for (const gone of ["Partly answered", "Still to confirm", "Checked", "General knowledge", "Show all"]) expect(card).not.toHaveTextContent(gone);
    expect(screen.queryByTestId("answer-card")).toBeNull();
    const sources = screen.getByTestId("simple-sources");
    expect(sources).toHaveTextContent("Sources (3 records)");
    expect(sources).not.toHaveAttribute("open");
    await userEvent.click(screen.getByText("Sources (3 records)"));
    expect(sources).toHaveAttribute("open");
    expect(sources).toHaveTextContent("Statement attached");
    expect(sourceCount(simple.cited)).toBe(3);
  });

  it("an answer from the other path keeps the full card", () => {
    const { cited: _gone, ...full } = simple;
    void _gone;
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: full }} />);
    expect(screen.getByTestId("answer-card")).toBeInTheDocument();
    expect(screen.queryByTestId("simple-answer")).toBeNull();
  });

  it("whole tokens replace what was shown and mark the turn simple; plain tokens append", () => {
    let turn: Turn = { id: "t", question: "q", phase: "thinking", streamed: "" };
    turn = applyEvent(turn, { event: "token", data: { text: "Let me check", whole: true } });
    turn = applyEvent(turn, { event: "token", data: { text: "", whole: true } });
    turn = applyEvent(turn, { event: "token", data: { text: "It was 9 (unverified) USD.", whole: true } });
    expect(turn.streamed).toBe("It was 9 (unverified) USD.");
    expect(turn.simple).toBe(true);
    render(<AnswerCard turn={turn} />);
    expect(screen.getByTestId("simple-answer")).toHaveTextContent("It was 9 (unverified) USD.");
    let old: Turn = { id: "o", question: "q", phase: "thinking", streamed: "" };
    old = applyEvent(old, { event: "token", data: { text: "PO 1 " } });
    old = applyEvent(old, { event: "token", data: { text: "is late." } });
    expect(old.streamed).toBe("PO 1 is late.");
    expect(old.simple).toBeFalsy();
  });
});
