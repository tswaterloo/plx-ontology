import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { WriteIntentView, WritePreview } from "@/lib/types";
import { concerns, WritePanel } from "./write-panel";

const api = vi.hoisted(() => ({ getWrites: vi.fn(), previewWrite: vi.fn(), approveWrite: vi.fn(), executeWrite: vi.fn() }));
vi.mock("@/lib/api", async (original) => ({ ...(await original<object>()), ...api }));

const intent = { intent_id: "wi_0123456789abcdef01234567", operation: "purchase_order_line_update", record_id: "line-1", citations: ["bc://production/c/purchaseOrders/po-9@1"], precondition: { etag: 'W/"1"' } };
const view = (over: Partial<WriteIntentView> = {}): WriteIntentView => ({ intent, status: "prepared", text: "Prepared, waiting for approval", at: "2026-11-08T12:00:00+00:00", missing: [], history: [], replayed: false, ...over });
const preview: WritePreview = {
  intent_id: intent.intent_id, intent, label: "Purchase order line change", current_etag: 'W/"1"',
  changes: [{ field: "quantity", current: 10, proposed: "12", changed: true }], lines: [],
  warnings: [{ code: "price_outside_sheet", text: "Line 1: 3.00 is outside the price sheet (1 to 2)." }], problems: [],
  request: { method: "PATCH", path: "/v2.0/{tenant}/Production/api/v2.0/companies(c)/purchaseOrderLines(line-1)" },
  undo: "PATCH the line back", live_missing: ["Writing is off (PLX_WRITES_LIVE=1 not set)"], preview_hash: "h".repeat(64),
};

describe("WritePanel", () => {
  it("matches entries by record id or citation, never an unrelated one", () => {
    expect(concerns(view(), ["po-9"])).toBe(true);
    expect(concerns(view(), ["line-1"])).toBe(true);
    expect(concerns(view(), ["po-10x"])).toBe(false);
    expect(concerns(view(), [""])).toBe(false);
  });

  it("previews field by field, approves only after the warning is acknowledged and a second confirm click, then runs as a dry run", async () => {
    api.getWrites.mockResolvedValue({ items: [view()] });
    api.previewWrite.mockResolvedValue(preview);
    api.approveWrite.mockResolvedValue(view({ status: "approved", text: "Approved by Vince" }));
    api.executeWrite.mockResolvedValue(view({ status: "would_write", text: "Would write (dry run): Writing is off", request: { method: "PATCH" } }));
    render(<WritePanel match={["po-9"]} />);
    fireEvent.click(await screen.findByTestId("write-preview"));
    expect(await screen.findByTestId("write-changes")).toHaveTextContent("quantity1012");
    const approve = screen.getByTestId("write-approve");
    expect(approve).toBeDisabled();
    fireEvent.click(screen.getByTestId("write-ack"));
    fireEvent.click(screen.getByTestId("write-approve"));
    expect(api.approveWrite).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("write-confirm"));
    await waitFor(() => expect(api.approveWrite).toHaveBeenCalledWith(intent.intent_id, { confirm: true, preview_hash: preview.preview_hash, acknowledge_warnings: true }));
    fireEvent.click(await screen.findByTestId("write-execute"));
    await waitFor(() => expect(screen.getByTestId("write-result")).toHaveTextContent("Would write (dry run)"));
    expect(api.executeWrite.mock.calls[0][1].idempotency_key).toMatch(/^[A-Za-z0-9_-]{16,80}$/);
  });
});
