import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AskNext, questionsFor } from "./ask-next";

describe("a record's Ask next panel", () => {
  it("asks what each kind of record is asked next, scoped to the record", () => {
    render(<AskNext type="PurchaseOrder" objectId="PurchaseOrder:demo:90388" label="PO 90388" party="Northwind Packaging" />);
    const links = screen.getAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual(["Which customer orders wait on PO 90388?", "What did Northwind Packaging last say about PO 90388?", "Landed cost of PO 90388"]);
    expect(links[0]).toHaveAttribute("href", "/ask?q=Which%20customer%20orders%20wait%20on%20PO%2090388%3F&object=PurchaseOrder%3Ademo%3A90388");
  });

  it("differs by record type and falls back to the latest for an unknown type", () => {
    expect(questionsFor("Lot", "Lot 2401", null)).toEqual(["Where was Lot 2401 used?", "Is Lot 2401 on hold or near expiry?"]);
    expect(questionsFor("SalesOrder", "SO 1", null)[1]).toBe("What did we last tell the customer about SO 1?");
    expect(questionsFor("Mystery", "X 1", null)).toEqual(["What's the latest on X 1?"]);
  });
});
