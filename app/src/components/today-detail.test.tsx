import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ObjectPage, ObjectSection } from "@/lib/types";
import { TodayDetail, type QueueRow } from "./today-detail";

const api = vi.hoisted(() => ({ getObject: vi.fn(), getObjectSection: vi.fn() }));
vi.mock("@/lib/api", async (original) => ({ ...(await original<object>()), ...api }));

const row: QueueRow = {
  key: "po-1", group: "po_late", type: "PurchaseOrder", objectId: "PurchaseOrder:x:1", title: "PO 106001",
  counterpart: "DSM", line: "Needed 24 Jun", days: 12, status: "late", href: "/po/106001",
};
const quick = { object_id: row.objectId, type: "PurchaseOrder", label: "106001", complete: false, freshness: {}, facts: [], related: [],
  status: { state: "late", text: "late", sources: [] } } as unknown as ObjectPage;

describe("Today's detail of a late PO", () => {
  it("says nothing about the supplier's date until the trail is read, then what the trail found", async () => {
    let answer: (s: ObjectSection) => void = () => undefined;
    api.getObject.mockResolvedValue(quick);
    api.getObjectSection.mockReturnValue(new Promise<ObjectSection>((r) => (answer = r)));
    render(<TodayDetail row={row} onDone={() => undefined} />);
    await waitFor(() => expect(api.getObjectSection).toHaveBeenCalled());
    expect(screen.getByText("12 days late")).toBeTruthy();
    // the quick first read has no trail: no "no date" claim the trail may contradict a moment later
    expect(screen.queryByText("No date from the supplier")).toBeNull();
    await act(async () =>
      answer({ object_id: row.objectId, section: "trail", trail: { kind: "purchase_order", title: "How we know", trails: [
        { subject: { object_id: row.objectId, type: "PurchaseOrder", label: "106001", open_url: "/po/106001" }, state: "late", days_late: 12,
          eta: { state: "confirmed", text: "Confirmed" }, steps: [] }] } } as unknown as ObjectSection),
    );
    await waitFor(() => expect(screen.getByText(/Supplier confirmed/)).toBeTruthy());
    expect(screen.queryByText("No date from the supplier")).toBeNull();
  });
});
