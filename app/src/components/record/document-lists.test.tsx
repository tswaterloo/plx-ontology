import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { DocList, DocRow, RecordDocuments } from "@/lib/types";
import { DocumentLists } from "./document-lists";

const ref = (object_id: string, label: string) => ({ object_id, type: object_id.split(":")[0], label, open_url: `/object/${object_id.split(":")[0]}/${object_id}` });
const issue = (i: number, status = "Reject"): DocRow => ({
  title: `Lot LOT-${i} rejected at receiving`,
  dated: "2026-10-05",
  status,
  tone: "bad",
  source: ref(`PlannerTask:fx:${i}`, "Planner task"),
  records: [ref(`Lot:fx:${i}`, `LOT-${i}`)],
  figures: [],
});
const list = (view: DocList["view"], rows: DocRow[], extra: Partial<DocList> = {}): DocList => ({ view, title: view, rows, withheld: 0, complete: true, ...extra });

describe("sheets and mail lists on a record page", () => {
  it("shows five rows, opens the rest in place and filters by status", () => {
    const rows = [...Array.from({ length: 7 }, (_, i) => issue(i)), issue(9, "Deviation")];
    render(<DocumentLists docs={{ lists: [list("supplier_issues", rows)] }} />);
    const box = screen.getByTestId("doc-list-supplier_issues");
    expect(within(box).getAllByTestId("doc-row")).toHaveLength(5);
    fireEvent.click(within(box).getByText("Show 3 more"));
    expect(within(box).getAllByTestId("doc-row")).toHaveLength(8);
    fireEvent.click(within(box).getByRole("button", { name: /Deviation/ }));
    expect(within(box).getAllByTestId("doc-row")).toHaveLength(1);
    expect(within(box).getByText("LOT-9").getAttribute("href")).toBe("/object/Lot/Lot:fx:9");
    expect(box.textContent).not.toMatch(/and \d+ more/);
  });

  it("says plainly what no list holds, what was left out and what access a list needs", () => {
    const docs: RecordDocuments = {
      lists: [
        list("forecasts", []),
        list("forecast_accuracy", [], { withheld: 2 }),
        list("price_sheets", [], { needs: "finance" }),
        list("answer_bank", [], { error: "The list could not be read.", complete: false }),
      ],
    };
    render(<DocumentLists docs={docs} />);
    expect(screen.getByTestId("doc-lists-empty").textContent).toBe("No forecasts or scored forecast months name this record that you can open.");
    expect(screen.getByTestId("doc-list-price_sheets-unread").textContent).toContain("needs finance access");
    expect(screen.getByTestId("doc-list-answer_bank-unread").textContent).toContain("could not be read");
  });

  it("shows figures with their units and counts rows it left out", () => {
    const row: DocRow = { title: "Resin base", status: "Billed off the sheet", tone: "bad", records: [], figures: [{ label: "Sheet price", value: "10.00", unit: "USD" }, { label: "Bias", value: "+57.9", unit: "%" }] };
    render(<DocumentLists docs={{ lists: [list("price_sheets", [row], { withheld: 1, complete: false })] }} />);
    const box = screen.getByTestId("doc-list-price_sheets");
    expect(box.textContent).toContain("10.00 USD");
    expect(box.textContent).toContain("+57.9%");
    expect(box.textContent).toContain("1 left out: it cites a record you can't open; the read was cut, so some may be missing.");
  });

  it("renders nothing without the section", () => {
    const { container } = render(<DocumentLists docs={null} />);
    expect(container.textContent).toBe("");
  });
});
