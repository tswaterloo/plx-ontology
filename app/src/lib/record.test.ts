import { describe, expect, it } from "vitest";
import { missingText } from "@/components/answer-parts";
import { factMap, factValue, groupLinked, groupRelated, groupTimeline, isBlocked, recordHead } from "./record";
import type { ObjectPage, Source } from "./types";

const bc = (field: string): Source => ({ system: "BC", handle: "bc://production/c/purchaseOrders/1@e", label: "BC production", fields: { [field]: "x" } });
const fact = (text: string, field: string) => ({ text, sources: [bc(field)], check: "read_from_record" });
const base = { object_id: "o", complete: true, freshness: {}, status: { state: "late", text: "x", sources: [] } } as unknown as ObjectPage;

describe("record page head", () => {
  it("a late PO: number and supplier as the title, a lead sentence, two short badges, money with its currency", () => {
    const po: ObjectPage = {
      ...base,
      type: "PurchaseOrder",
      label: "106001",
      summary: "DSM; no supplier confirmation found yet",
      facts: [fact("Status (BC): Open", "bc_status"), fact("Total cost: 1,993.33 CAD", "total_cost"), fact("Vendor name: DSM", "vendor_name"), fact("Vendor number: V06110", "vendor_number"), fact("Currency: CAD", "currency")],
      related: [{ object_id: "s", type: "Supplier", label: "DSM", open_url: "/object/Supplier/s" }],
      trail: {
        kind: "purchase_order",
        title: "How we know: 106001",
        trails: [
          {
            subject: { object_id: "o", type: "PurchaseOrder", label: "106001", open_url: "/o" },
            state: "late",
            days_late: 130,
            eta: { state: "no_confirmation_found", text: "No vendor confirmation found." },
            steps: [
              { kind: "due_date", title: "Expected on a line by 2026-06-24", check: "read_from_record" },
              { kind: "receipt", title: "Partial receipt: 1 of 18 KG", check: "read_from_record" },
            ],
            hidden_steps: 0,
            may_hide_more: false,
          },
        ],
      },
    } as unknown as ObjectPage;
    const h = recordHead(po);
    expect(h.title).toBe("PO 106001");
    expect(h.counterpart?.label).toBe("DSM");
    expect(h.subline).toBeNull();
    expect(h.lead).toBe("130 days late. DSM hasn't confirmed a date, and 1 of 18 kg has arrived.");
    expect(h.badges.map((b) => b.label)).toEqual(["130 days late", "No supplier confirmation found"]);
    // the lateness badge stays short; the ETA pill says the supplier's confirmation in full (it wraps)
    expect(h.badges[0].label.split(" ").length).toBeLessThanOrEqual(3);
    expect(h.stats.map((s) => [s.label, s.value])).toEqual([
      ["Total", "$1,993.33"],
      ["Needed by", "24 Jun 2026"],
      ["Received", "1 of 18 kg"],
    ]);
    expect(h.stats[0].caption).toBe("CAD");
    // no "(BC)", no "Fully received", no field dump: six chosen facts at most
    expect(h.keyFacts.filter((k) => k.value).map((k) => `${k.label}: ${k.value}`)).toEqual(["Status: Open", "Supplier: DSM (V06110)", "Currency: CAD"]);
  });

  it("says a partial receipt to 2 decimals, and several partial receipts as part arrived, never one line's figures as the PO's", () => {
    const withSteps = (steps: { kind: string; title: string }[]) =>
      ({ ...base, type: "PurchaseOrder", label: "106001", summary: "", facts: [], trail: { kind: "purchase_order", title: "t", trails: [{ subject: { object_id: "p", type: "PurchaseOrder", label: "106001", open_url: "/x" }, state: "late", days_late: 3, steps: steps.map((x) => ({ ...x, check: "read_from_record" })), hidden_steps: 0, may_hide_more: false }] } }) as unknown as ObjectPage;
    const one = recordHead(withSteps([{ kind: "receipt", title: "Partial receipt: 1.6904 of 2 KG" }]));
    expect(one.stats.find((x) => x.label === "Received")).toMatchObject({ value: "1.69 of 2 kg", caption: "85%" });
    const two = recordHead(withSteps([{ kind: "receipt", title: "Partial receipt: 1.6904 of 2 KG" }, { kind: "receipt", title: "Partial receipt: 0.4352 of 1 KG" }]));
    expect(two.stats.find((x) => x.label === "Received")).toMatchObject({ value: "Partly", caption: "2 receipts short of their lines" });
    expect(two.lead).toContain("part of it has arrived");
    expect(two.lead).not.toMatch(/1\.69|0\.43/);
  });

  it("a PO with no trail takes its lateness badge from its own status", () => {
    const po = { ...base, type: "PurchaseOrder", label: "90412", summary: "", facts: [], status: { state: "late", text: "Late by 12 days: due 17 Oct", sources: [] } } as unknown as ObjectPage;
    expect(recordHead(po).badges).toEqual([{ status: "late", label: "12 days late" }]);
  });

  it("a Draft PO says so; a status conflict is said in suspect violet", () => {
    const draft = { ...base, type: "PurchaseOrder", label: "106093", summary: "", facts: [fact("Status (BC): Draft", "bc_status")], status: { state: "missing", text: "Not on record yet: receipt", sources: [] } } as unknown as ObjectPage;
    expect(recordHead(draft).lead).toBe("Draft in Business Central; not sent to the supplier yet.");
    const conflicted = {
      ...draft,
      conflicts: [{ field: "status", values: [{ value: "Complete", trusted: false, why: "", source: { system: "Portal", handle: "api://x", label: "Portal purchase order" } }, { value: "Draft", trusted: false, why: "", source: bc("status") }] }],
    } as unknown as ObjectPage;
    const h = recordHead(conflicted);
    expect(h.lead).toBe("The Portal says Complete; Business Central says Draft. Check with Purchasing before acting.");
    expect(h.leadStatus).toBe("suspect");
  });

  it("a blocked customer says so first and prominently; an empty value is never printed", () => {
    const c = { ...base, type: "Customer", label: "Diversified", summary: "Customer", status: { state: "not_established", text: "Not established: no process", sources: [] }, facts: [fact("Blocked (BC): All", "bc_blocked"), fact("Balance due (BC): 0.00 CAD", "bc_balance_due"), fact("Credit limit (BC): 0", "bc_credit_limit"), fact("Currency: CAD", "currency"), fact("Erp code: C01100", "erp_code")] } as unknown as ObjectPage;
    const h = recordHead(c);
    expect(h.lead).toBe("Blocked in Business Central. No balance owing.");
    expect(h.badges).toEqual([{ status: "late", label: "Blocked" }]);
    expect(h.subline).toBe("C01100");
    expect(h.stats.find((s) => s.label === "Credit limit")?.value).toBe("No limit set");
    expect(h.keyFacts.find((k) => k.label === "Blocked")).toMatchObject({ value: "Blocked in Business Central", status: "late" });
    // "not established" says nothing
    expect(JSON.stringify(h)).not.toMatch(/not established/i);
    expect(isBlocked("")).toBe(false);
    expect(isBlocked("_x0020_")).toBe(false);
    expect(isBlocked("All")).toBe(true);
    const supplier = { ...base, type: "Supplier", label: "DSM", summary: "Supplier", facts: [fact("Blocked (BC): ", "bc_blocked"), fact("Balance (BC): 0.00 CAD", "bc_balance")], status: { state: "late", text: "Late by 130 days: planned arrival", sources: [] }, late_lines: [{ line: "PO number: 106001 (Business Central production)" }], related: [{ object_id: "p", type: "PurchaseOrder", label: "106001", open_url: "/p" }] } as unknown as ObjectPage;
    const s = recordHead(supplier);
    expect(s.lead).toBe("1 purchase order is late, by 130 days. Nothing is owed to DSM.");
    expect(s.keyFacts.find((k) => k.label === "Blocked")?.value).toBe("No");
    expect(s.badges).toEqual([]);
  });

  it("reads each fact by its field, whatever the label says", () => {
    const f = factMap([fact("Status (BC): Open", "bc_status")]);
    expect(f.get("status")).toMatchObject({ label: "Status", value: "Open" });
  });
});

describe("activity titles", () => {
  it("says what happened to which record, and repeats as a count of that kind", async () => {
    const { activityTitle } = await import("./record");
    expect(activityTitle("Required: sales order SO-FM-23", 1)).toBe("Sales order SO-FM-23 due");
    expect(activityTitle("Required: sales order SO-FM-23", 6)).toBe("6 sales orders due");
    expect(activityTitle("Posted: receipt 107057", 3)).toBe("3 receipts posted");
    // an unknown verb stays as written, never guessed
    expect(activityTitle("Frobbed: thing 1", 2)).toBe("Frobbed ×2");
    expect(activityTitle("Order placed", 1)).toBe("Order placed");
  });
});

describe("record page lists", () => {
  it("groups messages by thread, activity by month with repeats collapsed, and related by type without type-named rows", () => {
    const mail = (label: string, at: string) => ({ kind: "email" as const, label, at, source: bc("x") });
    const t = groupLinked([mail("Quality Concerns", "2026-09-01"), mail("RE: Quality Concerns", "2026-09-03"), mail("FW: Quality Concerns", "2026-09-02"), mail("Other", "2026-08-01")]);
    expect(t.map((x) => [x.subject, x.count, x.latest])).toEqual([["Quality Concerns", 3, "2026-09-03"], ["Other", 1, "2026-08-01"]]);
    const ev = (title: string, at: string) => ({ title, at, system: "Portal" as const });
    const months = groupTimeline([...Array.from({ length: 40 }, (_, i) => ev(`Required: sales order SO-${i}`, "2025-03-21")), ev("Posted: order", "2025-04-02")]);
    expect(months.map((m) => [m.month, m.rows.map((r) => r.count)])).toEqual([["April 2025", [1]], ["March 2025", [40]]]);
    const rel = groupRelated([
      { object_id: "1", type: "PurchaseOrderLine", label: "PO line", open_url: "" },
      { object_id: "2", type: "Receipt", label: "Receipt", open_url: "" },
      { object_id: "3", type: "Document", label: "2026091006454040.pdf", open_url: "" },
      { object_id: "4", type: "Document", label: "2026091006454040.pdf", open_url: "" },
    ]);
    expect(rel.find((g) => g.type === "Document")?.items).toHaveLength(1);
    expect(rel.find((g) => g.type === "Document")?.items[0].times).toBe(2);
    expect(rel.find((g) => g.type === "Receipt")?.items).toHaveLength(0);
  });

  it("says a missing item as a task with its owner last, without repeating the record's name", () => {
    expect(
      missingText({ what: "Supplier confirmation of Purchase order 106001", where_it_would_live: "Ask Purchasing. Supplier confirmation: Not found yet; not fully searched: Mail, SharePoint" }, "106001"),
    ).toBe("Supplier confirmation: not found yet; Mail and SharePoint aren't fully searched. Ask Purchasing.");
    expect(missingText({ what: "A new ship date", where_it_would_live: "supplier email" })).toBe("A new ship date. Supplier email.");
  });
});

describe("plain words for gaps and money in prose", () => {
  it("says the server's plumbing gaps as a reader would, and prose amounts with their currency", async () => {
    const { proseMoney } = await import("./format");
    expect(missingText({ what: "projection", where_it_would_live: "schema drift" })).toBe(
      "Some records may be incomplete: saved in an older format; the Workbench team can refresh them.",
    );
    // a stale read says which system's data may be out of date, never "the Portal. Stale source."
    expect(missingText({ what: "the Portal", where_it_would_live: "stale source" })).toBe("Portal data may be out of date.");
    expect(missingText({ what: "Business Central", where_it_would_live: "stale source" })).toBe("Business Central data may be out of date.");
    expect(missingText({ what: "A value the question asks for", where_it_would_live: "conflicting sources" })).toBe("Part of what you asked: the sources disagree on it.");
    expect(proseMoney("is in Draft and totals 9680 USD.")).toBe("is in Draft and totals $9,680.00 USD.");
    expect(proseMoney("a total of 14,611.33 CAD due")).toBe("a total of $14,611.33 CAD due");
    // a bare number or a PO number is never money
    expect(proseMoney("PO 106093 has 10000 units")).toBe("PO 106093 has 10000 units");
  });
});

describe("factValue", () => {
  it("writes a fact's date like every other date and leaves anything else", () => {
    expect(factValue("Oct 28, 2026")).toBe("28 Oct 2026");
    expect(factValue("2026-06-24")).toBe("24 Jun 2026");
    expect(factValue("P-008864")).toBe("P-008864");
    expect(factValue("25,113.600 USD")).toBe("$25,113.60 USD");
    expect(factValue("Foo 28, 2026")).toBe("Foo 28, 2026");
  });
});
