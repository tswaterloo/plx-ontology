import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { WriteIntentView, WritePreview } from "@/lib/types";
import { ApiError } from "@/lib/errors";
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
    // an intent id is matched whole: a short hex id inside it names nothing
    expect(concerns(view(), ["0123456789abcdef"])).toBe(false);
    expect(concerns(view(), [intent.intent_id.toUpperCase()])).toBe(true);
  });

  it("an Ask-prepared entry is found by its own intent id when it names no record the page knows (WEB-007)", async () => {
    const own = { ...intent, record_id: "unrelated-9", citations: [] };
    expect(concerns(view({ intent: own }), [intent.intent_id])).toBe(true);
    expect(concerns(view({ intent: own }), ["wi_ffffffffffffffffffffffff"])).toBe(false);
    api.getWrites.mockResolvedValue({ items: [view({ intent: own })] });
    render(<WritePanel match={[intent.intent_id]} />);
    expect(await screen.findByTestId("write-preview")).toBeInTheDocument();
  });

  it("a record that changed after the preview resets the page to preview again, re-acknowledge, re-approve (WEB-009)", async () => {
    api.approveWrite.mockReset();
    api.getWrites.mockResolvedValue({ items: [view()] });
    api.previewWrite.mockResolvedValue(preview);
    api.approveWrite.mockRejectedValueOnce(new ApiError(409, "action_refused", undefined, "The entry or the record changed since your preview: review it again before you approve."));
    api.approveWrite.mockResolvedValueOnce(view({ status: "approved", text: "Approved by Vince" }));
    render(<WritePanel match={["po-9"]} />);
    fireEvent.click(await screen.findByTestId("write-preview"));
    fireEvent.click(await screen.findByTestId("write-ack"));
    fireEvent.click(screen.getByTestId("write-approve"));
    fireEvent.click(screen.getByTestId("write-confirm"));
    expect(await screen.findByRole("alert")).toHaveTextContent("changed since your preview");
    // back at a preview button, the old acknowledgement and arming gone
    expect(screen.queryByTestId("write-confirm")).toBeNull();
    expect(screen.queryByTestId("write-ack")).toBeNull();
    fireEvent.click(screen.getByTestId("write-preview"));
    await screen.findByTestId("write-changes");
    expect(screen.getByTestId("write-approve")).toBeDisabled(); // must acknowledge again
    fireEvent.click(screen.getByTestId("write-ack"));
    fireEvent.click(screen.getByTestId("write-approve"));
    fireEvent.click(screen.getByTestId("write-confirm"));
    await waitFor(() => expect(api.approveWrite).toHaveBeenCalledTimes(2));
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
    expect(api.executeWrite.mock.calls[0][1].live).toBe(false);
  });

  it("says a live write when the preview lacks nothing, and asks the server for exactly that", async () => {
    api.executeWrite.mockClear();
    api.getWrites.mockResolvedValue({ items: [view({ status: "approved", text: "Approved by Vince" })] });
    api.previewWrite.mockResolvedValue({ ...preview, warnings: [], live_missing: [] });
    api.executeWrite.mockResolvedValue(view({ status: "written", text: "Written", request: { method: "PATCH" } }));
    render(<WritePanel match={["po-9"]} />);
    fireEvent.click(await screen.findByTestId("write-preview"));
    expect(await screen.findByTestId("write-mode")).toHaveTextContent("This writes to Business Central now");
    const button = screen.getByTestId("write-execute");
    expect(button).toHaveTextContent("Write to Business Central");
    expect(button).not.toHaveTextContent("dry run");
    fireEvent.click(button);
    await waitFor(() => expect(api.executeWrite).toHaveBeenCalledTimes(1));
    expect(api.executeWrite.mock.calls[0][1].live).toBe(true);
  });

  it("an approved entry runs only from a preview, and a preview that lacks something is a dry run", async () => {
    api.executeWrite.mockClear();
    api.getWrites.mockResolvedValue({ items: [view({ status: "approved", text: "Approved by Vince" })] });
    api.previewWrite.mockResolvedValue({ ...preview, warnings: [], live_missing: ["the BC write switch is off"] });
    api.executeWrite.mockResolvedValue(view({ status: "would_write", text: "Would write (dry run)", request: { method: "PATCH" } }));
    render(<WritePanel match={["po-9"]} />);
    await screen.findByTestId("write-preview");
    expect(screen.queryByTestId("write-execute")).toBeNull();   // nothing runs unseen
    fireEvent.click(screen.getByTestId("write-preview"));
    expect(await screen.findByTestId("write-mode")).toHaveTextContent("Dry run: nothing is written to Business Central (the BC write switch is off).");
    expect(screen.getByTestId("write-execute")).toHaveTextContent("Write (dry run)");
    fireEvent.click(screen.getByTestId("write-execute"));
    await waitFor(() => expect(api.executeWrite).toHaveBeenCalledTimes(1));
    expect(api.executeWrite.mock.calls[0][1].live).toBe(false);
  });
});
