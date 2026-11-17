import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { SourceRecords } from "@/lib/types";
import { citedLine, SourceRecordsPanel } from "./source-records";

const ref = (object_id: string, type: string, label: string) => ({ object_id, type, label, open_url: `/object/${type}/${encodeURIComponent(object_id)}` });
const got: SourceRecords = {
  handle: "bc://demo/purchaseOrders/90412",
  served: true,
  truncated: false,
  records: [
    { record: ref("PurchaseOrder:demo:90412", "PurchaseOrder", "90412"), how: "from", link: null },
    { record: ref("Supplier:demo:maple-leaf", "Supplier", "Maple Leaf Soapworks"), how: "linked", link: "supplier" },
    { record: ref("SalesOrder:demo:77120", "SalesOrder", "77120"), how: "linked", link: "supplies" },
  ],
} as SourceRecords;

describe("a source's records: what it was read into and what links to those", () => {
  it("lists the record it was read into apart from the linked ones, each opening its page", () => {
    render(<SourceRecordsPanel got={got} />);
    const from = screen.getByTestId("source-records-from");
    expect(from).toHaveTextContent("PO 90412");
    expect(from).not.toHaveTextContent("Maple Leaf");
    const linked = screen.getByTestId("source-records-linked");
    expect(linked).toHaveTextContent("Maple Leaf Soapworks");
    expect(linked).toHaveTextContent("SO 77120");
    expect(screen.getByRole("link", { name: /SO 77120/ }).getAttribute("href")).toBe("/object/SalesOrder/SalesOrder%3Ademo%3A77120");
    expect(citedLine(got)).toBe("Read into PO 90412; linked to Maple Leaf Soapworks and SO 77120");
  });

  it("says when no record the reader can open cites it, when the list was cut, and when it couldn't be read", () => {
    const { unmount } = render(<SourceRecordsPanel got={{ ...got, records: [] }} />);
    expect(screen.getByTestId("source-records")).toHaveTextContent("No record you can open cites it.");
    expect(citedLine({ ...got, records: [] })).toBeNull();
    unmount();
    const cut = render(<SourceRecordsPanel got={{ ...got, truncated: true }} />);
    expect(screen.getByTestId("source-records")).toHaveTextContent("the first 40 are listed");
    cut.unmount();
    const failed = render(<SourceRecordsPanel got="failed" />);
    expect(screen.getByTestId("source-records")).toHaveTextContent("couldn't be read");
    failed.unmount();
    // a server with no records to look in shows nothing, rather than "none cite it"
    const { container } = render(<SourceRecordsPanel got={{ ...got, served: false, records: [] }} />);
    expect(container).toBeEmptyDOMElement();
  });
  it("names each record in its line up to three, counts a longer list, and never cuts a list with 'and N more'", () => {
    const linked = (n: number) =>
      Array.from({ length: n }, (_, i) => ({ record: ref(`SalesOrder:demo:${7000 + i}`, "SalesOrder", String(7000 + i)), how: "linked" as const, link: "supplies" }));
    const from = got.records![0];
    expect(citedLine({ ...got, records: [from] })).toBe("Read into PO 90412");
    expect(citedLine({ ...got, records: [from, ...linked(10)] })).toBe("Read into PO 90412; linked to 10 records");
    expect(citedLine({ ...got, truncated: true, records: [from, ...linked(39)] })).toBe("Read into PO 90412; linked to at least 39 records");
    expect(citedLine({ ...got, records: linked(2) } as SourceRecords)).toBe("Linked to SO 7000 and SO 7001");
    for (const n of [1, 10, 60]) expect(citedLine({ ...got, records: [from, ...linked(n)] } as SourceRecords)).not.toMatch(/\bmore\b/);
  });
});
