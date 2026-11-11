import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { WorkLogEvent } from "@/lib/types";
import { continueHref, WorkLogRow } from "./work-log";

const api = vi.hoisted(() => ({ organiseWorkThread: vi.fn() }));
vi.mock("@/lib/api", async (original) => ({ ...(await original<object>()), ...api }));

const sent = (over: Partial<WorkLogEvent> = {}): WorkLogEvent => ({
  seq: 7, at: "2026-11-10T15:00:00Z", mine: true, kind: "action", verb: "sent", title: "PO 90412 ship date",
  thread: "card:card-1", source: "actions_journal", records: [], people: ["buyer@vendor.example"], pinned: false,
  loop: { state: "overdue", nudge: "No reply from buyer@vendor.example in 3 days.", follow_up: "Draft a short follow-up" }, ...over,
});

describe("work log", () => {
  it("continues each kind of thread where it lives", () => {
    expect(continueHref("ask:abc123")).toBe("/ask?c=abc123");
    expect(continueHref("autopilot:it-9")).toBe("/autopilot?item=it-9");
    expect(continueHref("record:PurchaseOrder/PO-90412")).toContain("PO-90412");
    expect(continueHref("record:saved/question")).toBe("/work-log");
  });

  it("an overdue send nudges, drafts the follow-up in Ask and can be closed by its person", async () => {
    api.organiseWorkThread.mockResolvedValue({});
    const changed = vi.fn();
    render(<ul><WorkLogRow e={sent()} onChanged={changed} /></ul>);
    expect(screen.getByText(/No reply from buyer@vendor.example/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Draft a follow-up" }).getAttribute("href")).toBe("/ask?q=Draft%20a%20short%20follow-up");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(api.organiseWorkThread).toHaveBeenCalledWith("card:card-1", { closed: true }));
    expect(changed).toHaveBeenCalled();
  });

  it("someone else's line (the owner's view) offers no continue, pin or close", () => {
    render(<ul><WorkLogRow e={sent({ mine: false, reader_name: "Ana" })} showWho /></ul>);
    expect(screen.getByText("Ana")).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Continue" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Close" })).toBeNull();
  });
});
