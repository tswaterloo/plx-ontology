import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ObjectView } from "./object-view";

vi.mock("next/navigation", () => ({
  useParams: () => ({}),
  useSearchParams: () => null,
  useRouter: () => ({ push: vi.fn() }),
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

afterEach(() => vi.unstubAllGlobals());

const SO = "SalesOrder:bc.sales_orders:so1";
const mail = { system: "Email", handle: "graph-mail://box/m1@c1", label: "Email from Acme", open_url: "/source?h=m1" };
const ref = (type: string, id: string, label: string, extra = {}) => ({ object_id: `${type}:bc.x:${id}`, type, label, open_url: `/object/${type}/${type}%3Abc.x%3A${id}`, evidence: [], ...extra });

// a customer order as the server sends it: its links one group per link kind
const PAGE = {
  object_id: SO, type: "SalesOrder", label: "SO-101", summary: "Customer order SO-101 for Acme Foods.",
  status: { state: "read", text: "Read", sources: [] }, freshness: {}, complete: true,
  facts: [{ text: "External document number: PO-55", sources: [{ system: "BC", handle: "bc://production/c/salesOrders/so1@e1", label: "BC sales order", fields: { external_document_number: "PO-55" }, open_url: "/source?h=so1" }], check: "read_from_record" }],
  links: [
    { link_type: "SalesOrder.ordered_by", direction: "forward", name: "ordered_by", words: "Ordered by", type: "Customer", role: "customer", count: 1, records: [ref("Customer", "c1", "Acme Foods")] },
    { link_type: "SalesInvoice.bills", direction: "reverse", name: "invoices", words: "Invoices", type: "SalesInvoice", role: "fulfilment", count: 1, records: [ref("SalesInvoice", "i1", "PSI-9", { tier: "verified", evidence: [mail] })] },
    { link_type: "Shipment.ships", direction: "reverse", name: "shipments", words: "Shipments", type: "Shipment", role: "fulfilment", count: 12, possible: 3, records: [ref("Shipment", "s0", "SH-3", { tier: "verified" }), ref("Shipment", "s1", "SH-4", { tier: "lead", possible: true })] },
    { link_type: "SalesOrder.mentioned_in_meeting_note", direction: "reverse", name: "mentioned_in_meeting_note", words: "Mentioned in meeting note", type: "MeetingNote", role: "related", count: 1, records: [ref("MeetingNote", "n1", "Weekly ops")] },
  ],
};

describe("a customer order's page", () => {
  it("names its customer, lists its shipments and invoices with the mail behind them, and every other link kind", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(PAGE), { status: 200 })));
    render(<ObjectView type="SalesOrder" id={SO} />);
    expect(await screen.findByTestId("object-title")).toHaveTextContent("Acme Foods");
    const f = screen.getByTestId("fulfilment");
    expect(f).toHaveTextContent("Shipments and invoices");
    expect(within(f).getByText(/PSI-9/).closest("a")?.getAttribute("href")).toContain("SalesInvoice");
    expect(within(f).getByTestId("link-evidence")).toHaveTextContent("Email");
    expect(f).toHaveTextContent("Suggested link"); // the shipment read from a reference
    expect(f).toHaveTextContent("and 11 more"); // 12 counted, 1 shown; the possible one never counted
    expect(f).toHaveTextContent("and 2 more possibly related");
    const linked = screen.getByTestId("related");
    expect(linked).toHaveTextContent("Weekly ops");
    expect(linked).not.toHaveTextContent("Acme Foods"); // the header names the customer
    // a detail opens the record it is read from
    expect(screen.getByTitle("From BC sales order")).toHaveTextContent("PO-55");
  });
});
