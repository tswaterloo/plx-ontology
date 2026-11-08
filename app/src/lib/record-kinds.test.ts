import { describe, expect, it } from "vitest";
import { familyOf, kindSections, peopleIn } from "./record-kinds";
import type { ObjectPage, Source } from "./types";

const src = (field: string): Source => ({ system: "BC", handle: "bc://x", label: "x", fields: { [field]: "x" } });
const fact = (text: string, field: string) => ({ text, sources: [src(field)], check: "read_from_record" });
const page = (type: string, extra: Partial<ObjectPage> = {}) =>
  ({ object_id: "o", type, label: "L", summary: type, complete: true, freshness: {}, status: { state: "read", text: "x", sources: [] }, ...extra }) as unknown as ObjectPage;
const ref = (type: string, label: string) => ({ object_id: `${type}:${label}`, type, label, open_url: `/object/${type}/${label}` });
const by = (s: ReturnType<typeof kindSections>, id: string) => s!.find((x) => x.id === id)!;

describe("tailored sections come from the schema", () => {
  it("families: events, tasks, threads, finance; orders and customers keep their own pages", () => {
    expect(familyOf("Meeting")).toBe("event");
    expect(familyOf("CalendarEvent")).toBe("event");
    expect(familyOf("PlannerTask")).toBe("task");
    expect(familyOf("MCApproval")).toBe("task");
    expect(familyOf("TeamsChat")).toBe("thread");
    expect(familyOf("GeneralLedgerEntry")).toBe("finance");
    expect(familyOf("SalesInvoice")).toBe("finance");
    expect(familyOf("PurchaseOrder")).toBeNull();
    expect(familyOf("Customer")).toBeNull();
    expect(kindSections(page("PurchaseOrder"))).toBeNull();
    expect(kindSections(page("NoSuchType"))).toBeNull();
  });

  it("a calendar event: attendees (linked when a contact is related), agenda, orders and records; empty states are plain", () => {
    const s = kindSections(
      page("CalendarEvent", {
        facts: [fact("Attendees: Ana Ruiz; Bo Chen", "attendees"), fact("Body preview: Agenda: pricing", "body_preview"), fact("Start: 2026-11-03T14:00:00Z", "start")],
        related: [ref("Contact", "Ana Ruiz"), ref("SalesOrder", "SO-1"), ref("Customer", "Acme")],
      }),
    )!;
    const att = by(s, "attendees").rows;
    expect(att.map((r) => r.value)).toEqual(["Ana Ruiz", "Bo Chen"]);
    expect(att[0].href).toBe("/object/Contact/Ana Ruiz");
    expect(att[1].href).toBeUndefined();
    expect(by(s, "agenda").rows[0].value).toContain("pricing");
    expect(by(s, "records").rows.map((r) => r.value)).toContain("SO-1");
    expect(by(s, "parties").rows.length).toBe(1);
    const none = kindSections(page("CalendarEvent"))!;
    expect(by(none, "attendees").rows).toEqual([]);
    expect(by(none, "attendees").empty).toMatch(/No attendees/);
  });

  it("a task: status, assignee, due, history newest first, approvals apart from related records", () => {
    const s = kindSections(
      page("MCTask", {
        facts: [fact("Stage: In review", "stage"), fact("Assignee: Dee", "assignee"), fact("Updated at: 2026-11-02", "updated_at")],
        timeline: [
          { at: "2026-10-01T10:00:00Z", system: "Portal", title: "Created" },
          { at: "2026-11-01T10:00:00Z", system: "Portal", title: "Moved" },
        ] as never,
        related: [ref("MCApproval", "Gate 1"), ref("MCProject", "Launch")],
      }),
    )!;
    expect(by(s, "status").rows[0].value).toBe("In review");
    expect(by(s, "who").rows[0].value).toBe("Dee");
    expect(by(s, "history").rows.map((r) => r.value)).toEqual(["Moved", "Created"]);
    expect(by(s, "approvals").rows.map((r) => r.value)).toEqual(["Gate 1"]);
    expect(by(s, "records").rows.map((r) => r.value)).toEqual(["Launch"]);
  });

  it("a Teams thread: participants from a JSON list, messages, mentioned records", () => {
    const s = kindSections(
      page("TeamsChat", {
        facts: [fact("Participants: " + JSON.stringify([{ displayName: "A" }, { displayName: "B" }]), "participants")],
        linked: [{ kind: "teams", label: "hello", at: "2026-11-01T10:00:00Z", source: src("x") }, { kind: "email", label: "mail", source: src("x") }] as never,
        related: [ref("PurchaseOrder", "106001")],
      }),
    )!;
    expect(by(s, "participants").rows.map((r) => r.value)).toEqual(["A", "B"]);
    expect(by(s, "details").rows.map((r) => r.value)).toEqual(["hello"]);
    expect(by(s, "mentioned").rows.map((r) => r.value)).toEqual(["PO 106001"]);
  });

  it("finance: amounts only when the server sent them; a reader without finance sees the plain reason, never a figure", () => {
    const withAmounts = kindSections(page("GeneralLedgerEntry", { facts: [fact("Debit amount: 1,200.50", "debit_amount"), fact("Account number: 4000", "account_number")], related: [ref("GlAccount", "Sales"), ref("SalesInvoice", "INV-9")] }))!;
    expect(by(withAmounts, "amounts").rows[0].value).toContain("1,200.50");
    expect(by(withAmounts, "account").rows.map((r) => r.value)).toContain("Sales");
    expect(by(withAmounts, "documents").rows.map((r) => r.value)).toContain("INV-9");
    const hidden = kindSections(page("SalesInvoice", { facts: [fact("Status: Open", "status")] }))!;
    expect(by(hidden, "amounts").rows).toEqual([]);
    expect(by(hidden, "amounts").empty).toMatch(/finance access/);
    expect(JSON.stringify(hidden)).not.toMatch(/\d\.\d\d/);
    expect(by(kindSections(page("AgedReceivable"))!, "aging").rows).toEqual([]);
    expect(kindSections(page("Payment"))!.some((x) => x.id === "aging")).toBe(false);
  });

  it("peopleIn: unknown shapes degrade to text, never throw", () => {
    expect(peopleIn("[not json")).toEqual(["[not json"]);
    expect(peopleIn("")).toEqual([]);
    expect(peopleIn(JSON.stringify([{ emailAddress: { name: "Z", address: "z@x" } }]))).toEqual(["Z"]);
  });
});
