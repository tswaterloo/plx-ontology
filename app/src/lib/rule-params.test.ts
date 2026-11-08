import { describe, expect, it } from "vitest";
import { ruleParams } from "./rule-params";

describe("ruleParams", () => {
  it("shows a record rule's conditions back", () => {
    expect(ruleParams({ schema: "plx-alert-rule/v1", subject: "SalesOrder", when: [
      { property: "required_date", op: "later" }, { property: "total_amount", op: "gt", value: "50000" }] }))
      .toEqual([{ name: "Records", value: "SalesOrder" }, { name: "required date", value: "moves later" },
        { name: "total amount", value: "over 50000" }]);
  });
  it("shows a signal rule's findings, floor and priority back", () => {
    expect(ruleParams({ schema: "plx-alert-rule/v1", signal: { kinds: ["duplicate_supplier_invoice"], min_amount: "10000", currency: "CAD", priority: "high" } }))
      .toEqual([{ name: "Findings", value: "duplicate supplier invoice" }, { name: "Amount at least", value: "10000 CAD" },
        { name: "Priority", value: "high only" }]);
  });
  it("shows nothing for something that is not a spec", () => {
    expect(ruleParams(null)).toEqual([]);
    expect(ruleParams("DROP TABLE")).toEqual([]);
  });
});
