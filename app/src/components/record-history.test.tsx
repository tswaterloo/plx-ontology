import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RecordHistory } from "./record-history";

vi.mock("next/navigation", () => ({ useSearchParams: () => null, useRouter: () => ({ push: vi.fn() }) }));
afterEach(() => vi.unstubAllGlobals());

const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { "content-type": "application/json" } });
const page = (over: object) => ({ object_id: "so1", type: "SalesOrder", label: "SO 1", entries: [], next_cursor: null, partial: false, message: null, ...over });
const changed = (at: string, prop: string, before: string[], after: string[]) => ({
  at, kind: "changed", title: `Changed ${prop}`, source: "Portal sales order", source_handle: "api://x@1",
  changes: [{ property: prop, label: prop, before, after, more: false }], more_changes: false, link: null, link_label: null,
});

function serve(handler: (url: string) => Response) {
  const f = vi.fn(async (url: string) => handler(String(url)));
  vi.stubGlobal("fetch", f);
  return f;
}

describe("record history tab", () => {
  it("lists changes newest first with before and after and their source, and pages older ones by cursor", async () => {
    const f = serve((url) =>
      url.includes("cursor=c1")
        ? json(page({ entries: [{ at: "2099-01-01T00:00:00Z", kind: "first_recorded", title: "First recorded from Portal sales order", source: "Portal sales order", changes: [], more_changes: false, link: null, link_label: null }] }))
        : url.includes("/api/history/")
          ? json(page({ entries: [changed("2099-01-05T00:00:00Z", "Customer PO", ["old"], ["new"])], next_cursor: "c1" }))
          : json({}, 404),
    );
    render(<RecordHistory type="SalesOrder" id="so1" />);
    const row = await screen.findByTestId("history-change");
    expect(row.textContent).toContain("old");
    expect(row.textContent).toContain("new");
    expect(screen.getByText("Portal sales order")).toBeTruthy();
    fireEvent.click(screen.getByTestId("history-older"));
    await screen.findByText("First recorded from Portal sales order");
    expect(screen.getAllByTestId("history-entry").map((e) => e.getAttribute("data-kind"))).toEqual(["changed", "first_recorded"]);
    expect(f.mock.calls.some(([u]) => String(u).includes("cursor=c1"))).toBe(true);
    expect(screen.queryByTestId("history-older")).toBeNull();
  });

  it("says some changes are hidden without a number", async () => {
    serve(() => json(page({ entries: [changed("2099-01-05T00:00:00Z", "Customer PO", [], ["x"])], partial: true })));
    render(<RecordHistory type="SalesOrder" id="so1" />);
    const note = await screen.findByTestId("history-partial");
    expect(note.textContent).not.toMatch(/\d/);
  });

  it("reads the record as recorded on a chosen date", async () => {
    const f = serve((url) =>
      url.includes("/api/as-of/")
        ? json({ object_id: "so1", type: "SalesOrder", label: "SO 1", at: "2099-01-03T23:59:59.999Z", values: [{ label: "Customer PO", values: ["old"], source: null }], links: [{ label: "Invoices", count: 1 }], partial: false, message: null })
        : json(page({ message: "No changes are recorded that you may see." })),
    );
    render(<RecordHistory type="SalesOrder" id="so1" />);
    await screen.findByText("No changes are recorded that you may see.");
    fireEvent.change(screen.getByTestId("as-of-day"), { target: { value: "2099-01-03" } });
    fireEvent.click(screen.getByTestId("as-of-show"));
    const result = await screen.findByTestId("as-of-result");
    expect(result.textContent).toContain("old");
    await waitFor(() => expect(f.mock.calls.some(([u]) => String(u).includes("/api/as-of/SalesOrder/so1?at=2099-01-03"))).toBe(true));
  });
});
