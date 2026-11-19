import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ObjectView } from "./object-view";

vi.mock("next/navigation", () => ({
  useParams: () => ({}),
  useSearchParams: () => null,
  usePathname: () => "/",
  useRouter: () => ({ push: vi.fn() }),
}));
afterEach(() => vi.unstubAllGlobals());

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
const po = (status: string) => ({
  object_id: "PurchaseOrder:106093", type: "PurchaseOrder", label: "PO 106093", summary: "",
  status: { state: "read", text: "Read", sources: [] },
  freshness: { as_of: "2026-11-17T22:40:00Z", systems: [{ system: "File", last_read: "2026-11-05T12:00:00Z" }] },
  complete: true,
  facts: [
    { text: `Status: ${status}`, sources: [{ system: "BC", label: "row", fields: { status } }] },
    { text: "Total: 9680.00 USD", sources: [{ system: "BC", label: "row" }] },
  ],
  late_lines: [{ line: "10000", item: "Bottle 500 ml", quantity: "1200", due_date: "2026-12-01", days_late: null }],
  not_found: [{ what: "Supplier confirmation", where_it_would_live: "Ask Purchasing." }, { what: "Receipt", where_it_would_live: "Ask Warehouse." }],
});
const serve = (page: unknown) =>
  vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(String(url).includes("/api/object/") ? json(page) : json({}))));

describe("an order page's layout", () => {
  it("puts the lines under the total, before the evidence and links; no freshness text in the rail", async () => {
    serve(po("Released"));
    render(<ObjectView type="PurchaseOrder" id="106093" />);
    const lines = await screen.findByTestId("late-lines");
    expect(within(lines).getAllByText("Bottle 500 ml")[0]).toBeInTheDocument();
    const after = (el: HTMLElement) => screen.getByTestId("object-title").compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING;
    expect(after(lines)).toBeTruthy();
    const nf = await screen.findByTestId("not-found");
    expect(lines.compareDocumentPosition(nf) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByTestId("freshness")).toBeNull();
    expect(screen.queryByText(/captures not refreshed|some records last read/i)).toBeNull();
  });

  it("says what is not on record once, collapsed, for a released order", async () => {
    serve(po("Released"));
    render(<ObjectView type="PurchaseOrder" id="106093" />);
    const sum = await screen.findByTestId("not-found-summary");
    expect(sum).toHaveTextContent("2 things not on record yet");
    expect((screen.getByTestId("not-found") as HTMLDetailsElement).open).toBe(false);
  });

  it("never lists what is not on record for a draft order", async () => {
    serve(po("Draft"));
    render(<ObjectView type="PurchaseOrder" id="106093" />);
    await screen.findByTestId("late-lines");
    await waitFor(() => expect(screen.getByTestId("object-title")).toBeInTheDocument());
    expect(screen.queryByTestId("not-found")).toBeNull();
    expect(screen.queryByText(/not on record yet/)).toBeNull();
  });

  it("puts the link explorer in the right rail", async () => {
    serve(po("Released"));
    render(<ObjectView type="PurchaseOrder" id="106093" />);
    await screen.findByTestId("late-lines");
    const btn = await screen.findByText(/Explore what this links to/);
    expect(btn.closest("main, [data-testid=record-main]")).toBeNull();
    expect(btn.closest("aside")).not.toBeNull();
  });
});
