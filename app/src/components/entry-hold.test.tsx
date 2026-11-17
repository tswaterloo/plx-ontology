import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { EntryHold as Hold, InstructPart } from "@/lib/types";
import { InstructionPart } from "./instruction-part";

const HOLD: Hold = {
  entry_id: "eh_0123456789abcdef01234567",
  kind: "order",
  label: "New BC sales order, held until each check is settled",
  open: 2,
  write: null,
  fields: [
    { key: "customer", field: "customer", line: null, label: "Customer", status: "check", reason: "the PO names the customer only by name", options: [] },
    { key: "line:1:item", field: "item", line: 1, label: "Item (line 1)", status: "cited", value: "SYN-10", options: [] },
    { key: "line:1:quantity", field: "quantity", line: 1, label: "Quantity (line 1)", status: "cited", value: "6", options: [] },
    {
      key: "line:1:unit_price",
      field: "unit_price",
      line: 1,
      label: "Unit price (line 1)",
      status: "check",
      reason: "the PO states no price",
      options: [{ value: "4.25", currency: "CAD", unit: "CS", basis: "last price", date: "2026-10-02", record: null }],
    },
  ],
};

const PART: InstructPart = {
  status: "drafted",
  text: "create an order from this PO",
  question: null,
  workflow: null,
  writes: [{ label: "BC Sandbox: new sales order header", status: "refused", problems: ["Line 1: its price is a check"], changes: [] }],
  documents: [],
  read: [],
  context: {},
  answers: [],
  meetings_used: [],
  entry: HOLD,
};

let fetchMock: ReturnType<typeof vi.fn>;
let replies: unknown[];

beforeEach(() => {
  replies = [];
  fetchMock = vi.fn(async (url: string) => {
    if (url.startsWith("/api/writes")) return new Response(JSON.stringify({ items: [] }), { status: 200 });
    if (url.startsWith("/api/suggest"))
      return new Response(
        JSON.stringify({
          query: "syn",
          took_ms: 1,
          source: "fixture",
          suggestions: [
            { object_id: "Customer:portal.sage_ar_customer:1", type: "Customer", label: "C100 Synth customer", match: "prefix", score: 1, open_url: "/o/1" },
            { object_id: "Product:bc.items:9", type: "Product", label: "SYN-10 soap", match: "prefix", score: 1, open_url: "/o/9" },
          ],
        }),
        { status: 200 },
      );
    return new Response(JSON.stringify(replies.shift()), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const posts = () => fetchMock.mock.calls.filter(([u]) => String(u).startsWith("/api/entries/"));
const bodyOf = (i: number) => JSON.parse(String((posts()[i][1] as RequestInit).body));

describe("EntryHold", () => {
  it("settles a price from its suggestion and a customer from a search, then shows the desk entry", async () => {
    render(<InstructionPart part={PART} answerId="a1" onInstruct={vi.fn()} />);
    // the refused write's reasons are the checks below: not shown twice
    expect(screen.queryByText("Line 1: its price is a check")).toBeNull();
    expect(screen.getByTestId("entry-hold").textContent).toContain("2 to settle");
    const priced = { ...HOLD, open: 1, fields: HOLD.fields.map((f) => (f.key === "line:1:unit_price" ? { ...f, status: "confirmed" as const, value: "4.25", currency: "CAD", confirmed_by: "Ana", confirmed_at: "2026-11-15T23:00:00Z", options: [] } : f)) };
    replies = [priced];
    fireEvent.click(screen.getByRole("button", { name: "Use 4.25 CAD per CS (last price, 2026-10-02)" }));
    // a suggestion only fills the form: nothing is confirmed until the Confirm click
    expect(posts()).toHaveLength(0);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Confirm unit price (line 1)" })));
    expect(String(posts()[0][0])).toBe("/api/entries/eh_0123456789abcdef01234567/confirm");
    expect(bodyOf(0)).toEqual({ key: "line:1:unit_price", value: "4.25", currency: "CAD" });
    expect(screen.getByText(/Confirmed by Ana/)).toBeTruthy();

    // the customer: a search lists customer records only; the pick is sent as a record
    await act(async () => fireEvent.change(screen.getByLabelText("Search customers"), { target: { value: "syn" } }));
    expect(screen.getByTestId("entry-found").textContent).toBe("C100 Synth customer");
    fireEvent.click(screen.getByRole("button", { name: "C100 Synth customer" }));
    const done = {
      ...priced,
      open: 0,
      fields: priced.fields.map((f) => (f.key === "customer" ? { ...f, status: "confirmed" as const, value: "C100 Synth customer", confirmed_by: "Ana", options: [] } : f)),
      write: { intent_id: "wi_0123456789abcdef01234567", label: "BC Sandbox: new sales order header", status: "prepared" as const, problems: [], changes: [] },
    };
    replies = [done];
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Confirm customer" })));
    expect(bodyOf(1)).toEqual({ key: "customer", record: "Customer:portal.sage_ar_customer:1" });
    expect(screen.getByTestId("entry-hold").textContent).toContain("The entry is on the write desk");
    expect(screen.getByText("A colleague who holds the write grant approves it on the write desk.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Confirm / })).toBeNull();
    // nothing approved or executed from here
    expect(fetchMock.mock.calls.some(([u]) => /\/approve|\/execute/.test(String(u)))).toBe(false);
  });

  it("shows the server's reason when a value is refused", async () => {
    render(<InstructionPart part={PART} answerId="a2" onInstruct={vi.fn()} />);
    fetchMock.mockImplementationOnce(async () => new Response(JSON.stringify({ error: { code: "bad_value", message: "A price is zero or more." } }), { status: 422 }));
    fireEvent.change(screen.getByLabelText("Unit price (line 1)"), { target: { value: "-1" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Confirm unit price (line 1)" })));
    expect(screen.getByText("A price is zero or more.")).toBeTruthy();
  });
});
