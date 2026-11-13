import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { InstructPart } from "@/lib/types";
import { InstructionPart } from "./instruction-part";

const EMAIL = { kind: "email", handle: "graph-mail://me/m1@1" };

const QUESTION: InstructPart = {
  status: "question",
  text: "reply to this",
  question: "Use what was agreed in Tuesday's ops stand-up?",
  proposed: [{ meeting_id: "m-ops", title: "Ops stand-up", day: "2026-11-10" }],
  meetings: [
    { meeting_id: "m-ops", title: "Ops stand-up", day: "2026-11-10" },
    { meeting_id: "m-qc", title: "QC weekly", day: "2026-11-06" },
  ],
  writes: [],
  documents: [],
  workflow: null,
  read: [{ kind: "email", label: "PO 90412 ship date", open_url: "/source?handle=x" }],
  context: EMAIL,
  answers: [],
  meetings_used: [],
};

const DRAFTED: InstructPart = {
  ...QUESTION,
  status: "drafted",
  question: null,
  proposed: [],
  writes: [
    {
      intent_id: "wi_0123456789abcdef01234567",
      label: "PO 90412 line 10000: expectedReceiptDate to 2026-11-20",
      status: "prepared",
      problems: [],
      changes: [{ field: "expectedReceiptDate", current: "2026-11-04", proposed: "2026-11-20", changed: true }],
    },
  ],
  documents: [{ name: "Ship date note.docx", kind: "docx", size: 2048, digest: "d", download_url: "/api/instruct/files/f_1", upload_id: "up_1", folder: "/Purchasing/Quotes" }],
};

const PREVIEW = {
  upload_id: "up_1",
  name: "Ship date note.docx",
  kind: "docx",
  size: 2048,
  digest: "d",
  folder: "/Purchasing/Quotes",
  before: "/Purchasing/Quotes, without Ship date note.docx",
  after: "/Purchasing/Quotes with Ship date note.docx (2048 bytes) added",
  problems: [],
  missing: ["This build has no SharePoint upload path: the approved upload is recorded, not made."],
  preview_hash: "h1",
  status: "prepared",
};

let fetchMock: ReturnType<typeof vi.fn>;
let replies: unknown[];

beforeEach(() => {
  replies = [];
  fetchMock = vi.fn(async (url: string) => {
    if (url.startsWith("/api/writes")) return new Response(JSON.stringify({ items: [] }), { status: 200 });
    return new Response(JSON.stringify(replies.shift()), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const calls = (path: string) => fetchMock.mock.calls.filter(([u]) => String(u).startsWith(path));

describe("InstructionPart", () => {
  it("continues the instruction on its own page with the meeting picked or the answer typed", () => {
    const onInstruct = vi.fn();
    render(<InstructionPart part={QUESTION} answerId="abc123" onInstruct={onInstruct} />);
    expect(screen.getByRole("link", { name: "PO 90412 ship date" }).getAttribute("href")).toBe("/source?handle=x");
    fireEvent.click(screen.getByRole("button", { name: "Use Ops stand-up (2026-11-10)" }));
    expect(onInstruct).toHaveBeenLastCalledWith("Use Ops stand-up", {
      source: "graph-mail://me/m1@1",
      instruction: { instruction_id: "abc123", answer: null, use: ["m-ops"] },
    });
    fireEvent.change(screen.getByLabelText("Your answer"), { target: { value: "Dock 4" } });
    fireEvent.click(screen.getByRole("button", { name: "Answer" }));
    expect(onInstruct).toHaveBeenLastCalledWith("Dock 4", {
      source: "graph-mail://me/m1@1",
      instruction: { instruction_id: "abc123", answer: "Dock 4", use: [] },
    });
  });

  it("asks whether to draft or answer, and sends the pick with the same page", () => {
    const onInstruct = vi.fn();
    render(<InstructionPart part={{ ...QUESTION, status: "route", proposed: [], meetings: [], read: [] }} answerId="r1" onInstruct={onInstruct} />);
    fireEvent.click(screen.getByRole("button", { name: "Answer it" }));
    expect(onInstruct).toHaveBeenLastCalledWith("reply to this", { source: "graph-mail://me/m1@1", mode: "ask" });
    fireEvent.click(screen.getByRole("button", { name: "Draft it" }));
    expect(onInstruct).toHaveBeenLastCalledWith("reply to this", { source: "graph-mail://me/m1@1", mode: "do" });
  });

  it("shows a proposed workflow in Draft mode with no keep button the studio would refuse", () => {
    const workflow = { label: "Monday supplier chase", trigger: { kind: "schedule.weekly", words: "Every Monday", params: { weekday: "monday" } }, conditions: [], actions: [{ kind: "instruction", words: "Draft a chase for each late PO line", params: {} }], mode: "draft" as const };
    render(<InstructionPart part={{ ...DRAFTED, status: "workflow", writes: [], documents: [], workflow }} answerId="w1" onInstruct={vi.fn()} />);
    expect(screen.getByTestId("instruct-workflow").textContent).toContain("cannot keep workflows on this server yet");
    expect(screen.queryByRole("button", { name: /Keep as draft/ })).toBeNull();
    expect(calls("/api/autopilot/workflows")).toEqual([]);
  });

  it("shows before and after, and approves an upload only on a second Confirm click", async () => {
    replies = [PREVIEW, { ...PREVIEW, status: "would_upload" }];
    render(<InstructionPart part={DRAFTED} answerId="abc123" onInstruct={vi.fn()} />);
    const change = screen.getByTestId("instruct-change").textContent ?? "";
    expect(change).toContain("2026-11-04");
    expect(change).toContain("2026-11-20");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Preview upload" })));
    expect(screen.getByTestId("upload-preview").textContent).toContain("with Ship date note.docx");
    fireEvent.click(screen.getByRole("button", { name: "Approve upload" }));
    // Approve only arms the Confirm: nothing is approved yet
    expect(calls("/api/instruct/uploads/up_1/approve")).toHaveLength(0);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Confirm upload" })));
    const [, init] = calls("/api/instruct/uploads/up_1/approve")[0];
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({ confirm: true, preview_hash: "h1" });
    expect(screen.getByTestId("upload-preview").textContent).toContain("Recorded as a dry run.");
  });

  it("says approving uploads now when nothing is missing, and shows the upload made", async () => {
    const ack = "Adding a file to SharePoint is not a routine write: your confirm approves this one upload.";
    replies = [{ ...PREVIEW, missing: [], acknowledge: [ack] }, { ...PREVIEW, missing: [], acknowledge: [], status: "uploaded" }];
    render(<InstructionPart part={DRAFTED} answerId="abc123" onInstruct={vi.fn()} />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Preview upload" })));
    // what the confirm accepts is shown before it
    expect(screen.getByTestId("upload-acknowledge").textContent).toBe(ack);
    fireEvent.click(screen.getByRole("button", { name: "Approve upload" }));
    expect(calls("/api/instruct/uploads/up_1/approve")).toHaveLength(0);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Confirm: upload it now" })));
    expect(calls("/api/instruct/uploads/up_1/approve")).toHaveLength(1);
    expect(screen.getByTestId("upload-preview").textContent).toContain("Uploaded to SharePoint.");
    expect(screen.getByTestId("upload-preview").textContent).not.toContain("recorded, not made");
    expect(screen.queryByTestId("upload-acknowledge")).toBeNull();
  });

  it("lists every blank of a filled form with its source before the copy can be saved", async () => {
    const cite = { record: "PurchaseOrder:90412", field: "po_number", source: "bc://po/1@a", open_url: "/source?handle=bc" };
    const fill = {
      name: "Questionnaire (filled).xlsx",
      original: "Questionnaire.xlsx",
      kind: "xlsx" as const,
      record: { object_id: "PurchaseOrder:90412", type: "PurchaseOrder", label: "PO 90412", open_url: "/object/PurchaseOrder/x" },
      counts: { filled: 1, kept: 0, person_only: 1, unknown_field: 0, no_fact: 0, ambiguous: 1 },
      fields: [
        { label: "PO Number", before: "", after: "90412", status: "filled" as const, reason: "", citation: cite },
        { label: "Carrier", before: "", after: "", status: "ambiguous" as const, reason: "2 different values", citation: null },
        { label: "Signature", before: "", after: "", status: "person_only" as const, reason: "", citation: null },
      ],
      notes: [],
      digest_before: "a",
      digest_after: "b",
      size: 4096,
      download_url: "/api/instruct/files/f_2",
      upload_id: "up_2",
      folder: "/Purchasing/Quotes",
      reply: null,
    };
    replies = [{ ...PREVIEW, upload_id: "up_2", name: fill.name }];
    render(<InstructionPart part={{ ...DRAFTED, writes: [], documents: [], fills: [fill] }} answerId="f1" onInstruct={vi.fn()} />);
    const rows = screen.getByTestId("fill-fields").querySelectorAll("tbody tr");
    expect([...rows].map((r) => r.getAttribute("data-status"))).toEqual(["filled", "ambiguous", "person_only"]);
    expect(rows[0].textContent).toContain("90412");
    expect(screen.getByRole("link", { name: "PurchaseOrder:90412 · po_number" }).getAttribute("href")).toBe("/source?handle=bc");
    expect(rows[1].textContent).toContain("The records disagree");
    // saving is the upload's own preview and confirm: nothing approved from the review
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Preview upload" })));
    expect(calls("/api/instruct/uploads/up_2")).toHaveLength(1);
    expect(calls("/api/instruct/uploads/up_2/approve")).toHaveLength(0);
  });
});
