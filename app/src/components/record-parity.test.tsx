import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { isRawGuid, recordLabel } from "@/lib/format";
import { recordHead } from "@/lib/record";
import type { BizPage, ObjectPage } from "@/lib/types";
import { ObjectView, withDoor } from "./object-view";

vi.mock("next/navigation", () => ({
  useParams: () => ({}),
  useSearchParams: () => null,
  usePathname: () => "/",
  useRouter: () => ({ push: vi.fn() }),
}));

afterEach(() => vi.unstubAllGlobals());

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const fact = (text: string, field = "x") => ({
  text,
  sources: [{ system: "BC" as const, handle: `bc://prod/c/t/1@${field}`, label: "BC", fields: { [field]: "v" } }],
  check: "read_from_record",
});

const base = (type: string, label: string, extra: Partial<ObjectPage> = {}): ObjectPage =>
  ({
    object_id: `${type}:demo:1`,
    type,
    label,
    summary: "",
    status: { state: "read", text: "Read", sources: [] },
    freshness: {},
    complete: true,
    facts: [],
    related: [],
    linked: [],
    timeline: [],
    ...extra,
  }) as unknown as ObjectPage;

describe("record page parity across all business record kinds", () => {
  it("renders elevated hero, kind badge, toolbar, and clickable links for SalesOrder", async () => {
    const so = base("SalesOrder", "SO100234", {
      facts: [
        fact("Status: Open", "status"),
        fact("Customer PO: CUST-PO-9", "external_document_number"),
        fact("Order date: 2026-11-01", "order_date"),
        fact("Currency: CAD", "currency"),
      ],
      related: [{ object_id: "Customer:demo:c1", type: "Customer", label: "Acme Beauty", open_url: "/object/Customer/c1" }],
    });
    const door = {
      kind: "sales_order",
      title: "SO 100234",
      subtitle: "Customer order",
      sources: "Sources: Sage",
      facts: [
        { label: "Customer", value: { text: "Acme Beauty", href: "/account?kind=customer&h=c1" } },
        { label: "Status", value: { text: "open" } },
        { label: "Value", value: { money: { amount: "12450.00", currency: "CAD" } } },
      ],
      sections: [
        {
          key: "lines",
          title: "Lines",
          columns: ["Item", "Name", "Ordered"],
          total: 1,
          rows: [[{ text: "SKU-1", href: "/records?q=SKU-1" }, { text: "Hand Cream" }, { number: "500" }]],
        },
      ],
    } as unknown as BizPage;
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(json(so))));
    render(<ObjectView type="SalesOrder" id="SalesOrder:demo:1" door={door} />);
    expect(await screen.findByTestId("record-kind-badge")).toHaveTextContent("Customer order");
    expect(screen.getByTestId("object-title")).toHaveTextContent("SO 100234");
    expect(screen.getByTestId("detail-stats")).toHaveTextContent("$12,450.00");
    expect(screen.getByRole("link", { name: "SKU-1" })).toHaveAttribute("href", "/records?q=SKU-1");
    expect(screen.getByRole("button", { name: /Save/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "History" })).toBeInTheDocument();
  });

  it("never shows $0.00 when SalesOrder value is unknown", () => {
    const so = base("SalesOrder", "SO100999", { facts: [fact("Status: Open", "status")] });
    const door = {
      kind: "sales_order",
      title: "SO 100999",
      subtitle: "Customer order",
      sources: "Sources: Sage",
      facts: [{ label: "Value", value: { text: "Not recorded" } }],
      sections: [],
    } as unknown as BizPage;
    const head = withDoor(recordHead(so), door);
    expect(head.stats.find((s) => s.label === "Value")?.value).toBe("Not recorded");
    expect(JSON.stringify(head)).not.toContain("$0.00");
  });

  it("unmade Batch with QC On-Hold never reads Hold badge", () => {
    const unmade = base("Batch", "B-2026-01", {
      facts: [
        fact("Status: Scheduled", "status"),
        fact("Quality status: On-Hold", "quality_status"),
        fact("Planned quantity: 500", "planned_quantity"),
        fact("Scheduled date: 2026-12-01", "scheduled_date"),
      ],
      related: [{ object_id: "Product:demo:p1", type: "Product", label: "Bulk Lotion", open_url: "/object/Product/p1" }],
    });
    const head = recordHead(unmade);
    expect(head.badges.map((b) => b.label)).toEqual(["Scheduled"]);
    expect(head.badges.some((b) => /hold/i.test(b.label))).toBe(false);
    expect(head.stats.find((s) => s.label === "Produced")?.value).toBe("Not made yet");
    expect(head.counterpart?.label).toBe("Bulk Lotion");
  });

  it("realized Batch with QC On-Hold still shows On hold badge", () => {
    const made = base("Batch", "B-2026-02", {
      facts: [
        fact("Status: Realized", "status"),
        fact("Quality status: On-Hold", "quality_status"),
        fact("Realized date: 2026-11-10", "realized_date"),
        fact("Produced quantity: 480", "produced_quantity"),
      ],
    });
    const head = recordHead(made);
    expect(head.badges.some((b) => b.label === "On hold")).toBe(true);
  });

  it("gives Product, Lot, Shipment, Receipt, and SupplierInvoice rich structured heads", () => {
    const product = recordHead(
      base("Product", "Lavender Soap Bar", {
        facts: [fact("Item code: BAR-100", "item_code"), fact("Category: Bars", "category"), fact("Active: Yes", "active"), fact("Unit price: 4.50 CAD", "unit_price")],
      }),
    );
    expect(product.subline).toBe("BAR-100 · Bars");
    expect(product.badges).toEqual([{ status: "ok", label: "Active" }]);
    expect(product.stats.find((s) => s.label === "Unit price")).toMatchObject({ value: "$4.50", caption: "CAD" });

    const lot = recordHead(
      base("Lot", "L-991", {
        facts: [fact("Quality status: Released", "quality_status"), fact("Expiry date: 2027-06-01", "expiry_date"), fact("Quantity on hand: 250", "quantity_on_hand")],
        related: [{ object_id: "Product:demo:p1", type: "Product", label: "Shea Butter", open_url: "/object/Product/p1" }],
      }),
    );
    expect(lot.counterpart?.label).toBe("Shea Butter");
    expect(lot.badges).toEqual([{ status: "ok", label: "Released" }]);
    expect(lot.stats.map((s) => s.label)).toEqual(["Quantity", "Expires"]);

    const receipt = recordHead(
      base("Receipt", "RCP10055", {
        facts: [fact("Status: Posted", "status"), fact("Receipt date: 2026-11-12", "receipt_date"), fact("Quantity received: 120", "quantity_received")],
        related: [{ object_id: "Supplier:demo:s1", type: "Supplier", label: "Croda", open_url: "/object/Supplier/s1" }],
      }),
    );
    expect(receipt.counterpart?.label).toBe("Croda");
    expect(receipt.stats.map((s) => s.label)).toEqual(["Received", "Quantity"]);

    const inv = recordHead(
      base("SupplierInvoice", "RelSageApOpenDocument:x", {
        facts: [fact("Document number: INV-884", "document_number"), fact("Original amount: 3200.00 CAD", "original_amount"), fact("Amount due: 0.00 CAD", "amount_due"), fact("Paid: Yes", "paid")],
        related: [{ object_id: "Supplier:demo:s2", type: "Supplier", label: "Polyrheo", open_url: "/object/Supplier/s2" }],
      }),
    );
    expect(inv.title).toBe("Invoice INV-884");
    expect(inv.counterpart?.label).toBe("Polyrheo");
    expect(inv.badges).toEqual([{ status: "ok", label: "Paid" }]);
    expect(inv.stats.find((s) => s.label === "Amount")).toMatchObject({ value: "$3,200.00", caption: "CAD" });
  });

  it("never shows raw GUIDs as record titles or link labels", () => {
    const guid = "3f2504e0-4f89-11d3-9a0c-0305e82c3301";
    expect(isRawGuid(guid)).toBe(true);
    expect(recordLabel("SalesOrder", guid)).toBe("Customer order");
    expect(recordLabel("Shipment", guid)).toBe("Shipment");
    const head = recordHead(base("SalesOrder", guid, { facts: [fact("Order number: SO-441", "order_number")] }));
    expect(head.title).toBe("SO-441");
    expect(head.title).not.toContain(guid);
  });
});
