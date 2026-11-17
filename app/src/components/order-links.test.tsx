import { fireEvent, render, screen, within } from "@testing-library/react";
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
    expect(f).toHaveTextContent("11 other records not listed here"); // 12 counted, 1 shown; the possible one never counted
    expect(f).toHaveTextContent("2 possibly related records not listed here");
    expect(f).not.toHaveTextContent(/and \d+ more/);
    const linked = screen.getByTestId("related");
    expect(linked).toHaveTextContent("Weekly ops");
    expect(linked).not.toHaveTextContent("Acme Foods"); // the header names the customer
    // a detail opens the record it is read from
    expect(screen.getByTitle("From BC sales order")).toHaveTextContent("PO-55");
  });
  it("opens a long link group in place and says what the page never loaded apart, with no 'and N more' line", async () => {
    for (const loaded of [1, 10, 60]) {
      const ships = Array.from({ length: loaded }, (_, n) => ref("Shipment", `s${n}`, `SH-${n}`, { tier: "verified" }));
      const page = { ...PAGE, links: PAGE.links.map((g) => (g.name === "shipments" ? { ...g, count: loaded + 5, possible: 0, records: ships } : g)) };
      vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(page), { status: 200 })));
      const view = render(<ObjectView type="SalesOrder" id={SO} />);
      await screen.findByTestId("object-title");
      const group = screen.getByTestId("link-group-Shipment.ships");
      const rows = () => within(group).queryAllByText(/^SH-\d+$/).length;
      const shown = Math.min(loaded, 8);
      expect(rows()).toBe(shown);
      expect(group).toHaveTextContent("5 other records not listed here");
      expect(group).not.toHaveTextContent(/and \d+ more/);
      if (loaded > 8) {
        fireEvent.click(within(group).getByRole("button", { name: `Show ${loaded - 8} more` }));
        expect(rows()).toBe(loaded); // every loaded row once, none lost or doubled
        expect(group).toHaveTextContent("5 other records not listed here");
        fireEvent.click(within(group).getByRole("button", { name: "Show fewer" }));
        expect(rows()).toBe(8);
      } else {
        expect(within(group).queryByTestId("show-more")).toBeNull();
      }
      view.unmount();
      vi.unstubAllGlobals();
    }
  });
});

describe("a record's linked messages and documents", () => {
  it("lists the ones the reader may open by title and only counts the rest", async () => {
    const page = { ...PAGE, links: [], linked: [{ kind: "email", label: "Order confirmation from Acme", source: mail }], linked_withheld: 2 };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(page), { status: 200 })));
    render(<ObjectView type="SalesOrder" id={SO} />);
    const linked = await screen.findByTestId("linked");
    expect(linked).toHaveTextContent("Order confirmation from Acme");
    expect(within(linked).getByTestId("linked-withheld")).toHaveTextContent("Not listed: 2 linked documents you can't open.");
  });

  it("still says what is there when the reader may open none of them", async () => {
    const page = { ...PAGE, links: [], linked: [], linked_withheld: 1 };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(page), { status: 200 })));
    render(<ObjectView type="SalesOrder" id={SO} />);
    expect(await screen.findByTestId("linked-withheld")).toHaveTextContent("Not listed: 1 linked document you can't open.");
  });

  it("words a source with no reader set up as not available yet, and a cut count as at least", async () => {
    const page = { ...PAGE, links: [], linked: [], linked_withheld: 0, linked_unavailable: 3, linked_counted_all: false };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(page), { status: 200 })));
    render(<ObjectView type="SalesOrder" id={SO} />);
    expect(await screen.findByTestId("linked-unavailable")).toHaveTextContent("Not listed: at least 3 linked documents not available yet.");
    expect(screen.queryByTestId("linked-withheld")).toBeNull();
    expect(screen.getByTestId("linked")).not.toHaveTextContent("can't open");
  });
});
