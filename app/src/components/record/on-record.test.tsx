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
  it("says in words what is not on record for a thin record, and offers Ask as the next step", () => {
    render(<OnRecord obj={base} />);
    const box = screen.getByTestId("on-record");
    for (const t of ["No person named on record", "Nothing linked yet", "No change recorded"]) expect(box).toHaveTextContent(t);
    expect(box).not.toHaveTextContent("Item");
    expect(screen.getByRole("link", { name: "Ask what matters about it" }).getAttribute("href")).toContain("/ask?q=What+should+I+know+about+BAR-LAV-100");
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
    expect(answer(rich, "Who is on it").a).toBe("Ana (QC lead, PLX); Bo (Buyer); Cy (Planner); 1 more");
    expect(answer(rich, "Linked to").a).toBe("Lot L-24-0917, QC note, #qc and 1 more");
    expect(answer(rich, "Linked to").href).toBe("/object/Lot/Lot%3Ademo%3AL-24-0917");
    expect(answer(rich, "Last change").a).toMatch(/^Put on hold, /);
    expect(answer(rich, "Next step").a).toBe("Release the hold once the retest passes");
    expect(answer(rich, "Next step").href).toBeUndefined();
  });

  it("stays off the record types that have their own page design", () => {
    for (const type of ["PurchaseOrder", "SalesOrder", "AssemblyOrder", "Customer", "Supplier"]) {
      const { container, unmount } = render(<OnRecord obj={{ ...base, type }} />);
      expect(container).toBeEmptyDOMElement();
      unmount();
    }
  });
});
