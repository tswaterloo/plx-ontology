import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ObjectPage } from "@/lib/types";
import { Situation } from "./situation";

const ref = (type: string, object_id: string, label: string) => ({ type, object_id, label, open_url: "" });
const ao = {
  object_id: "AssemblyOrder:demo:AO-7712",
  type: "AssemblyOrder",
  label: "AO-7712",
  status: { state: "open", text: "" },
  related: [ref("SalesOrder", "SalesOrder:demo:77120", "77120"), ref("PurchaseOrder", "PurchaseOrder:demo:90412", "90412"), ref("Item", "Item:demo:X", "X")],
  people: [{ name: "Priya Nair", role: "Planner" }],
  timeline: [
    { title: "Released", at: "2026-10-03T13:00:00Z", system: "BC" },
    { title: "Bars delayed to 4 Nov", at: "2026-10-21T13:12:00Z", system: "Email" },
  ],
} as unknown as ObjectPage;

describe("an order's situation band", () => {
  it("names what a production order is for, where its materials come from, who is on it and the newest change", () => {
    render(<Situation obj={ao} />);
    const band = screen.getByTestId("situation");
    expect(screen.getByRole("link", { name: "SO 77120" }).getAttribute("href")).toBe("/object/SalesOrder/SalesOrder%3Ademo%3A77120");
    expect(screen.getByRole("link", { name: "PO 90412" })).toBeTruthy();
    expect(band).toHaveTextContent("Priya Nair (Planner)");
    expect(band).toHaveTextContent(/Bars delayed to 4 Nov, /);
    expect(band).not.toHaveTextContent("X");
  });

  it("draws no band for a record with nothing on record, and stays off party pages", () => {
    const { container: bare } = render(<Situation obj={{ ...ao, type: "SalesOrder", related: [], people: [], timeline: [] } as ObjectPage} />);
    expect(bare).toBeEmptyDOMElement();
    for (const type of ["Supplier", "Customer", "Person"]) {
      const { container } = render(<Situation obj={{ ...ao, type } as ObjectPage} />);
      expect(container).toBeEmptyDOMElement();
    }
  });

  it("shows only the cells whose fact is on record, and never the party the title already names", () => {
    const po = { ...ao, type: "PurchaseOrder", related: [], people: [] } as unknown as ObjectPage;
    // the title names the supplier, linked (party) or by name only (named): its cell would say it again
    for (const [party, named, related] of [
      [ref("Supplier", "Supplier:demo:maple-leaf", "Maple Leaf Soapworks"), undefined, []],
      [null, "maple leaf soapworks", [ref("Supplier", "Supplier:demo:m", "Maple Leaf Soapworks")]],
    ] as const) {
      const { unmount } = render(<Situation obj={{ ...po, related } as unknown as ObjectPage} party={party} named={named} />);
      const band = screen.getByTestId("situation");
      expect(band).toHaveTextContent(/^Last change/);
      for (const t of ["Supplier", "Needed for", "Who", "not linked", "No one", "No change"]) expect(band).not.toHaveTextContent(t);
      unmount();
    }
    // a supplier other than the title's is said
    render(<Situation obj={{ ...po, related: [ref("Supplier", "Supplier:demo:other", "Other Co")] } as unknown as ObjectPage} named="Maple Leaf Soapworks" />);
    expect(screen.getByTestId("situation")).toHaveTextContent(/^SupplierOther Co/);
  });

  it("reads a purchase order against the orders it is needed for", () => {
    const po = { ...ao, type: "PurchaseOrder", related: [ref("SalesOrder", "SalesOrder:demo:77120", "77120"), ref("AssemblyOrder", "AssemblyOrder:demo:AO-7712", "AO-7712")] } as unknown as ObjectPage;
    render(<Situation obj={po} party={ref("Supplier", "Supplier:demo:maple-leaf", "Maple Leaf Soapworks")} />);
    const band = screen.getByTestId("situation");
    expect(band).toHaveTextContent(/^Needed forSO 77120AO-7712/);
  });

  it("gives a lot, a shipment and each invoice its own pair of cells", () => {
    const words = (type: string, related: ReturnType<typeof ref>[]) => {
      const { unmount } = render(<Situation obj={{ ...ao, type, related, people: [], timeline: [] } as unknown as ObjectPage} />);
      const text = screen.getByTestId("situation").textContent;
      unmount();
      return text;
    };
    expect(words("Lot", [ref("PurchaseOrder", "PurchaseOrder:demo:1", "1"), ref("Shipment", "Shipment:demo:2", "2")])).toMatch(/^Received onPO 1Used in/);
    expect(words("Shipment", [ref("SalesOrder", "SalesOrder:demo:3", "3"), ref("Customer", "Customer:demo:c", "Acme")])).toMatch(/^OrderSO 3CustomerAcme/);
    expect(words("SalesInvoice", [ref("Customer", "Customer:demo:c", "Acme")])).toBe("CustomerAcme");
    expect(words("PurchaseInvoice", [ref("Supplier", "Supplier:demo:s", "Maple")])).toBe("SupplierMaple");
  });
});
