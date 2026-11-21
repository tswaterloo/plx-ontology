import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ObjectPage } from "@/lib/types";
import { OnRecord, answersOf } from "./on-record";

const base = {
  object_id: "Item:demo:BAR-LAV-100",
  type: "Item",
  label: "BAR-LAV-100 Lavender soap bar 100 g",
  summary: "",
  complete: true,
  status: { state: "open", text: "" },
  freshness: {},
} as unknown as ObjectPage;
const answer = (obj: ObjectPage, q: string) => answersOf(obj).find((a) => a.q === q)!;

describe("on record: who, linked, last change and next step on a record without a page of its own", () => {
  it("leaves out what is not on record for a thin record, and offers Ask as the next step", () => {
    render(<OnRecord obj={base} />);
    const box = screen.getByTestId("on-record");
    for (const t of ["Who is on it", "Linked to", "Last change", "No person", "Nothing linked", "No change"]) expect(box).not.toHaveTextContent(t);
    expect(box).not.toHaveTextContent("Item");
    expect(screen.getByRole("link", { name: "Ask about it" }).getAttribute("href")).toContain("/ask?q=What+should+I+know+about+BAR-LAV-100");
  });

  it("names the people, the links, the newest change by date and the proposed step", () => {
    const rich = {
      ...base,
      status: { state: "hold", text: "On QC hold" },
      people: [
        { name: "Ana", role: "QC lead", organisation: "PLX" },
        { name: "Bo", role: "Buyer" },
        { name: "Cy", role: "Planner" },
        { name: "Di", role: "Sales" },
      ],
      related: [{ label: "Lot L-24-0917", object_id: "Lot:demo:L-24-0917", type: "Lot", open_url: "" }],
      linked: [{ kind: "email", label: "QC note", source: {} }, { kind: "teams", label: "#qc", source: {} }, { kind: "email", label: "Old", source: {} }],
      timeline: [
        { title: "Released", at: "2026-10-01T10:00:00Z", system: "BC" },
        { title: "Put on hold", at: "2026-11-02T10:00:00Z", system: "MC" },
        { title: "Undated note", system: "Email" },
      ],
      proposals: [{ id: "p", kind: "record_update", title: "Release the hold once the retest passes", sent: false, subject: {} }],
    } as unknown as ObjectPage;
    // four of each: counted, never "A, B, C and 1 more" (each is listed on the page)
    expect(answer(rich, "Who is on it").a).toBe("4 people named on record");
    expect(answer(rich, "Linked to").a).toBe("4 linked records");
    const three = { ...rich, people: rich.people!.slice(0, 3), linked: rich.linked!.slice(0, 2) } as ObjectPage;
    expect(answer(three, "Who is on it").a).toBe("Ana (QC lead, PLX); Bo (Buyer); Cy (Planner)");
    expect(answer(three, "Linked to").a).toBe("Lot L-24-0917, QC note and #qc");
    expect(answer(rich, "Linked to").href).toBe("/object/Lot/Lot%3Ademo%3AL-24-0917");
    expect(answer(rich, "Last change").a).toMatch(/^Put on hold, /);
    expect(answer(rich, "Next step").a).toBe("Release the hold once the retest passes");
    expect(answer(rich, "Next step").href).toBeUndefined();
  });

  it("names each linked record once and never the record itself", () => {
    const inv = (n: number) => ({ label: n < 3 ? "IN1000180804" : `IN10001808${n}`, object_id: `RelSageArIbh:x:${n}`, type: "RelSageArIbh", open_url: "" });
    const rows = { ...base, label: "IN1000180804", related: [0, 1, 2, 5, 6].map(inv) } as unknown as ObjectPage;
    expect(answer(rows, "Linked to").a).toBe("IN100018085 and IN100018086");
    const self = { ...base, label: "IN1000180804", related: [0, 1, 2].map(inv) } as unknown as ObjectPage;
    expect(answer(self, "Linked to").a).toBe("3 linked records");
  });

  it("stays off the record types that have their own page design", () => {
    for (const type of ["PurchaseOrder", "SalesOrder", "AssemblyOrder", "Customer", "Supplier"]) {
      const { container, unmount } = render(<OnRecord obj={{ ...base, type }} />);
      expect(container).toBeEmptyDOMElement();
      unmount();
    }
  });
});

describe("a record's own facts, said once", () => {
  const shipment = {
    ...base,
    object_id: "Shipment:sage.oe_shipment_header:{\"shiuniq\":\"513\"}",
    type: "Shipment",
    label: "Shipment SH1000000001",
    timeline: [
      { title: "Shipped", at: "2016-10-06T10:00:00Z", system: "Sage", own: true },
      // a linked customer's later orders are beside the shipment, not its change
      { title: "Ordered: customer GRANITE CLUB", at: "2019-09-05T10:00:00Z", system: "Sage", own: false },
    ],
    links: [
      { role: "linked", type: "ArOpenDocument", type_label: "AR open document", words: "AR open document", records: [
        { object_id: "ArOpenDocument:a", type: "ArOpenDocument", label: "1000099210", open_url: "" },
        { object_id: "ArOpenDocument:b", type: "ArOpenDocument", label: "1000099210", open_url: "" },
      ] },
      { role: "linked", type: "Customer", words: "Customer", records: [{ object_id: "Customer:g", type: "Customer", label: "GRANITE CLUB", open_url: "" }] },
    ],
    related: [
      { object_id: "ArOpenDocument:a", type: "ArOpenDocument", label: "1000099210", open_url: "" },
      { object_id: "ArOpenDocument:b", type: "ArOpenDocument", label: "1000099210", open_url: "" },
      { object_id: "Customer:g", type: "Customer", label: "GRANITE CLUB", open_url: "" },
      { object_id: "Customer:g", type: "Customer", label: "GRANITE CLUB", open_url: "" },
    ],
  } as unknown as ObjectPage;

  it("takes the last change from the record's own events, never a linked record's", () => {
    expect(answer(shipment, "Last change").a).toMatch(/^Shipped, 6 Oct 2016$/);
    const onlyOthers = { ...shipment, timeline: [shipment.timeline![1]] } as ObjectPage;
    expect(answer(onlyOthers, "Last change").empty).toBe(true);
  });

  it("counts the linked records the Linked list shows, a document's repeated rows once", () => {
    expect(answer(shipment, "Linked to").a).toBe("1000099210 and GRANITE CLUB");
    // without link groups: each record once by its id (one was listed twice: 4, not 5)
    const r = (n: number) => ({ object_id: `Customer:${n}`, type: "Customer", label: `C${n}`, open_url: "" });
    const many = { ...shipment, links: undefined, related: [r(1), r(2), r(3), r(4), r(4)] } as unknown as ObjectPage;
    expect(answer(many, "Linked to").a).toBe("4 linked records");
  });

  it("leaves the last change to the situation strip on a type that has one", () => {
    render(<OnRecord obj={shipment} />);
    expect(screen.getByTestId("on-record")).not.toHaveTextContent("Last change");
  });
});
