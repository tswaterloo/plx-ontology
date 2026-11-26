import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const asked = vi.hoisted(() => ({ calls: [] as Array<Record<string, unknown>>, fail: false, hold: false, push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: asked.push, replace: vi.fn() }), usePathname: () => "/records", useSearchParams: () => new URLSearchParams("") }));
const cell = (t: string, href?: string) => ({ text: t, href, hidden: false });
const rowOf = (n: number) => [cell(`BC-${n}`, `/object/Batch/b${n}`), cell("Base"), { money: { amount: "1234567.5", currency: "CAD" } }, { number: "120000.25 kg" }, { day: "2020-03-05" }, {}];
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getRecordList: (_v: string, f: { filters?: Record<string, string>; offset?: number; sort?: string; direction?: string; facets?: boolean; q?: string }) => {
    asked.calls.push({ ...f });
    if (asked.fail) return Promise.reject(new Error("down"));
    if (asked.hold) return new Promise(() => undefined);
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
import { recordListPath } from "@/lib/api";
import { forgetKeptReads, keepRead } from "@/lib/use-read";

beforeEach(() => {
  asked.calls = [];
  asked.fail = false;
  asked.hold = false;
  asked.push.mockClear();
  forgetKeptReads();
});

describe("a Records list", () => {
  it("shows money, counts and dates formatted, a dash for no value, and a link on every row", async () => {
    render(<RecordList view="batches" />);
    expect(await screen.findByRole("link", { name: "BC-1" })).toHaveAttribute("href", "/object/Batch/b1");
    const row = screen.getAllByRole("row")[1];
    expect(within(row).getByText("$1,234,567.50 CAD")).toBeInTheDocument();
    expect(within(row).getByText("120,000.25 kg")).toBeInTheDocument();
    expect(within(row).getByText("5 Mar 2020")).toBeInTheDocument();
    expect(within(row).getByLabelText("none")).toHaveTextContent("—");
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

  it("the stored-read key is the exact API path, with filter keys in one order", () => {
    expect(recordListPath("batches", { filters: { zeta: "two words", status: "Open" }, q: "base", sort: "total", direction: "desc", offset: 50, facets: false })).toBe("/api/records/list?view=batches&q=base&offset=50&facets=false&sort=total&direction=desc&f=status%3AOpen&f=zeta%3Atwo+words");
  });

  it("a second visit draws the kept list in the first render, labelled while it is read again", () => {
    const path = recordListPath("batches", { offset: 0, facets: true, q: "" });
    keepRead(path, {
      view: "batches", title: "Batches", total: 1, offset: 0, more: false, next_offset: 1,
      table: { key: "batches", title: "Batches", columns: ["Batch code"], rows: [[cell("BC-stored", "/object/Batch/stored")]], total: 1 },
      column_keys: ["batch_code"], sort: "scheduled_date", direction: "desc", searchable: true, filters: [],
    }, true, undefined, Date.now() - 60_000);
    asked.hold = true;
    render(<RecordList view="batches" />);
    expect(screen.getByRole("link", { name: "BC-stored" })).toBeInTheDocument();
    expect(screen.getByText(/reading it again/)).toBeInTheDocument();
    expect(asked.calls[0]).toMatchObject({ facets: true, offset: 0 });
  });

  it("formats verdicts honestly (never labeling nothing_outstanding or stale as late), provides All/Open/Late quick filter, and hides duplicate search box when controlled", async () => {
    const { formatStatusLabel, formatVerdict, isRowActivelyLate } = await import("./record-list");
    expect(formatStatusLabel("open")).toBe("Open");
    expect(formatStatusLabel("closed")).toBe("Closed");
    expect(formatStatusLabel("cancelled")).toBe("Cancelled");
    expect(formatVerdict("neither")).toMatchObject({ label: "—" });
    expect(formatVerdict("nothing_outstanding")).toMatchObject({ label: "Closed" });
    expect(formatVerdict("stale")).toMatchObject({ label: "Stale" });
    expect(formatVerdict("late (not counted)")).toMatchObject({ label: "Late (excluded)" });
    expect(formatVerdict("on_time")).toMatchObject({ label: "On time" });
    expect(formatVerdict("late")).toMatchObject({ label: "Late" });

    expect(isRowActivelyLate([cell("Open"), cell("nothing_outstanding")], ["status", "verdict"])).toBe(false);
    expect(isRowActivelyLate([cell("Open"), cell("stale")], ["status", "verdict"])).toBe(false);
    expect(isRowActivelyLate([cell("Open"), cell("late (not counted)")], ["status", "verdict"])).toBe(false);
    expect(isRowActivelyLate([cell("Closed"), cell("late")], ["status", "verdict"])).toBe(false);
    expect(isRowActivelyLate([cell("Open"), cell("late")], ["status", "verdict"])).toBe(true);

    render(<RecordList view="batches" externalQuery="" />);
    await screen.findByRole("link", { name: "BC-1" });
    expect(screen.queryByRole("searchbox", { name: "Search this list" })).toBeNull();
    expect(screen.getByTestId("record-quick-filter")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("quick-filter-open"));
    await waitFor(() => expect(asked.calls.at(-1)).toMatchObject({ filters: { status: "Open" } }));
  });

  it("preserves active filter and page when leaving via a row click and restores it on remount", async () => {
    const { unmount } = render(<RecordList view="batches" />);
    await screen.findByRole("link", { name: "BC-1" });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Status" }), "Open");
    await waitFor(() => expect(screen.getAllByRole("link", { name: /BC-/ })).toHaveLength(1));
    const row = screen.getAllByRole("row")[1];
    await userEvent.click(within(row).getByText("Base"));
    expect(asked.push).toHaveBeenCalledWith("/object/Batch/b1");
    unmount();

    asked.calls = [];
    render(<RecordList view="batches" />);
    await waitFor(() => expect(asked.calls[0]).toMatchObject({ filters: { status: "Open" } }));
  });
});
