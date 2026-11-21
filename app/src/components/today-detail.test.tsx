import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/errors";
import type { BizPage, ObjectPage, ObjectSection } from "@/lib/types";
import { doorReceived, TodayDetail, type QueueRow } from "./today-detail";
import { SuggestionActions } from "./record/next-step";
import type { ProactiveSuggestion } from "@/lib/types";

const api = vi.hoisted(() => ({ getObject: vi.fn(), getObjectSection: vi.fn(), bizPage: vi.fn() }));
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
    // the server has no door page for it (switched off): the record's own page
    api.bizPage.mockRejectedValue(new ApiError(404, "pages_off"));
    api.getObject.mockResolvedValue(quick);
    api.getObjectSection.mockReturnValue(new Promise<ObjectSection>((r) => (answer = r)));
    render(<TodayDetail row={row} onDone={() => undefined} />);
    await waitFor(() => expect(api.getObjectSection).toHaveBeenCalled());
    expect(screen.getByText("12 days late")).toBeTruthy();
    // the quick first read has no trail: no "no date" claim the trail may contradict a moment later
    expect(screen.queryByText("No date from the supplier")).toBeNull();
    await act(async () =>
      answer({ object_id: row.objectId, section: "trail", trail: { kind: "purchase_order", title: "Evidence", trails: [
        { subject: { object_id: row.objectId, type: "PurchaseOrder", label: "106001", open_url: "/po/106001" }, state: "late", days_late: 12,
          eta: { state: "confirmed", text: "Confirmed" }, steps: [] }] } } as unknown as ObjectSection),
    );
    await waitFor(() => expect(screen.getByText(/Supplier confirmed/)).toBeTruthy());
    expect(screen.queryByText("No date from the supplier")).toBeNull();
  });
});

describe("Today's detail through the pages door", () => {
  const door = {
    kind: "purchase_order", title: "PO100045", subtitle: "Purchase order", sources: "", took_ms: 40,
    facts: [
      { label: "Supplier", value: { text: "EZ Chemicals", href: "/account?kind=supplier&h=bc%3A%2F%2Fv1", hidden: false } },
      { label: "Status", value: { text: "open", hidden: false } },
      { label: "Ordered", value: { day: "2026-09-01", hidden: false } },
      { label: "Total (sum of lines)", value: { money: { amount: "10083.00", currency: "CAD" }, hidden: false } },
    ],
    sections: [{ key: "lines", title: "Lines", columns: ["Item", "Description", "Ordered", "Received", "Invoiced", "Unit", "Unit price", "Line total", "Expected"],
      rows: [[{ text: "RM-1" }, { text: "a" }, { number: "10" }, { number: "4" }, {}, { text: "kg" }, {}, {}, {}],
             [{ text: "RM-2" }, { text: "b" }, { number: "5" }, { number: "5" }, {}, { text: "kg" }, {}, {}, {}]].map((r) => r.map((c) => ({ hidden: false, ...c }))) }],
  } as unknown as BizPage;
  const doorRow: QueueRow = { ...row, key: "po-2", objectId: "PurchaseOrder:portal.app_purchase_order:2", title: "PO PO100045",
    counterpart: null, href: "/order?kind=purchase_order&h=api%3A%2F%2Fpo%2F2" };

  it("reads the door page the link opens, never the old record page, and shows its figures", async () => {
    api.getObject.mockReset();
    api.bizPage.mockReset().mockResolvedValue(door);
    api.getObjectSection.mockReset().mockReturnValue(new Promise(() => undefined));
    render(<TodayDetail row={doorRow} onDone={() => undefined} />);
    await waitFor(() => expect(screen.getAllByText("Total (sum of lines)").length).toBeGreaterThan(0));
    expect(api.bizPage).toHaveBeenCalledWith("purchase_order", { h: "api://po/2", record: undefined }, expect.anything(), true);
    expect(api.getObject).not.toHaveBeenCalled();
    expect(screen.getAllByText(/10,083/).length).toBeGreaterThan(0);
    // received against ordered from the lines, in their one unit
    expect(screen.getByText("9 of 15")).toBeTruthy();
    // the supplier links to its account page over the door
    expect(screen.getAllByRole("link", { name: "EZ Chemicals" })[0].getAttribute("href")).toBe("/account?kind=supplier&h=bc%3A%2F%2Fv1");
  });

  it("falls back to the record's own page when the door has none for it", async () => {
    api.bizPage.mockReset().mockRejectedValue(new ApiError(404, "not_found"));
    api.getObject.mockReset().mockResolvedValue(quick);
    api.getObjectSection.mockReset().mockReturnValue(new Promise(() => undefined));
    render(<TodayDetail row={{ ...row, key: "po-3" }} onDone={() => undefined} />);
    await waitFor(() => expect(api.getObject).toHaveBeenCalled());
    expect(api.bizPage).toHaveBeenCalledWith("purchase_order", { h: undefined, record: "PurchaseOrder:x:1" }, expect.anything(), true);
  });
});

describe("Received on a PO's lines", () => {
  const page = (unit: string[]) => ({ sections: [{ key: "lines", columns: ["Ordered", "Received", "Unit"],
    rows: unit.map((u, n) => [{ number: String(10 + n), hidden: false }, { number: n ? "0" : "1", hidden: false }, { text: u, hidden: false }]) }] }) as unknown as BizPage;
  it("adds quantities only in one stated unit: lines with no unit are counted short, never summed", () => {
    expect(doorReceived(page(["kg", "kg"]))).toMatchObject({ got: "1", of: "21", uom: "kg" });
    expect(doorReceived(page(["", ""]))).toMatchObject({ got: "Partly", lines: 2 });
    expect(doorReceived(page([""]))).toMatchObject({ got: "1", of: "10" });
  });
});

describe("A drafted supplier email with no address on record", () => {
  const offer = (kind: string, label: string) => ({ kind, label, outbound: kind === "send_email" || kind === "teams_message" });
  const card = (address: string | null) => ({
    id: "s1", draft: { channel: "email", to: { address, name: null }, subject: "106001: please confirm", body: "Hello" },
    actions: [offer("send_email", "Send email"), offer("teams_message", "Teams message"), offer("mark_done", "Mark done"), offer("snooze", "Snooze")],
  }) as unknown as ProactiveSuggestion;
  it("offers no Review and send as the main action; the send waits under More, saying it needs the address", async () => {
    const { unmount } = render(<SuggestionActions card={card(null)} onDone={() => undefined} />);
    expect(screen.queryByText("Review and send")).toBeNull();
    expect(screen.getByRole("button", { name: "Teams message" })).toBeTruthy();
    act(() => screen.getByRole("button", { name: "More actions" }).dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, button: 0 } as PointerEventInit)));
    expect(await screen.findByText("Send email (add the address)")).toBeTruthy();
    unmount();
    render(<SuggestionActions card={card("orders@dsm.example")} onDone={() => undefined} />);
    expect(screen.getByRole("button", { name: "Review and send" })).toBeTruthy();
  });
});
