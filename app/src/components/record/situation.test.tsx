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

  it("says in words what a customer order has not on record, and stays off party pages", () => {
    render(<Situation obj={{ ...ao, type: "SalesOrder", related: [], people: [], timeline: [] } as ObjectPage} />);
    const band = screen.getByTestId("situation");
    for (const t of ["Customer not linked", "No production or purchase order linked", "No one named on record", "No change recorded"]) expect(band).toHaveTextContent(t);
    for (const type of ["Supplier", "Customer", "Person"]) {
      const { container } = render(<Situation obj={{ ...ao, type } as ObjectPage} />);
      expect(container).toBeEmptyDOMElement();
    }
  });

  it("reads a purchase order against its supplier and the orders it is needed for, the supplier from the header when no link names it", () => {
    const po = { ...ao, type: "PurchaseOrder", related: [ref("SalesOrder", "SalesOrder:demo:77120", "77120"), ref("AssemblyOrder", "AssemblyOrder:demo:AO-7712", "AO-7712")] } as unknown as ObjectPage;
    render(<Situation obj={po} party={ref("Supplier", "Supplier:demo:maple-leaf", "Maple Leaf Soapworks")} />);
    const band = screen.getByTestId("situation");
    expect(band).toHaveTextContent(/^SupplierMaple Leaf Soapworks/);
    expect(band).toHaveTextContent("Needed forSO 77120AO-7712");
    expect(screen.getByRole("link", { name: "Maple Leaf Soapworks" })).toHaveAttribute("href", "/object/Supplier/Supplier%3Ademo%3Amaple-leaf");
  });

  it("gives a lot, a shipment and each invoice its own pair of cells", () => {
    const words = (type: string) => {
      const { unmount } = render(<Situation obj={{ ...ao, type, related: [], people: [], timeline: [] } as ObjectPage} />);
      const text = screen.getByTestId("situation").textContent;
      unmount();
      return text;
    };
    expect(words("Lot")).toMatch(/^Received onNo purchase order linkedUsed inNot linked to an order or shipment/);
    expect(words("Shipment")).toMatch(/^OrderNo customer order linkedCustomerCustomer not linked/);
    expect(words("SalesInvoice")).toMatch(/^OrderNo order or shipment linkedCustomer/);
    expect(words("PurchaseInvoice")).toMatch(/^Purchase orderNo purchase order linkedSupplierSupplier not linked/);
  });
});
