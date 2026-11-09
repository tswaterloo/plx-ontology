import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { MyWork, MyWorkItem } from "@/lib/types";
import { MyWorkList } from "./my-work";

const base: MyWorkItem = {
  item_id: "ap_1", autopilot: "customer_po", label: "Customer PO received", title: "Customer PO received: PO 1 is entered",
  mine: true, happened: { object_id: "Email:x:1" }, citations: ["mail://synth/1"], due_at: "2026-11-08T17:00:00Z",
  created_at: "2026-11-08T15:00:00Z", card_id: "autopilot:ap_1", offers: [], write_ready: true,
  prepared: { summary: "PO 1 is entered as order 7", draft: { body: "Hello, received." }, gaps: ["lines_from_po_document"] },
};

describe("my work", () => {
  it("shows the prepared work with approve only on the reader's own items", () => {
    const work: MyWork = { status: "read", sent: 0, writes: 0, digest: { open: 2, due_today: 2, by_autopilot: {}, nudges: [] },
      items: [base, { ...base, item_id: "ap_2", mine: false, card_id: null, write_ready: false }] };
    render(<MyWorkList work={work} />);
    expect(screen.getAllByTestId("my-work-item")).toHaveLength(2);
    expect(screen.getAllByTestId("my-work-entry")).toHaveLength(1);
    expect(screen.getByText(/a report's item \(view only\)/)).toBeTruthy();
    expect(screen.getAllByText(/Missing: lines from po document/)).toHaveLength(2);
  });

  it("says when the autopilot is switched off", () => {
    render(<MyWorkList work={{ status: "off", sent: 0, writes: 0, items: [], digest: { open: 0, due_today: 0, by_autopilot: {}, nudges: [] } }} />);
    expect(screen.getByText(/switched off/)).toBeTruthy();
  });
});
