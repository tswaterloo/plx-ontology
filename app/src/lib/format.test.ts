import { describe, expect, it } from "vitest";
import { ago, formatMoney, money, objectHref, plainServerNote, safeHref, safeNext, typeLabel } from "./format";

describe("safeHref", () => {
  it("allows same-origin paths and https links only", () => {
    expect(safeHref("/o/po/5280")).toBe("/o/po/5280");
    expect(safeHref("https://outlook.office.com/mail/")).toBe("https://outlook.office.com/mail/");
    for (const bad of ["http://outlook.office.com/mail/", "https://user:pw@evil.example/", "javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,x", "//evil.example", "/\\evil.example", "vbscript:x", "file:///etc/passwd", "", null, undefined]) {
      expect(safeHref(bad as string)).toBeNull();
    }
  });
});

describe("safeNext", () => {
  it("only returns same-origin paths", () => {
    expect(safeNext("/ask?q=hi")).toBe("/ask?q=hi");
    for (const bad of ["https://evil.example", "//evil.example", "/\\evil", "javascript:x", "/signin?next=/", null]) {
      expect(safeNext(bad)).toBe("/");
    }
  });
});

describe("formatMoney", () => {
  it("formats decimal strings without float rounding", () => {
    expect(formatMoney("2245.50", "CAD")).toBe("$2,245.50");
    expect(formatMoney("0.4491", "CAD")).toBe("$0.4491");
    expect(formatMoney("12345678901234567.89", "USD")).toBe("US$12,345,678,901,234,567.89");
    expect(formatMoney("-0.1", "CAD")).toBe("-$0.10");
    expect(formatMoney("1000", "EUR")).toBe("€1,000.00");
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
    expect(money({ amount: "10080.00", currency: "CAD" })).toBe("$10,080.00");
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
