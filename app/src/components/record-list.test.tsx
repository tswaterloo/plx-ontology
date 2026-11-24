import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const asked = vi.hoisted(() => ({ calls: [] as Array<Record<string, unknown>>, fail: false, push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: asked.push, replace: vi.fn() }), usePathname: () => "/records", useSearchParams: () => new URLSearchParams("") }));
const cell = (t: string, href?: string) => ({ text: t, href });
const rowOf = (n: number) => [cell(`BC-${n}`, `/object/Batch/b${n}`), cell("Base"), { money: { amount: "1234567.5", currency: "CAD" } }, { number: "120000.25 kg" }, { day: "2020-03-05" }, {}];
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getRecordList: (_v: string, f: { filters?: Record<string, string>; offset?: number; sort?: string; direction?: string; facets?: boolean; q?: string }) => {
    asked.calls.push({ ...f });
    if (asked.fail) return Promise.reject(new Error("down"));
    const all = f.filters?.status === "Open" ? [1] : Array.from({ length: 30 }, (_, i) => i + 1);
    const off = f.offset ?? 0;
    const page = all.slice(off, off + 25);
    return Promise.resolve({
      view: "batches", title: "Batches", total: all.length, offset: off, more: off + page.length < all.length, next_offset: off + page.length,
      table: { key: "batches", title: "Batches", columns: ["Batch code", "Product", "Total", "Quantity", "Scheduled", "Note"], rows: page.map(rowOf), total: all.length },
      column_keys: ["batch_code", "product", "total", "quantity", "scheduled_date", "note"], sort: f.sort ?? "scheduled_date", direction: f.direction ?? "desc", searchable: true,
      filters: f.facets ? [{ key: "status", label: "Status", options: [{ value: "", label: "All" }, { value: "Open", label: "Open", count: 1234 }] }] : [],
    });
  },
}));

import { RecordList } from "./record-list";

beforeEach(() => {
  asked.calls = [];
  asked.fail = false;
  asked.push.mockClear();
});

describe("a Records list", () => {
  it("shows money, counts and dates formatted, a dash for no value, and a link on every row", async () => {
    render(<RecordList view="batches" />);
    expect(await screen.findByRole("link", { name: "BC-1" })).toHaveAttribute("href", "/object/Batch/b1");
    const row = screen.getAllByRole("row")[1];
    expect(within(row).getByText("$1,234,567.50 CAD")).toBeInTheDocument();
    expect(within(row).getByText("120,000.25 kg")).toBeInTheDocument();
    expect(within(row).getByText("5 Mar 2020")).toBeInTheDocument();
    expect(within(row).getByLabelText("none")).toHaveTextContent("–");
    await userEvent.click(within(row).getByText("Base"));
    expect(asked.push).toHaveBeenCalledWith("/object/Batch/b1");
    expect(screen.getByTestId("record-list-total")).toHaveTextContent("1 to 25 of 30 records");
    expect(screen.getByRole("option", { name: "Open (1,234)" })).toBeInTheDocument();
  });

  it("sorts by a column, flips it, and asks for the filters' choices only on the first read", async () => {
    render(<RecordList view="batches" />);
    await screen.findByRole("link", { name: "BC-1" });
    expect(asked.calls[0]).toMatchObject({ facets: true });
    await userEvent.click(screen.getByRole("button", { name: "Total" }));
    await waitFor(() => expect(asked.calls.at(-1)).toMatchObject({ sort: "total", direction: "desc", offset: 0 }));
    expect(asked.calls.at(-1)).toMatchObject({ facets: false });
    await userEvent.click(screen.getByRole("button", { name: "Total" }));
    await waitFor(() => expect(asked.calls.at(-1)).toMatchObject({ sort: "total", direction: "asc" }));
    expect(screen.getByRole("columnheader", { name: "Total" })).toHaveAttribute("aria-sort", "ascending");
  });

  it("pages without repeating a row and a filter starts again from the first page", async () => {
    render(<RecordList view="batches" />);
    await screen.findByRole("link", { name: "BC-1" });
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(screen.getByRole("link", { name: "BC-30" })).toBeInTheDocument());
    expect(screen.queryByRole("link", { name: "BC-1" })).toBeNull();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Status" }), "Open");
    await waitFor(() => expect(screen.getAllByRole("link", { name: /BC-/ })).toHaveLength(1));
    expect(asked.calls.at(-1)).toMatchObject({ filters: { status: "Open" }, offset: 0 });
    expect(screen.getByTestId("record-list-total")).toHaveTextContent("1 to 1 of 1 record");
    await userEvent.click(screen.getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(screen.getAllByRole("link", { name: /BC-/ })).toHaveLength(25));
  });

  it("a failed read says so with Retry, never an empty list", async () => {
    asked.fail = true;
    render(<RecordList view="batches" />);
    expect(await screen.findByRole("button", { name: /try again|retry/i })).toBeInTheDocument();
    expect(screen.queryByTestId("record-list-batches")).toBeNull();
  });
});
