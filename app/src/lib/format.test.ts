import { describe, expect, it } from "vitest";
import { ago, fieldValue, formatMoney, money, objectHref, plainServerNote, readable, safeHref, safeNext, typeLabel, withoutFloatNoise, groupDigits } from "./format";
import { recordLabel } from "./format";

describe("safeHref", () => {
  it("allows same-origin paths and https links only", () => {
    expect(safeHref("/o/po/5280")).toBe("/o/po/5280");
    expect(safeHref("https://outlook.office.com/mail/")).toBe("https://outlook.office.com/mail/");
    for (const bad of ["http://outlook.office.com/mail/", "https://user:pw@evil.example/", "javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,x", "//evil.example", "/\\evil.example", "/\n/evil.example", "/\t/evil.example", "/\r/evil.example", "/x\\..\\\\evil.example", "https://outlook.office.com/\tmail/", "vbscript:x", "file:///etc/passwd", "", null, undefined]) {
      expect(safeHref(bad as string)).toBeNull();
    }
  });
});

describe("safeNext", () => {
  it("only returns same-origin paths", () => {
    expect(safeNext("/ask?q=hi")).toBe("/ask?q=hi");
    expect(safeNext("/po/../ask#top")).toBe("/ask#top");
    // browsers drop tab/newline and read "\\" as "/": each of these would leave the site
    for (const bad of ["https://evil.example", "//evil.example", "/\\evil", "javascript:x", "/signin?next=/", "/signin", "/./signin", "/\n/evil.example", "/\t/evil.example", "/\r\n/evil.example", "/\u0000/evil.example", "/\u007f/evil.example", "/x/\\evil", " //evil.example", 42 as unknown as string, null]) {
      expect(safeNext(bad)).toBe("/");
    }
  });
});

describe("formatMoney", () => {
  it("formats decimal strings without float rounding", () => {
    expect(formatMoney("2245.50", "CAD")).toBe("$2,245.50 CAD");
    expect(formatMoney("0.4491", "CAD")).toBe("$0.4491 CAD");
    expect(formatMoney("12345678901234567.89", "USD")).toBe("$12,345,678,901,234,567.89 USD");
    expect(formatMoney("-0.1", "CAD")).toBe("-$0.10 CAD");
    expect(formatMoney("1000", "EUR")).toBe("€1,000.00 EUR");
  });

  it("shows non-decimal input as given instead of guessing", () => {
    expect(formatMoney("about 2k", "CAD")).toBe("about 2k CAD");
    expect(formatMoney("1e3", "CAD")).toBe("1e3 CAD");
  });
});

describe("object routes and labels", () => {
  it("builds the same route shape operator_api returns as open_url", () => {
    expect(objectHref("PurchaseOrder", "PurchaseOrder:demo:90412")).toBe("/object/PurchaseOrder/PurchaseOrder%3Ademo%3A90412");
    expect(objectHref("Lot", "a/b?c")).toBe("/object/Lot/a%2Fb%3Fc");
  });

  it("names projection types in plain words", () => {
    expect(typeLabel("PurchaseOrder")).toBe("Purchase order");
    expect(typeLabel("purchase_order")).toBe("Purchase order");
    expect(typeLabel("QualityCertificate")).toBe("Quality certificate");
    expect(typeLabel("")).toBe("Record");
  });

  it("formats Money objects", () => {
    expect(money({ amount: "10080.00", currency: "CAD" })).toBe("$10,080.00 CAD");
    expect(money(null)).toBe("");
  });
});

describe("plainServerNote", () => {
  it("shows a plain sentence and drops anything that looks technical", () => {
    expect(plainServerNote("Demo data: sign in with the demo button.")).toBe("Demo data: sign in with the demo button.");
    for (const bad of ['{"error":1}', "<b>x</b>", "Traceback (most recent call last)", "KeyError: 'po'", 'File "/srv/x.py"', "x".repeat(300), "", null]) {
      expect(plainServerNote(bad)).toBeNull();
    }
  });
});

describe("ago", () => {
  it("reads naturally", () => {
    const now = new Date("2026-10-29T12:00:00Z");
    expect(ago("2026-10-29T11:59:30Z", now)).toBe("just now");
    expect(ago("2026-10-29T11:15:00Z", now)).toBe("45 min ago");
    expect(ago("2026-10-28T12:00:00Z", now)).toBe("1 day ago");
  });
});

describe("dates, money and words a reader sees", () => {
  it("shows a date-only value as its own day, never the evening before", async () => {
    const { formatDateTime } = await import("./format");
    const { formatDate, isUnsetDate, fieldValue } = await import("./format");
    expect(formatDateTime("2026-06-24")).toBe("24 Jun 2026");
    // one date style; the year goes when it is the current one
    expect(formatDateTime("2026-06-24T16:00:00Z", new Date("2026-11-01T12:00:00Z"))).toMatch(/^24 Jun, 12:00/);
    expect(formatDateTime("2025-06-24T16:00:00Z", new Date("2026-11-01T12:00:00Z"))).toMatch(/^24 Jun 2025, 12:00/);
    expect(formatDate("2026-06-24", { short: true, now: new Date("2026-11-01T12:00:00Z") })).toBe("24 Jun");
    expect(formatDate("2026-09-02")).toBe("2 Sep 2026");
    // BC's empty date is no date: never "1 Jan 1"
    expect(isUnsetDate("0001-01-01")).toBe(true);
    expect(formatDate("0001-01-01")).toBe("");
    expect(formatDateTime("0001-01-01T00:00:00Z")).toBe("");
    expect(fieldValue("row.requestedReceiptDate", "0001-01-01")).toBe("Not set");
  });

  it("says every time one way, in Eastern: day first, AM/PM, ET", async () => {
    const { formatDateTime, eastern } = await import("./format");
    const now = new Date("2026-11-13T15:00:00Z");
    expect(formatDateTime("2026-11-13T07:52:00Z", now)).toBe("13 Nov, 2:52 AM ET");
    expect(formatDateTime("2026-06-24T16:00:00Z", now)).toBe("24 Jun, 12:00 PM ET");
    // the status lines drop the day when it is today, in Eastern (03:30 UTC is still the 12th there)
    expect(eastern("2026-11-13T14:05:00Z", now)).toBe("9:05 AM ET");
    expect(eastern("2026-11-13T03:30:00Z", now)).toBe("12 Nov, 10:30 PM ET");
    expect(eastern("not a time", now)).toBe("");
  });

  it("gives a reliability problem its headline in words", async () => {
    const { opsHeadline } = await import("./format");
    expect(opsHeadline("sharepoint_lists capture last refreshed 12 days ago (target 3 h); stale since 1 Nov")).toBe("sharepoint lists capture last refreshed 12 days ago (target 3 h)");
  });

  it("always says the currency, whatever its spelling", async () => {
    const { currencyCode } = await import("./format");
    expect(formatMoney("9680", "USD")).toBe("$9,680.00 USD");
    expect(formatMoney("827.11", "CAD$")).toBe("$827.11 CAD");
    expect(currencyCode("US$")).toBe("USD");
    expect(currencyCode("cad")).toBe("CAD");
  });

  it("labels fields and server text in plain words", async () => {
    const { fieldLabel, readable, isInternalField, internalType } = await import("./format");
    // the source line names the system: a label never carries "(BC)"
    expect(fieldLabel("bc_fully_received")).toBe("Fully received");
    expect(fieldLabel("erp_code")).toBe("Code");
    expect(fieldLabel("row.buyFromCity")).toBe("Buy from city");
    expect(fieldLabel("qc_status")).toBe("QC status");
    expect(fieldLabel("custom_fields.fmPoNumber")).toBe("FM PO number");
    expect(fieldLabel("po number")).toBe("PO number");
    for (const k of ["row.id", "row.currencyId", "row.change_key", "row.content_sha256", "row.authentication_results", "external_id"]) expect(isInternalField(k)).toBe(true);
    for (const k of ["row.number", "row.vendorName", "row.totalAmountIncludingTax", "row.orderDate"]) expect(isInternalField(k)).toBe(false);
    expect(readable('Line PurchaseOrderLine:bc.purchase_order_lines:{"id":"ce91"} is open')).toBe("Line Purchase order line is open");
    expect(readable("Not found as of 2026-11-01T05:55:34.427Z")).toBe("Not found as of 1 Nov 2026");
    // a timestamp is said as the Toronto day it falls on
    expect(readable("as of 2026-11-01T02:00:00Z")).toBe("as of 31 Oct 2026");
    expect(readable("needed by 2026-06-24")).toBe("needed by 24 Jun 2026");
    expect(readable("S-ORD101102: LINE_NOT_INVOICED: no posted invoice")).toBe("S-ORD101102: no posted invoice");
    expect(readable("Sales_x0020_Shipment")).toBe("Sales Shipment");
    expect(internalType("BcEntityMapping")).toBe(true);
    expect(internalType("PurchaseOrder")).toBe(false);
  });
});

describe("a float's noise", () => {
  it("never shows: prose, money and record fields read the figure, a real precision stays", () => {
    expect(readable("billed at 116.25000000000001 per KG, net 18.599999999999998")).toBe("billed at 116.25 per KG, net 18.6");
    expect(formatMoney("116.25000000000001", "CAD")).toBe("$116.25 CAD");
    expect(fieldValue("directUnitCost", "116.25000000000001")).toBe("116.25");
    for (const kept of ["3.14159265358", "1.2345678.9", "SKU-116.25000000000001x", "116.25"]) expect(withoutFloatNoise(kept)).toBe(kept);
  });

  it("is a double's shortest form only: a real figure with many places is never rounded (WEB-003)", () => {
    expect(withoutFloatNoise("0.30000000000000004 and -2.0000000000000004")).toBe("0.3 and -2");
    // magnitude never decides: these are real decimals, not a float's residue
    for (const kept of ["100000000.1234567", "1234.5678901", "12345678901234567890.123456789", "0.12345678901234", "1.0000001"]) {
      expect(withoutFloatNoise(kept)).toBe(kept);
    }
    expect(formatMoney("100000000.1234567", "CAD")).toBe("$100,000,000.1234567 CAD");
  });

  it("groups a record's figure from its text, keeping every decimal", () => {
    expect(groupDigits("100000000.1234567")).toBe("100,000,000.1234567");
    expect(groupDigits("1234.56789")).toBe("1,234.56789");
    expect(groupDigits("-2505")).toBe("-2,505");
    expect(groupDigits("116.25000000000001")).toBe("116.25");
    expect(groupDigits("12345678901234567890.5")).toBe("12,345,678,901,234,567,890.5");
    expect(groupDigits("10 kg")).toBe("10 kg");
  });
});

describe("recordLabel", () => {
  it("names every order one way, whatever the source wrote", () => {
    expect(recordLabel("PurchaseOrder", "106001")).toBe("PO 106001");
    expect(recordLabel("PurchaseOrder", "PO100095")).toBe("PO 100095");
    expect(recordLabel("PurchaseOrder", "PO 4809")).toBe("PO 4809");
    expect(recordLabel("SalesOrder", "SO77120")).toBe("SO 77120");
    expect(recordLabel("Customer", "POLARIS")).toBe("POLARIS");
    expect(recordLabel("SalesOrder", "SO SO-11")).toBe("SO-11");
  });
});

describe("readable: the reasoning engine's words, said plainly", () => {
  it("never shows capture names, link names, tier floors or lead carriers", () => {
    const raw =
      "Lateness is not stated: the due dates are not proven complete or readable: the portal_api capture is stale; lead carrier: the portal_api capture is stale; a PurchaseOrder.same_record_as link below the tier floor (lead) is no.";
    const said = readable(raw);
    expect(said).toBe(
      "Can't say yet whether it's late: not every due date could be confirmed: the Portal data is out of date; a possible match to another copy of this order isn't confirmed.",
    );
    for (const word of ["portal_api", "tier floor", "lead carrier", "same_record_as", "PurchaseOrder."]) expect(said).not.toContain(word);
  });

  it("says a stale read in the answer's own sentence by its system", () => {
    expect(readable("Not settled: the portal api read here may be out of date; the projection read here may be out of date.")).toBe(
      "Still to confirm: the Portal data is out of date; the Workbench copy of the records is out of date.",
    );
  });

  it("leaves ordinary text alone", () => {
    expect(readable("Needed by the supplier; ask Purchasing.")).toBe("Needed by the supplier; ask Purchasing.");
  });
});
