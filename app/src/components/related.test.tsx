import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/errors";
import type { AutopilotAuditRow, MemoryItem, Template, WorkLogEvent } from "@/lib/types";
import { forgetRelatedReads, itemDocTypes, itemRecords, Related } from "./related";

const api = vi.hoisted(() => ({ getMemory: vi.fn(), getTemplates: vi.fn(), getWorkLog: vi.fn(), getAutopilotAudit: vi.fn(), fillTemplate: vi.fn() }));
vi.mock("@/lib/api", async (original) => ({ ...(await original<object>()), ...api }));
const who = vi.hoisted(() => ({ owner: false, viewing: null as null | { id: string } }));
vi.mock("@/components/auth", () => ({ useAuth: () => ({ state: { status: "signed-in", me: { principal: who.owner ? "owner@x" : "ana@x", owner: who.owner } } }) }));
vi.mock("@/components/view-as", () => ({ useViewingAs: () => who.viewing }));

const PO = "PurchaseOrder:PO-90412";
const SO = "SalesOrder:SO-5512";
const mem = (id: number, text: string, records: string[]): MemoryItem => ({ id, text, records, kind: "fact", at: `2026-11-1${id}T12:00:00Z`, confidence: 1, importance: 1, source: "you" });
const tpl = (id: number, name: string, doc_type: Template["doc_type"], over: Partial<Template> = {}): Template => ({
  id, name, doc_type, body: "", created_at: "2026-11-01T00:00:00Z", updated_at: "2026-11-01T00:00:00Z", mine: true, style_notes: "", uses: 0, ...over,
});
const ev = (seq: number, title: string, at: string): WorkLogEvent => ({ seq, at, mine: true, kind: "action", verb: "sent", title, thread: `card:${seq}`, source: "actions_journal", records: [], people: [], pinned: false });
const run = (item_id: string, event: string, status: string, at: string): AutopilotAuditRow => ({ at, event, status, item_id, workflow: "supplier_eta", undoable: false, undone: false, mode: "dry_run" });

beforeEach(() => {
  forgetRelatedReads();
  Object.values(api).forEach((f) => f.mockReset());
  who.owner = false;
  who.viewing = null;
  api.getMemory.mockResolvedValue({ available: true, items: [mem(1, "Vendor ships Tuesdays only", [PO]), mem(2, "Kestrel wants the PO number in the subject", [SO])] });
  api.getTemplates.mockResolvedValue({
    available: true,
    templates: [tpl(5, "ETA chase", "supplier_email", { uses: 9 }), tpl(6, "Made from this PO", "po_note", { source_ref: PO }), tpl(7, "Order confirmation", "order_confirmation")],
  });
  api.getWorkLog.mockImplementation(async ({ record }: { record: string }) => ({
    all_readers: false, offset: 0, total: null,
    events: record === PO ? [ev(1, "Asked the vendor for a date", "2026-11-12T14:00:00Z"), ev(2, "Shared ETA", "2026-11-13T14:00:00Z")] : [ev(2, "Shared ETA", "2026-11-13T14:00:00Z")],
  }));
  api.getAutopilotAudit.mockResolvedValue({ live: 0, rows: [run("it-1", "outcome", "prepared", "2026-11-13T15:00:00Z"), run("it-2", "outcome", "rejected", "2026-11-13T16:00:00Z")] });
});

describe("Related panel", () => {
  it("on a drafted autopilot item: what the draft used is left to DraftUse, the rest is marked not applied", async () => {
    api.getMemory.mockResolvedValue({ available: true, items: [mem(1, "Vendor ships Tuesdays only", [PO]), mem(3, "Ask for tracking on every chase", [PO]), mem(4, "Old address, moved", [PO])] });
    render(<Related records={[PO]} label="PO 90412" docTypes={["supplier_email"]} itemId="it-1" used={{ memory: [{ id: 1, text: "Vendor ships Tuesdays only" }], related: [{ id: 3, text: null }], template: { id: 5, name: "ETA chase" } }} />);
    const block = await screen.findByTestId("related-memory");
    expect(block).toHaveTextContent("Memory, not applied");
    expect(block).toHaveTextContent("Old address, moved");
    // listed once: the applied entry and the one read for the draft stay in DraftUse
    expect(block).not.toHaveTextContent("Vendor ships Tuesdays only");
    expect(block).not.toHaveTextContent("Ask for tracking");
    const tpls = await screen.findByTestId("related-templates");
    expect(tpls).toHaveTextContent("Templates, not applied");
    expect(tpls).toHaveTextContent("Made from this PO");
    expect(tpls).not.toHaveTextContent("ETA chase");
  });

  it("on a drafted item whose entries all went into the draft: says nothing else, not nothing at all", async () => {
    api.getMemory.mockResolvedValue({ available: true, items: [mem(1, "Vendor ships Tuesdays only", [PO])] });
    api.getTemplates.mockResolvedValue({ available: true, templates: [] });
    api.getWorkLog.mockResolvedValue({ all_readers: false, offset: 0, total: null, events: [] });
    render(<Related records={[PO]} used={{ memory: [{ id: 1, text: "Vendor ships Tuesdays only" }] }} />);
    expect(await screen.findByTestId("related-empty")).toHaveTextContent("Nothing else in your memory, templates or work log on this.");
    expect(screen.queryByTestId("related-memory")).toBeNull();
  });

  it("on a record: only that record's memory, its own template first, the log once per entry, links into each page", async () => {
    render(<Related records={[PO, "Supplier:V-77"]} label="PO 90412" docTypes={["supplier_email"]} />);
    await screen.findByTestId("related-memory");
    expect(screen.getByTestId("related-memory")).toHaveTextContent("Vendor ships Tuesdays only");
    expect(screen.getByTestId("related-memory")).not.toHaveTextContent("Kestrel");
    const names = screen.getAllByTestId("related-template").map((li) => li.textContent);
    expect(names[0]).toContain("Made from this PO");
    expect(names[1]).toContain("ETA chase");
    expect(names.join(" ")).not.toContain("Order confirmation");
    // the same entry on both records is listed once
    expect(screen.getAllByTestId("related-log-row")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "Memory" }).getAttribute("href")).toBe(`/memory?record=${encodeURIComponent(PO)}`);
    expect(screen.getByRole("link", { name: "Work log" }).getAttribute("href")).toBe(`/work-log?record=${encodeURIComponent(PO)}`);
    // a reader other than the owner never asks for the audit log
    expect(api.getAutopilotAudit).not.toHaveBeenCalled();
  });

  it("a long work-log line wraps on a phone: its link is never inside a clipped, unwrapped line", async () => {
    const long = "These are packaging items with the cost of each line and who supplied it, across every open purchase order this quarter";
    api.getWorkLog.mockResolvedValue({ all_readers: false, offset: 0, total: null, events: [ev(9, long, "2026-11-13T14:00:00Z")] });
    render(<Related records={[PO]} label="PO 90412" dense />);
    const link = await screen.findByRole("link", { name: long });
    for (let p: HTMLElement | null = link; p; p = p.parentElement) {
      expect(p.className).not.toMatch(/(^|\s)(truncate|whitespace-nowrap|text-nowrap)(\s|$)/);
    }
    expect(screen.getByTestId("related-log-text").className).toContain("[overflow-wrap:anywhere]");
  });

  it("fills a template for the record in place", async () => {
    api.fillTemplate.mockResolvedValue({ template_id: 6, name: "Made from this PO", doc_type: "po_note", text: "PO 90412 is due", fields: [], record: PO });
    render(<Related records={[PO]} label="PO 90412" />);
    const fill = (await screen.findAllByTestId("related-template-fill"))[0];
    expect(fill).toHaveTextContent("Fill for PO 90412");
    fireEvent.click(fill);
    await waitFor(() => expect(api.fillTemplate).toHaveBeenCalledWith(6, PO));
  });

  it("inline in Autopilot the owner sees this item's run log only", async () => {
    who.owner = true;
    render(<Related records={[PO]} itemId="it-1" workflow="supplier_eta" dense />);
    const log = await screen.findByTestId("related-run-log");
    expect(screen.getAllByTestId("related-run-row")).toHaveLength(1);
    expect(log).toHaveTextContent("Prepared");
    expect(log).not.toHaveTextContent("Rejected");
    expect(screen.getByRole("link", { name: "Audit log" }).getAttribute("href")).toBe("/autopilot-log");
  });

  it("viewing as someone reads none of their memory, templates or log", () => {
    who.viewing = { id: "ana" };
    const { container } = render(<Related records={[PO]} itemId="it-1" />);
    expect(container).toBeEmptyDOMElement();
    for (const f of Object.values(api)) expect(f).not.toHaveBeenCalled();
  });

  it("a read that fails says so and the others still show", async () => {
    api.getMemory.mockRejectedValue(new Error("down"));
    render(<Related records={[PO]} />);
    expect(await screen.findByTestId("related-failed")).toHaveTextContent("Couldn't read your memory just now.");
    expect(screen.getAllByTestId("related-log-row").length).toBeGreaterThan(0);
  });

  it("the page measure's session, refused the reader's own memory and log, shows no failure", async () => {
    api.getMemory.mockRejectedValue(new ApiError(403, "canary_route_refused"));
    api.getWorkLog.mockRejectedValue(new ApiError(403, "canary_route_refused"));
    render(<Related records={[PO]} />);
    await waitFor(() => expect(screen.queryByText(/Reading your memory/)).toBeNull());
    expect(screen.queryByTestId("related-failed")).toBeNull();
    // and never says the memory it was not served holds nothing
    expect(screen.queryByTestId("related-empty")).toBeNull();
  });

  it("memory that can't be read (people list not loaded) is a failure, never an empty memory", async () => {
    api.getMemory.mockResolvedValue({ available: false, items: [mem(1, "Vendor ships Tuesdays only", [PO])] });
    render(<Related records={[PO]} />);
    expect(await screen.findByTestId("related-failed")).toHaveTextContent("memory");
    expect(screen.queryByTestId("related-memory")).toBeNull();
  });

  it("nothing kept says so in one line and still links to each page", async () => {
    api.getMemory.mockResolvedValue({ available: true, items: [] });
    api.getTemplates.mockResolvedValue({ available: true, templates: [] });
    api.getWorkLog.mockResolvedValue({ all_readers: false, offset: 0, total: 0, events: [] });
    render(<Related records={[PO]} />);
    expect(await screen.findByTestId("related-empty")).toHaveTextContent("hold nothing on this yet");
    expect(screen.getByTestId("related-links")).toHaveTextContent("MemoryTemplatesWork log");
  });

  it("ten panels on one screen read memory and templates once", async () => {
    render(
      <>
        {Array.from({ length: 10 }, (_, i) => (
          <Related key={i} records={[`PurchaseOrder:PO-${i}`]} />
        ))}
      </>,
    );
    await waitFor(() => expect(screen.getAllByTestId("related-panel")).toHaveLength(10));
    await waitFor(() => expect(api.getWorkLog).toHaveBeenCalledTimes(10));
    expect(api.getMemory).toHaveBeenCalledTimes(1);
    expect(api.getTemplates).toHaveBeenCalledTimes(1);
  });

  it("an autopilot item's records and template types come from what happened; unknown inputs give none", () => {
    expect(itemRecords({ object_id: PO, records: [PO, SO, "not an id", 7] })).toEqual([PO, SO]);
    expect(itemRecords(undefined)).toEqual([]);
    expect(itemDocTypes("mail", { side: "supplier" })).toEqual(["supplier_email", "phrasing"]);
    expect(itemDocTypes("fiix", {})).toEqual([]);
  });
});
