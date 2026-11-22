import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ObjectView } from "./object-view";
import type { BizPage } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useParams: () => ({}),
  useSearchParams: () => null,
  usePathname: () => "/",
  useRouter: () => ({ push: vi.fn() }),
}));
afterEach(() => vi.unstubAllGlobals());

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const po = { object_id: "PurchaseOrder:p1", type: "PurchaseOrder", label: "Purchase order PO1", summary: "", status: { state: "read", text: "Read", sources: [] }, freshness: {}, complete: true,
  late_lines: [{ line: "1", item: "", days_late: 4, quantity: "7", sources: [] }] };
const door: BizPage = {
  kind: "purchase_order", title: "PO1", subtitle: "Purchase order", facts: [], sources: "Sage",
  sections: [{ key: "lines", title: "Lines", columns: ["Item", "Description", "Ordered"], total: 1,
    rows: [[{ text: "TOW-300", href: "/records?q=TOW-300" }, { text: "Micro fiber towels" }, { number: "7" }]] }],
} as unknown as BizPage;

describe("the one record page with the business tables' lines", () => {
  it("shows the door's lines with their record links under the full page (Follow, Save, History), and the late lines apart", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(json(po))));
    render(<ObjectView type="PurchaseOrder" id="PurchaseOrder:p1" door={door} />);
    const link = await screen.findByRole("link", { name: "TOW-300" });
    expect(link).toHaveAttribute("href", "/records?q=TOW-300");
    expect(screen.getByText("Micro fiber towels")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Save/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "History" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("late-lines")).toBeInTheDocument());
  });

  it("without a door page the record's own lines show, as before", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(json(po))));
    render(<ObjectView type="PurchaseOrder" id="PurchaseOrder:p1" door={null} />);
    await waitFor(() => expect(screen.getByTestId("late-lines")).toBeInTheDocument());
    expect(screen.queryByText("Micro fiber towels")).toBeNull();
  });
});

describe("a raw source table's fields", () => {
  const fact = (text: string) => ({ text, sources: [{ system: "Sage", label: "Sage ap vendor", handle: "api://x" }], check: "read_from_record" });
  const raw = { object_id: "RelSageApObs:x", type: "RelSageApObs", type_label: "AP obs", label: "AP obs 1", summary: "", status: { state: "read", text: "Read", sources: [] }, freshness: {}, complete: true,
    facts: [fact("Amtbalhih: 45508"), fact("Idvend: V1")], __n: 2 };

  it("are folded under the source's own words, never listed as the record's details", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(json(raw))));
    render(<ObjectView type="RelSageApObs" id="RelSageApObs:x" />);
    const fold = await screen.findByTestId("source-fields");
    expect(fold).toHaveTextContent("Fields as Sage names them (2)");
    expect(screen.getAllByText(/Amtbalhih/)).toHaveLength(1);   // not also a headline figure
    expect(screen.queryByText("Details")).toBeNull();
  });

  it("a curated record keeps its Details list", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(json({ ...raw, type: "Shipment", type_label: null, object_id: "Shipment:x", label: "Shipment 1", facts: [fact("Carrier: Purolator"), fact("Note: left at dock")] }))));
    render(<ObjectView type="Shipment" id="Shipment:x" />);
    await screen.findByRole("tab", { name: "History" });
    expect(await screen.findByText("Details", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.queryByTestId("source-fields")).toBeNull();
  });
});
