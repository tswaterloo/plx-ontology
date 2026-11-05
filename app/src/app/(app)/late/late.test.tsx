import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { LatePo } from "@/lib/types";
import LatePage from "./page";

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

const rows = () => within(screen.getByTestId("late-table")).getAllByTestId("late-row") as HTMLTableRowElement[];

describe("late list", () => {
  it.each([1, 10, 60, 150])("reads every page of %i orders, none lost or repeated", async (n) => {
    const f = serve(n);
    render(<LatePage />);
    await screen.findByTestId("late-table");
    expect(rows()).toHaveLength(n);
    expect(new Set(rows().map((r) => r.textContent)).size).toBe(n);
    expect(f).toHaveBeenCalledTimes(Math.ceil(n / 100));
    expect(screen.getByTestId("late-all-count")).toHaveTextContent(n === 1 ? "1 PO" : `${n} POs`);
    // the page leads with the verdict and the why: how many, the oldest, and how long
    const head = screen.getByTestId("late-header");
    expect(head).toHaveTextContent(`Late POs${n} past the needed-by date.`);
    expect(head).toHaveTextContent(`Oldest: PO ${100000 + n} (${n % 3 === 0 ? "DSM" : "Acme"}), ${n === 1 ? "1 day" : `${n} days`}.`);
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
    expect(rows().map((r) => r.cells[0].textContent)).toEqual(["PO 100012", "PO 100009", "PO 100006", "PO 100003"]);
    expect(screen.getByTestId("late-all-count")).toHaveTextContent("4 of 12 match");
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "v10k" } });
    expect(rows().map((r) => r.cells[0].textContent)).toEqual(["PO 100009", "PO 100003"]);
    expect(rows()[0].cells[4]).toHaveTextContent("$9,000.00 CAD");
    fireEvent.change(screen.getByLabelText("Supplier"), { target: { value: "\u0000all" } });
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "all" } });
    fireEvent.click(screen.getByRole("tab", { name: /Up to 30 days 12/ }));
    fireEvent.click(screen.getByRole("button", { name: /Value/ }));
    // value within each currency, largest first; orders without a value last
    expect(rows().slice(0, 6).map((r) => r.cells[4].textContent)).toEqual([
      "$9,000.00 CAD", "$5,000.00 CAD", "$1,000.00 CAD", "$11,000.00 USD", "$7,000.00 USD", "$3,000.00 USD",
    ]);
    expect(rows()[6].cells[4]).toHaveTextContent("No value");
    expect(screen.getByRole("columnheader", { name: /Value/ })).toHaveAttribute("aria-sort", "descending");
  });
});
