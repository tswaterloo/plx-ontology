import { describe, expect, it } from "vitest";
import { snoozeChoices } from "./area";

const at = (now: string) => Object.fromEntries(snoozeChoices(new Date(now)).map(([iso, label]) => [label, iso]));
const toronto = (iso: string) => new Date(iso).toLocaleString("en-CA", { timeZone: "America/Toronto", weekday: "short", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

describe("snooze choices (WEB-018): 8 AM Toronto on the right calendar day", () => {
  it("summer: tomorrow 8:00 reads 8:00 in Toronto (12:00Z), not 9:00", () => {
    const c = at("2026-07-15T15:00:00Z"); // Wednesday
    expect(c["Tomorrow morning"]).toBe("2026-07-16T12:00:00Z");
    expect(toronto(c["Tomorrow morning"])).toContain("08:00");
    expect(c["Next Monday"]).toBe("2026-07-20T12:00:00Z");
  });

  it("winter: 13:00Z is 8:00 in Toronto", () => {
    const c = at("2026-01-14T15:00:00Z");
    expect(c["Tomorrow morning"]).toBe("2026-01-15T13:00:00Z");
    expect(c["Next Monday"]).toBe("2026-01-19T13:00:00Z");
  });

  it("the Saturday night before spring-forward: tomorrow is Sunday, next Monday is Monday", () => {
    const c = at("2026-03-08T04:30:00Z"); // Saturday 23:30 in Toronto, DST starts at 2 AM that night
    expect(toronto(c["Tomorrow morning"])).toContain("Sun");
    expect(toronto(c["Tomorrow morning"])).toContain("08:00");
    expect(toronto(c["Next Monday"])).toContain("Mon");
    expect(toronto(c["Next Monday"])).toContain("2026-03-09");
    expect(toronto(c["Next Monday"])).toContain("08:00");
  });

  it("the Saturday night before fall-back, and late Sunday/Monday boundaries", () => {
    const fall = at("2026-11-01T03:30:00Z"); // Saturday 23:30 EDT, clocks go back at 2 AM
    expect(toronto(fall["Tomorrow morning"])).toContain("Sun, 2026-11-01");
    expect(toronto(fall["Tomorrow morning"])).toContain("08:00");
    expect(toronto(fall["Next Monday"])).toContain("Mon, 2026-11-02");
    expect(toronto(fall["Next Monday"])).toContain("08:00");
    const sunday = at("2026-03-09T03:30:00Z"); // Sunday 23:30 EDT: next Monday is the next day
    expect(toronto(sunday["Next Monday"])).toContain("Mon, 2026-03-09");
    const monday = at("2026-03-09T14:00:00Z"); // Monday morning: next Monday is a week on
    expect(toronto(monday["Next Monday"])).toContain("Mon, 2026-03-16");
  });
});
