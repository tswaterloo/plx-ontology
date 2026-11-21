import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { applyEvent } from "@/lib/thread";
import type { Answer } from "@/lib/types";
import { AnswerCard, type Turn } from "./answer-card";
import { AnswerText, sourceCount, splitTables } from "./simple-answer";

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

describe("answer tables", () => {
  it("keep a code, number or date on one line and let longer words wrap", () => {
    render(<AnswerText text={"| PO | Note |\n|---|---|\n| PO100415 | waiting on the carrier for two weeks |"} />);
    const code = screen.getByText("PO100415").closest("td");
    const note = screen.getByText("waiting on the carrier for two weeks").closest("td");
    expect(code?.className).toContain("whitespace-nowrap");
    expect(note?.className).not.toContain("whitespace-nowrap");
  });
});

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

  it("a run that failed shows no Correct/Wrong and no Think it through", () => {
    const ok: Turn = { id: "t", question: "q", phase: "done", streamed: "", answer: simple };
    const { unmount } = render(<AnswerCard turn={ok} onDeeper={() => {}} />);
    expect(screen.queryByRole("button", { name: /this answer was right/i })).not.toBeNull();
    expect(screen.queryByText("Think it through")).not.toBeNull();
    unmount();
    const failed: Turn = { ...ok, answer: { ...simple, assistant_unavailable: "usage_limit" } };
    render(<AnswerCard turn={failed} onDeeper={() => {}} />);
    expect(screen.queryByRole("button", { name: /this answer was right/i })).toBeNull();
    expect(screen.queryByText("Think it through")).toBeNull();
  });

  it("links each file the answer made for download under its own name", () => {
    const doc = { name: "Late POs.docx", kind: "docx" as const, size: 2048, download_url: "/api/instruct/files/f_0123456789abcdef01234567" };
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: { ...simple, documents: [doc] } }} />);
    const link = screen.getByText("Late POs.docx");
    expect(link.getAttribute("href")).toBe(doc.download_url);
    expect(link.getAttribute("download")).toBe("Late POs.docx");
    expect(screen.getByTestId("simple-documents")).toHaveTextContent("2 KB");
    expect(screen.queryByTestId("document-changes")).toBeNull();
  });

  it("an edited copy lists every change before and after, to review before it is used", () => {
    const doc = {
      name: "Agreement (edited).docx", kind: "docx" as const, size: 4096,
      download_url: "/api/instruct/files/f_0123456789abcdef01234568",
      changes: [{ field: "paragraph", before: "within thirty (30) days (Net 30)", after: "within forty-five (45) days (Net 45)" },
                { field: "Product", before: "", after: "Rosemary Mint Body Wash" }],
    };
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: { ...simple, documents: [doc] } }} />);
    const changes = screen.getByTestId("document-changes");
    expect(changes).toHaveTextContent("2 changes from the original");
    expect(changes.querySelector("del")?.textContent).toBe("within thirty (30) days (Net 30)");
    expect([...changes.querySelectorAll("ins")].map((n) => n.textContent)).toEqual(
      ["within forty-five (45) days (Net 45)", "Rosemary Mint Body Wash"]);
  });

  it("an answer from the other path keeps the full card", () => {
    const { cited: _gone, ...full } = simple;
    void _gone;
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: full }} />);
    expect(screen.getByTestId("answer-card")).toBeInTheDocument();
    expect(screen.queryByTestId("simple-answer")).toBeNull();
  });

  it("whole tokens replace what was shown and mark the turn simple; plain tokens append", async () => {
    let turn: Turn = { id: "t", question: "q", phase: "thinking", streamed: "" };
    turn = applyEvent(turn, { event: "token", data: { text: "Let me check", whole: true } });
    turn = applyEvent(turn, { event: "token", data: { text: "", whole: true } });
    turn = applyEvent(turn, { event: "token", data: { text: "It was 9 (unverified) USD.", whole: true } });
    expect(turn.streamed).toBe("It was 9 (unverified) USD.");
    expect(turn.simple).toBe(true);
    render(<AnswerCard turn={turn} />);
    // written out from the top while the answer is under way (``useReveal``)
    await waitFor(() => expect(screen.getByTestId("simple-answer")).toHaveTextContent("It was 9 (unverified) USD."));
    let old: Turn = { id: "o", question: "q", phase: "thinking", streamed: "" };
    old = applyEvent(old, { event: "token", data: { text: "PO 1 " } });
    old = applyEvent(old, { event: "token", data: { text: "is late." } });
    expect(old.streamed).toBe("PO 1 is late.");
    expect(old.simple).toBeFalsy();
  });

  it("bold the answer writes inside its table is bold, never asterisks", () => {
    render(<AnswerText text={"| Month | POs |\n|---|---|\n| March | 91 |\n| **Total** | **108 (unverified)** |"} />);
    const shown = screen.getByTestId("simple-answer-text");
    expect(shown.textContent).not.toContain("**");
    expect(screen.getByText("Total").tagName).toBe("STRONG");
    expect(screen.getAllByTitle(/Not found in the records this answer read/).map((m) => m.textContent)).toEqual(["108 (unverified)"]);
  });

  it("an unverified figure is underlined with a tooltip, never words in the middle of the sentence", () => {
    render(<AnswerText text={"On standard 30 (unverified)-day terms, open for 7 (unverified) weeks.\n\n| PO | Total |\n|---|---|\n| 1 | 9 (unverified) USD |"} />);
    const shown = screen.getByTestId("simple-answer-text").cloneNode(true) as HTMLElement;
    shown.querySelectorAll(".sr-only").forEach((n) => n.remove());
    expect(shown.textContent).toContain("On standard 30-day terms, open for 7 weeks.");
    expect(shown.textContent).toContain("9 USD");
    expect(shown.textContent).not.toContain("unverified");
    const marks = screen.getAllByTitle(/Not found in the records this answer read/);
    expect(marks.map((m) => m.textContent)).toEqual(["30 (unverified)", "7 (unverified)", "9 (unverified)"]);
  });

  it("while it works, the steps say what it reads and what came back; narration then a read is reading, not writing", async () => {
    let turn: Turn = { id: "t", question: "q", phase: "thinking", streamed: "" };
    const step = (message: string) => ({ event: "status" as const, data: { step: "reading" as const, message, elapsed_ms: 1 } });
    turn = applyEvent(turn, step("Querying purchase orders"));
    turn = applyEvent(turn, step("2 rows from purchase orders"));
    turn = applyEvent(turn, { event: "token", data: { text: "Let me check the suppliers.", whole: true } });
    turn = applyEvent(turn, step("Querying suppliers"));
    expect(turn.phase).toBe("thinking");
    const { unmount } = render(<AnswerCard turn={turn} />);
    const progress = screen.getByTestId("progress");
    expect(progress).toHaveTextContent("2 rows from purchase orders");
    expect(progress).toHaveTextContent("Querying suppliers");
    expect(progress).not.toHaveTextContent("Writing");
    await waitFor(() => expect(screen.getByTestId("simple-answer")).toHaveTextContent("Let me check the suppliers."));
    unmount();
    turn = applyEvent(turn, { event: "token", data: { text: "", whole: true } });
    turn = applyEvent(turn, { event: "token", data: { text: "Two are open.", whole: true } });
    expect(turn.phase).toBe("streaming");
    render(<AnswerCard turn={turn} />);
    expect(screen.getByTestId("progress")).toHaveTextContent("Writing");
  });
});
