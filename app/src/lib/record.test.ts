import { describe, expect, it } from "vitest";
import { missingText } from "@/components/answer-parts";
import { factMap, groupLinked, groupRelated, groupTimeline, isBlocked, recordHead } from "./record";
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
    expect(h.badges.map((b) => b.label)).toEqual(["130 days late", "No confirmed date"]);
    for (const b of h.badges) expect(b.label.split(" ").length).toBeLessThanOrEqual(3);
    expect(h.stats.map((s) => [s.label, s.value])).toEqual([
      ["Total", "$1,993.33"],
      ["Needed by", "24 Jun 2026"],
      ["Received", "1 of 18 kg"],
    ]);
    expect(h.stats[0].caption).toBe("CAD");
    // no "(BC)", no "Fully received", no field dump: six chosen facts at most
    expect(h.keyFacts.filter((k) => k.value).map((k) => `${k.label}: ${k.value}`)).toEqual(["Status: Open", "Supplier: DSM (V06110)", "Currency: CAD"]);
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
      "Some records: their format changed since they were read; the Workbench team can re-read them.",
    );
    expect(missingText({ what: "A value the question asks for", where_it_would_live: "conflicting sources" })).toBe("Part of what you asked: the sources disagree on it.");
    expect(proseMoney("is in Draft and totals 9680 USD.")).toBe("is in Draft and totals $9,680.00 USD.");
    expect(proseMoney("a total of 14,611.33 CAD due")).toBe("a total of $14,611.33 CAD due");
    // a bare number or a PO number is never money
    expect(proseMoney("PO 106093 has 10000 units")).toBe("PO 106093 has 10000 units");
  });
});
