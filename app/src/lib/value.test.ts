import { describe, expect, it } from "vitest";
import { parseValue, statusText, STATUSES } from "./value";

describe("value envelope (contract C1)", () => {
  it("refuses an unknown or missing status and unknown fields, never reads them as ok", () => {
    for (const raw of [{ value: 3, status: "fine" }, { value: 3 }, [], "ok", null, { value: 3, status: "ok", extra: 1 }]) {
      expect(() => parseValue(raw)).toThrow();
    }
  });
  it("refuses a status other than ok without a reason or a time", () => {
    for (const status of STATUSES.filter((s) => s !== "ok")) expect(() => parseValue({ value: null, status })).toThrow();
  });
  it("reads the same statuses as operating/value.py", () => {
    expect([...STATUSES]).toEqual(["ok", "partial", "cut", "stale", "unknown", "not_run", "off", "failed"]);
  });
  it("says each status in words, nothing for ok, never a bare 0", () => {
    const now = new Date("2026-11-23T20:00:00Z");
    expect(statusText({ status: "ok", as_of: null, why: null }, now)).toBeNull();
    expect(statusText({ status: "stale", as_of: "2026-11-23T19:26:00Z", why: null }, now)).toBe("As of 2:26 PM ET");
    expect(statusText({ status: "failed", as_of: null, why: "Business Central" }, now)).toBe("Reading failed: Business Central");
    expect(statusText({ status: "unknown", as_of: null, why: "no promised date" }, now)).toBe("Not recorded: no promised date");
  });
});
