import { describe, expect, it } from "vitest";
import { pageContextQuery, scopedToPage } from "./page-context";

describe("pageContextQuery", () => {
  it("names what each kind of page has open, and nothing on the Ask page itself", () => {
    expect(pageContextQuery("/object/PurchaseOrder/PurchaseOrder%3A90412", "")).toBe("&object=PurchaseOrder%3A90412");
    expect(pageContextQuery("/source", "?handle=graph-mail%3A%2F%2Fme%2Fm1%401")).toBe("&source=graph-mail%3A%2F%2Fme%2Fm1%401");
    expect(pageContextQuery("/autopilot", "?item=ap_1")).toBe("&item=ap_1");
    expect(pageContextQuery("/meetings", "?meeting=m-ops")).toBe("&meeting=m-ops");
    expect(pageContextQuery("/late", "")).toBe("&page=%2Flate");
    expect(pageContextQuery("/ask", "?q=x")).toBe("");
  });
});

describe("scopedToPage", () => {
  it("takes the page along for a line about it or an instruction, never for a plain question", () => {
    for (const line of ["reply to this", "chase the supplier", "Can you draft a chase?", "what is late on it?", "update the ship date to Nov 20"]) expect(scopedToPage(line)).toBe(true);
    for (const line of ["what ships next week", "Can you tell me when PO 1234 ships?", "update me on PO 1234", "late POs", "tell me about Maple"]) expect(scopedToPage(line)).toBe(false);
  });
});
