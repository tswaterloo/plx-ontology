import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LatePo } from "@/lib/types";
import LatePage from "./page";

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
function serve(n: number) {
  const all = Array.from({ length: n }, (_, i) => row(i + 1));
  const f = vi.fn(async (url: string) => {
    const u = new URL(String(url), "http://x");
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
    expect(f).toHaveBeenCalledTimes(Math.ceil(n / 100));
    expect(screen.getByTestId("late-all-count")).toHaveTextContent(n === 1 ? "1 PO" : `${n} POs`);
    // the page leads with the figures: how many, the oldest, and how long
    expect(screen.getByTestId("late-header")).toHaveTextContent("Late POs");
    expect(screen.getByTestId("late-count")).toHaveTextContent(`${n}${n === 1 ? "PO" : "POs"}`);
    const oldest = screen.getByTestId("late-oldest");
    expect(oldest).toHaveTextContent(`${n}${n === 1 ? "day" : "days"}`);
    expect(oldest).toHaveTextContent(`PO ${100000 + n}, ${n % 3 === 0 ? "DSM" : "Acme"}`);
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
});
