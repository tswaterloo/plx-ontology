import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const asked = vi.hoisted(() => ({ calls: [] as Array<Record<string, unknown>>, fail: false }));
const cell = (t: string, href?: string) => ({ text: t, href });
const batch = (n: number) => [cell(`BC-${n}`, `/object/Batch/b${n}`), cell("Base"), cell("L"), cell("Realized"), cell("Released"), {}, {}];
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getRecordList: (_v: string, f: { status?: string; hold?: string; offset?: number }) => {
    asked.calls.push({ ...f });
    if (asked.fail) return Promise.reject(new Error("down"));
    const all = f.hold === "yes" ? [1] : [1, 2, 3];
    const off = f.offset ?? 0;
    const page = all.slice(off, off + 2);
    return Promise.resolve({ view: "production", title: "Production batches", total: all.length, offset: off, more: off + page.length < all.length, next_offset: off + page.length,
      table: { key: "batches", title: "Batches", columns: ["Batch", "Product", "Lot", "Status", "QC", "Scheduled", "Produced"], rows: page.map(batch), total: all.length },
      filters: [{ key: "hold", label: "QC hold", options: [{ value: "", label: "All" }, { value: "yes", label: "On hold" }] }] });
  },
}));

import { RecordList } from "./record-list";

describe("Records production list", () => {
  it("pages without repeating a row, links each batch, and a filter starts again from the first row", async () => {
    render(<RecordList view="production" query="" />);
    expect(await screen.findByRole("link", { name: "BC-1" })).toHaveAttribute("href", "/object/Batch/b1");
    await userEvent.click(screen.getByRole("button", { name: "Show more" }));
    await waitFor(() => expect(screen.getByRole("link", { name: "BC-3" })).toBeInTheDocument());
    expect(screen.getAllByRole("link", { name: /BC-/ })).toHaveLength(3);
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "QC hold" }), "yes");
    await waitFor(() => expect(screen.getAllByRole("link", { name: /BC-/ })).toHaveLength(1));
    expect(asked.calls.at(-1)).toMatchObject({ hold: "yes", offset: 0 });
    expect(screen.getByTestId("record-list-total")).toHaveTextContent("1 in all");
  });

  it("a failed read says so with Retry, never an empty list", async () => {
    asked.fail = true;
    render(<RecordList view="production" query="" />);
    expect(await screen.findByRole("button", { name: /try again|retry/i })).toBeInTheDocument();
    expect(screen.queryByTestId("record-list-production")).toBeNull();
  });
});
