import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LatePo } from "@/lib/types";
import { LateView as LatePage } from "./late-view";

// jsdom has no matchMedia: the page and its toolbar read the width as a phone's
beforeEach(() => {
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: false, media: q, addEventListener: () => undefined, removeEventListener: () => undefined }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const row = (n: number): LatePo => ({
  object_id: `PurchaseOrder:${n}`, label: `PO ${100000 + n}`, reason: "late", open_url: `/object/PurchaseOrder/${n}`, sources: [],
  supplier: n % 3 === 0 ? "DSM" : "Acme", days_late: n, due_date: "2026-06-24",
  amount: n % 2 ? { amount: `${n * 1000}.00`, currency: n % 4 === 1 ? "CAD" : "USD" } : null,
});

/** /api/late over `n` orders, paged as the server pages them (an order may repeat across pages). */
function serve(n: number, suggestions: unknown[] = [], over: (p: LatePo) => LatePo = (p) => p) {
  const all = Array.from({ length: n }, (_, i) => over(row(i + 1)));
  const f = vi.fn(async (url: string) => {
    const u = new URL(String(url), "http://x");
    if (u.pathname === "/api/suggestions") return new Response(JSON.stringify({ status: "read", items: suggestions }), { status: 200 });
    const offset = Number(u.searchParams.get("offset")), limit = Number(u.searchParams.get("limit"));
    const items = all.slice(Math.max(0, offset - 1), offset + limit); // overlaps the last page by one
    return new Response(JSON.stringify({ total: n, offset, items, more: offset + limit < n, next_offset: Math.min(n, offset + limit), as_of: null }), { status: 200 });
  });
  vi.stubGlobal("fetch", f);
  return f;
}

const rows = () => within(screen.getByTestId("late-table")).getAllByTestId("late-row");
const po = (r: HTMLElement) => within(r).getByTestId("late-po").textContent;
const value = (r: HTMLElement) => within(r).getByTestId("late-value");

describe("late list", () => {
  it.each([1, 10, 60, 150])("reads every page of %i orders, none lost or repeated", async (n) => {
    const f = serve(n);
    render(<LatePage />);
    await screen.findByTestId("late-table");
    expect(rows()).toHaveLength(n);
    expect(new Set(rows().map((r) => r.textContent)).size).toBe(n);
    expect(f.mock.calls.filter((c) => String(c[0]).includes("/api/late")).length).toBe(Math.ceil(n / 100));
    expect(screen.getByTestId("late-all-count")).toHaveTextContent(n === 1 ? "1 PO" : `${n} POs`);
    // the page leads with the figures: how many, the oldest, and how long
    expect(screen.getByTestId("late-header")).toHaveTextContent("Late POs");
    expect(screen.getByTestId("late-count")).toHaveTextContent(`${n}${n === 1 ? "PO" : "POs"}`);
    const oldest = screen.getByTestId("late-oldest");
    expect(oldest).toHaveTextContent(`${n}${n === 1 ? "day" : "days"}`);
    expect(oldest).toHaveTextContent(`PO ${100000 + n}, ${n % 3 === 0 ? "DSM" : "Acme"}`);
  });

  it("offers no supplier date filter or count when no PO's supplier date state is known, and says the late step in a row's own words", async () => {
    const sugg = { id: "s1", subject: { object_id: "PurchaseOrder:2", type: "PurchaseOrder", label: "100002", open_url: "" },
      action: { text: "Ask the supplier for a confirmed delivery date and tell the people whose orders need this material.", rule: "late_ask_supplier" } };
    serve(3, [sugg]);
    render(<LatePage />);
    await screen.findByTestId("late-table");
    expect(screen.queryByLabelText("Supplier date")).toBeNull();
    // every PO is up to 30 days late: the empty bands are no tabs
    expect(screen.getAllByRole("tab").map((t) => t.getAttribute("aria-label"))).toEqual(["All 3", "Up to 30 days 3"]);
    expect(screen.queryByText("No supplier date")).toBeNull();
    expect(await within(rows()[1]).findByText("Ask Acme for a confirmed delivery date")).toBeTruthy();
  });

  it("a busy server is a calm notice that tries again by itself", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let calls = 0;
    const ok = serve(3);
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (++calls === 1 ? new Response(JSON.stringify({ error: { code: "busy", message: "x" } }), { status: 503 }) : ok(url))));
    render(<LatePage />);
    const notice = await screen.findByTestId("server-notice");
    expect(notice).toHaveTextContent("Retrying in a few seconds");
    expect(screen.queryByRole("alert")).toBeNull();
    await vi.advanceTimersByTimeAsync(21_000);
    await screen.findByTestId("late-table");
    expect(rows()).toHaveLength(3);
  });

  it("filters by supplier, days late and value band, and sorts by a column", async () => {
    serve(12);
    render(<LatePage />);
    await screen.findByTestId("late-table");
    // longest late first by default
    expect(rows()[0]).toHaveTextContent("PO 100012");
    fireEvent.change(screen.getByLabelText("Supplier"), { target: { value: "DSM" } });
    expect(rows().map(po)).toEqual(["PO 100012", "PO 100009", "PO 100006", "PO 100003"]);
    expect(screen.getByTestId("late-all-count")).toHaveTextContent("4 of 12 match");
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "v10k" } });
    expect(rows().map(po)).toEqual(["PO 100009", "PO 100003"]);
    expect(value(rows()[0])).toHaveTextContent("$9,000.00 CAD");
    fireEvent.change(screen.getByLabelText("Supplier"), { target: { value: "\u0000all" } });
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "all" } });
    fireEvent.click(screen.getByRole("tab", { name: /Up to 30 days 12/ }));
    fireEvent.change(screen.getByLabelText("Sort by"), { target: { value: "value" } });
    // value within each currency, largest first; orders without a value last
    expect(rows().slice(0, 6).map((r) => value(r).textContent)).toEqual([
      "$9,000.00 CAD", "$5,000.00 CAD", "$1,000.00 CAD", "$11,000.00 USD", "$7,000.00 USD", "$3,000.00 USD",
    ]);
    expect(value(rows()[6])).toHaveTextContent("No value");
    expect(screen.getByLabelText("Sort by")).toHaveValue("value");
    expect(screen.getByRole("button", { name: "Sorted largest or latest first" })).toBeTruthy();
  });

  it("gives each row what a buyer chases with, sums the list by supplier and currency, and filters by what blocks it", async () => {
    const sugg = {
      id: "s1", kind: "vendor_chase", rank: 1, headline: "", new: false, acknowledged: false, confidence: {}, subject: { object_id: "PurchaseOrder:6", type: "PurchaseOrder", label: "PO 100006" },
      action: { text: "Confirm the 4 Nov date still holds with the vendor", rule: "x", suggestion_only: true },
      eta: { answer: "yes", in_mail: true, text: "", when: "2026-11-04", source: { system: "Email", handle: "h", label: "mail", as_of: "2026-10-21T15:00:00Z" } },
      draft: { audience: "vendor", body: "", channel: "email", subject: "", sent: false, status: "draft_not_sent", to: { name: "Jordan Lee", basis: "last sender" } },
      impact: { orders_at_risk: 2, orders: [{ object_id: "SalesOrder:77120", type: "SalesOrder", label: "77120", open_url: "" }, { object_id: "AssemblyOrder:7712", type: "AssemblyOrder", label: "AO-7712", open_url: "" }] },
    };
    serve(6, [sugg], (p) => ({ ...p, confirmation: p.object_id === "PurchaseOrder:6" ? "confirmed" : Number(p.object_id.split(":")[1]) % 2 ? "none_found" : "found_unverified", open_lines: 2 }));
    render(<LatePage />);
    await screen.findByTestId("late-table");
    const first = rows()[0];
    expect(po(first)).toBe("PO 100006");
    await within(first).findByText(/promised 4 Nov/);
    expect(within(first).getByTestId("late-chase")).toHaveTextContent("promised 4 Nov · last word 21 Oct, Email · ask Jordan Lee · holds SO 77120 +1 · 2 open lines");
    expect(within(first).getByTestId("late-next")).toHaveTextContent("Confirm the 4 Nov date still holds with the vendor");
    // a row with no drafted follow-up still says its state and the next step from it
    expect(within(rows()[1]).getByTestId("late-chase")).toHaveTextContent(/^no supplier date · 2 open lines$/);
    expect(within(rows()[1]).getByTestId("late-next")).toHaveTextContent("ask the supplier");
    const glance = screen.getByTestId("late-glance");
    expect(glance).toHaveTextContent("2 suppliers");
    expect(glance).toHaveTextContent("$6,000.00 CAD in 2 POs"); // 1 + 5 thousand: CAD rows 1 and 5
    expect(glance).toHaveTextContent("$3,000.00 USD in 1 PO");
    expect(glance).toHaveTextContent("3 without a trusted value");
    expect(glance).toHaveTextContent("3 no supplier date");
    expect(glance).toHaveTextContent("2 date to check");
    fireEvent.change(screen.getByLabelText("Supplier date"), { target: { value: "no_date" } });
    expect(rows().map(po)).toEqual(["PO 100005", "PO 100003", "PO 100001"]);
    expect(screen.getByTestId("late-glance")).toHaveTextContent("3 no supplier date");
    expect(screen.getByTestId("late-glance")).not.toHaveTextContent("date to check");
  });
});
