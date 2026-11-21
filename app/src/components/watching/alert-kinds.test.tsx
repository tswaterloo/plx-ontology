import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const calls = vi.hoisted(() => ({ items: [] as Array<[string, number]> }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getAlertKinds: () =>
    Promise.resolve({
      rule: "",
      kinds: [{ kind: "late_po", label: "Late PO", surface: "alert" }],
      types: [{ type: "po_late", kind: "late_po", label: "Late purchase order", default: "on", home: true, precision: null }],
    }),
  getBriefing: () => Promise.resolve({ status: "read", groups: [{ event_type: "po_late", count: 2 }] }),
  getBriefingItems: (type: string, offset: number) => {
    calls.items.push([type, offset]);
    const item = (n: string) => ({ id: `pe-${n}`, event_type: type, label: "Late purchase order", object_id: `PurchaseOrder:demo:${n}`, object_type: "PurchaseOrder", subject_label: n, summary: "No supplier date", open_url: "" });
    return Promise.resolve(offset === 0 ? { event_type: type, total: 2, offset: 0, more: true, items: [item("91000")] } : { event_type: type, total: 2, offset: 1, more: false, items: [item("91001")] });
  },
}));

import { AlertKindsSection, TypeItems } from "./alert-kinds";

describe("Watching alert counts", () => {
  it("open the alerts they count, a page at a time, each linked to its record", async () => {
    render(<AlertKindsSection />);
    await userEvent.click(await screen.findByText("Late PO"));
    expect(calls.items).toEqual([]); // nothing read until asked
    await userEvent.click(await screen.findByRole("button", { name: "Show 2" }));
    const list = await screen.findByTestId("items-po_late");
    const first = within(list).getByRole("link", { name: /PO 91000/ });
    expect(first).toHaveAttribute("href", "/object/PurchaseOrder/PurchaseOrder%3Ademo%3A91000");
    await userEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(await within(list).findByRole("link", { name: /PO 91001/ })).toBeInTheDocument();
    expect(calls.items).toEqual([["po_late", 0], ["po_late", 1]]);
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
  });
});

describe("A kind's further alerts", () => {
  it("never lists again the rows already shown above them", async () => {
    calls.items = [];
    render(<TypeItems type="po_late" count={1} more="1 more" skip={["pe-91000"]} />);
    await userEvent.click(screen.getByRole("button", { name: "1 more" }));
    // the first page held only the row shown above: nothing listed, never "none of these"
    await userEvent.click(await screen.findByRole("button", { name: "Show more" }));
    expect(screen.queryByText(/None of these/)).toBeNull();
    // the next page starts past the rows read, the skipped one included
    const list = await screen.findByTestId("items-po_late");
    expect(await within(list).findByRole("link", { name: /PO 91001/ })).toBeInTheDocument();
    expect(within(list).queryByRole("link", { name: /PO 91000/ })).toBeNull();
    expect(calls.items).toEqual([["po_late", 0], ["po_late", 1]]);
  });
});

describe("Watching alert kind rows", () => {
  it("say On or Off on every row, a page-surface kind included", async () => {
    const { surfaceWord } = await import("./alert-kinds");
    const on = [{ default: "on" as const }];
    const off = [{ default: "off" as const }];
    expect(surfaceWord({ label: "Data health", surface: "data_health", types: on })).toBe("On");
    expect(surfaceWord({ label: "Data health", surface: "data_health", types: off })).toBe("Off");
    expect(surfaceWord({ label: "Stale records", surface: "data_health", types: on })).toBe("On · Data health");
    expect(surfaceWord({ label: "Week", surface: "weekly", types: off })).toBe("Off · Weekly review");
    expect(surfaceWord({ label: "Late PO", surface: "alert", types: off })).toBe("Off");
  });
});
