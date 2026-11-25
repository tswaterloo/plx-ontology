import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const readStatement = vi.fn();
const exportXlsx = vi.fn();
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  readStatement: (...a: unknown[]) => readStatement(...a),
  exportXlsx: (...a: unknown[]) => exportXlsx(...a),
}));
import { applyEvent } from "@/lib/thread";
import type { Answer } from "@/lib/types";
import { AnswerCard, type Turn } from "./answer-card";
import { AnswerText, checkLine, partLine, sourceCount, splitTables } from "./simple-answer";

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

describe("answer table type", () => {
  it("uses the page font (never the monospace data font) and right-aligns number columns only", () => {
    render(<AnswerText text={"| Person | Replies |\n|---|---|\n| Ana | 12 |\n| Bo | 3.5% |"} />);
    const name = screen.getByText("Ana").closest("td");
    const count = screen.getByText("12").closest("td");
    expect(name?.className).not.toMatch(/\bnum\b|font-mono/);
    expect(count?.className).not.toMatch(/\bnum\b|font-mono/);
    expect(count?.className).toContain("text-right");
    expect(name?.className).not.toContain("text-right");
    expect(screen.getByText("Replies").closest("th")?.className).toContain("text-right");
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

  it("shows the work under Sources: each read's query and the measure definitions the figures rest on", async () => {
    const worked: Answer = {
      ...simple,
      cited: [{ ...simple.cited![0], query: "SELECT supplier, owed, currency FROM supplier_balances", measure: null }, simple.cited![1]],
      definitions: [{ id: "payables_open", name: "Payables open", definition: "Open supplier invoices, by currency.", owner: "Finance", version: "v1a2b3c4d", defined_on: "2026-11-23" }],
    };
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: worked }} />);
    // the definition the figures rest on is beside the answer, not folded into Sources
    expect(screen.getByTestId("simple-definitions")).toHaveTextContent("Payables open (agreed definition): Open supplier invoices, by currency. Owner Finance, definition v1a2b3c4d, set 23 Nov 2026.");
    await userEvent.click(screen.getByText("Sources (3 records)"));
    const queries = screen.getAllByTestId("simple-query");
    expect(queries).toHaveLength(1);   // the search hit list ran no statement
    expect(queries[0]).toHaveTextContent("SELECT supplier, owed, currency FROM supplier_balances");
  });

  it("lists what a source's query reads and filters on; a changed or removed filter reads again as the reader and Reset goes back", async () => {
    const sql = "SELECT supplier, owed, currency FROM supplier_balances WHERE owed > 0 AND currency = 'CAD'";
    const worked: Answer = {
      ...simple,
      cited: [{ ...simple.cited![0], query: sql, measure: null, work: { views: ["supplier_balances"], more_filters: 0, filters: [{ index: 0, text: "owed > 0" }, { index: 1, text: "currency = 'CAD'" }] } }],
    };
    readStatement.mockReset().mockResolvedValueOnce({ sql: "x", columns: ["supplier", "owed", "currency"], rows: [["West Inc", "77.00", "USD"]], row_count: 1, truncated: false, read_at: "2026-11-23T17:00:00Z", as_of: null, work: null });
    readStatement.mockRejectedValueOnce(Object.assign(new Error("x"), { code: "filter_unworkable" }));
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: worked }} />);
    await userEvent.click(screen.getByText("Sources (2 records)"));
    const work = screen.getByTestId("simple-work");
    expect(work).toHaveTextContent("Reads supplier_balances");
    await userEvent.click(screen.getByRole("button", { name: "currency = 'CAD'" }));
    await userEvent.click(screen.getByRole("button", { name: "Take out" }));
    await waitFor(() => expect(screen.getByText("West Inc")).toBeTruthy());
    expect(readStatement).toHaveBeenCalledWith(sql, [{ index: 1, text: null }]);
    expect(screen.getByText(/Read again with your filters: 1 row/)).toBeTruthy();
    // the next change carries every change so far, by the original statement's places
    await userEvent.click(screen.getByRole("button", { name: "owed > 0" }));
    const box = screen.getByLabelText("Filter 1");
    await userEvent.clear(box);
    await userEvent.type(box, "owed > 1000{Enter}");
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(readStatement).toHaveBeenLastCalledWith(sql, [{ index: 0, text: "owed > 1000" }, { index: 1, text: null }]);
    expect(screen.getByText("West Inc")).toBeTruthy();   // a refused change leaves the last read shown
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await userEvent.click(screen.getByRole("button", { name: "Reset filters" }));
    await waitFor(() => expect(screen.queryByText("West Inc")).toBeNull());
    expect(screen.getByText(/^supplier balances, as of/)).toBeTruthy();
    expect(readStatement).toHaveBeenCalledTimes(2);
  });

  it("an answer that asks back offers the readings as quick replies, and a declared gap says what is not on record", async () => {
    const onClarify = vi.fn();
    const asked: Answer = { ...simple, outcome: "abstained", direct_answer: "Two Croda accounts match.", cited: [], clarify: { question: "Which Croda account?", options: [{ label: "Croda Inc (US)", detail: null, object: null }, { label: "Croda Canada", detail: null, object: null }], free_text: true }, not_found: [] };
    const { unmount } = render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: asked }} onClarify={onClarify} />);
    expect(screen.getByText("Which Croda account?")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Croda Canada" }));
    expect(onClarify).toHaveBeenCalled();
    unmount();
    const gap: Answer = { ...simple, outcome: "not_found", direct_answer: "Freight cost per shipment is not kept.", cited: [], not_found: [{ what: "freight cost per shipment", where_it_would_live: "carrier invoices" }] };
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "Freight is not kept.\n```gap\n{\"missing\": \"x\"}\n```", answer: gap }} />);
    expect(screen.getByTestId("simple-gaps")).toHaveTextContent("Not on record: freight cost per shipment (would be kept in carrier invoices).");
    expect(screen.queryByText(/```gap/)).toBeNull();
  });

  it("a run that failed shows no Correct/Wrong and no Deep research", () => {
    const ok: Turn = { id: "t", question: "q", phase: "done", streamed: "", answer: simple };
    const { unmount } = render(<AnswerCard turn={ok} onDeeper={() => {}} />);
    expect(screen.queryByRole("button", { name: /this answer was right/i })).not.toBeNull();
    expect(screen.queryByText("Deep research")).not.toBeNull();
    unmount();
    const failed: Turn = { ...ok, answer: { ...simple, assistant_unavailable: "usage_limit" } };
    render(<AnswerCard turn={failed} onDeeper={() => {}} />);
    expect(screen.queryByRole("button", { name: /this answer was right/i })).toBeNull();
    expect(screen.queryByText("Deep research")).toBeNull();
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

describe("the second check of an answer", () => {
  it("says in one line what it found, and why when it did not run", () => {
    expect(checkLine({ status: "checked", claims: 0, seconds: 11.6, why: null, disagrees: [] })).toBe("Checked against what was read: nothing found wrong (12 s).");
    expect(checkLine({ status: "repaired", claims: 1, seconds: 9, why: null, disagrees: [] })).toBe("Checked against what was read: 1 statement sent back for correction.");
    expect(checkLine({ status: "flagged", claims: 2, seconds: 9, why: null, disagrees: [] })).toBe("Checked against what was read: the check found a conflict or could not confirm 2 statements; shown as written.");
    expect(checkLine({ status: "disagrees", claims: 3, seconds: 9, why: null, disagrees: ["Check: x: the reads show y"] })).toMatch(/still disagrees with what is noted under the answer/);
    expect(checkLine({ status: "not_checked", claims: 0, seconds: 40, why: "ran out of time", disagrees: [] })).toBe("Not checked against what was read: ran out of time.");
  });
});

describe("what was read", () => {
  it("says per part: read with what it found, read with nothing found, or not read", () => {
    expect(partLine({ reads: 2, found: 1200 })).toBe("read, 1,200 found");
    expect(partLine({ reads: 1, found: 0 })).toBe("read, nothing found");
    expect(partLine({ reads: 0, found: 0 })).toBe("not read");
  });
});

describe("simple Ask Excel export", () => {
  it("exports the whole answer from the footer and carries truncation metadata for previewed sources", async () => {
    exportXlsx.mockReset().mockResolvedValue(new Blob(["xlsx"]));
    if (!URL.createObjectURL) Object.defineProperty(URL, "createObjectURL", { value: () => "blob:x", configurable: true });
    if (!URL.revokeObjectURL) Object.defineProperty(URL, "revokeObjectURL", { value: () => {}, configurable: true });
    const preview: Answer = {
      ...simple,
      cited: [{ ...simple.cited![0], records: 250, query: "SELECT supplier, owed, currency FROM supplier_balances" }],
    };
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: preview }} />);
    const buttons = screen.getAllByTestId("excel-export-btn");
    // Last export button is in the SimpleAnswer footer (answer-level export)
    await userEvent.click(buttons[buttons.length - 1]);
    await waitFor(() => expect(exportXlsx).toHaveBeenCalledTimes(1));
    expect(exportXlsx.mock.calls[0][0]).toMatchObject({
      title: preview.question,
      summary: preview.direct_answer,
      tables: [
        {
          title: "supplier balances",
          total_rows: 250,
          records: 250,
          truncated: true,
        },
      ],
    });
  });
});
