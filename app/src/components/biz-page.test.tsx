import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/errors";
import type { BizPage } from "@/lib/types";

const nav = vi.hoisted(() => ({ params: new URLSearchParams("kind=chain&chain=TAKE%205") }));
const api = vi.hoisted(() => ({ bizPage: vi.fn() }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => nav.params,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/account",
}));
// signed in, so the toolbar's Pin shows whenever the page offers it
vi.mock("@/components/auth", async (orig) => ({
  ...(await orig<object>()),
  useAuth: () => ({ state: { status: "signed-in", me: { display_name: "Ana", principals: ["staff:ana"] } } }),
}));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), bizPage: api.bizPage }));

import { BizArticle, BizRecordPage } from "./biz-page";

const CHAIN: BizPage = {
  kind: "chain",
  title: "TAKE 5",
  subtitle: "Chain of 3 accounts",
  facts: [{ label: "Currency", value: { text: "CAD", hidden: false } }],
  sections: [
    {
      key: "owed",
      title: "What they owe",
      columns: ["Currency", "Not yet due", "Total"],
      rows: [[{ text: "CAD", hidden: false }, { money: { amount: "15.00", currency: "CAD" }, hidden: false }, { hidden: true }]],
      total: 1,
      note: "1 open document you can't see is left out of the totals.",
    },
    { key: "open_orders", title: "Open orders", columns: ["Order"], rows: [], total: 0, note: "None open." },
  ],
  sources: "Sources: Sage; as of Nov 17, 9:04 PM ET",
  took_ms: 40,
};

function show() {
  return render(<BizRecordPage allowed={["customer", "chain", "supplier"]} fallbackTitle="Account" />);
}

afterEach(() => {
  api.bizPage.mockReset();
  nav.params = new URLSearchParams("kind=chain&chain=TAKE%205");
});

describe("BizRecordPage", () => {
  it("reads the page once and shows its numbers, notes and one Sources line", async () => {
    api.bizPage.mockResolvedValue(CHAIN);
    show();
    expect(await screen.findByRole("heading", { name: "TAKE 5" })).toBeTruthy();
    expect(api.bizPage).toHaveBeenCalledWith("chain", { h: "", chain: "TAKE 5" }, expect.anything());
    expect(screen.getByText("$15.00", { exact: false })).toBeTruthy();
    expect(screen.getByText("Restricted")).toBeTruthy();
    expect(screen.getByText(/left out of the totals/)).toBeTruthy();
    expect(screen.getByText("None open.")).toBeTruthy();
    expect(screen.getAllByTestId("biz-sources")).toHaveLength(1);
  });

  it("never shows an empty verdict while it reads", async () => {
    api.bizPage.mockReturnValue(new Promise(() => {}));
    show();
    expect(screen.queryByText("None open.")).toBeNull();
    expect(screen.queryByTestId("biz-page")).toBeNull();
    expect(await screen.findByRole("status")).toBeTruthy();
  });

  it("says what failed instead of a blank page", async () => {
    api.bizPage.mockRejectedValue(new ApiError(503, "page_unavailable", undefined, "The record's figures aren't built yet; try again in a minute."));
    show();
    expect(await screen.findByText(/figures aren't built yet/)).toBeTruthy();
    expect(screen.queryByTestId("biz-page")).toBeNull();
  });

  it("refuses a kind this page does not show, without reading", async () => {
    nav.params = new URLSearchParams("kind=purchase_order&h=x");
    show();
    expect(await screen.findByText("This page shows one account, and this address names none.")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open Orders" })).toHaveAttribute("href", "/orders");
    expect(screen.getByRole("button", { name: "Search" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Pin / })).toBeNull();
    expect(api.bizPage).not.toHaveBeenCalled();
  });

  it("offers no Pin while the record could not be read, and offers it once it is", async () => {
    api.bizPage.mockRejectedValueOnce(new ApiError(404, "not_found"));
    const view = show();
    await screen.findByRole("alert").catch(() => undefined);
    await vi.waitFor(() => expect(api.bizPage).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: /^Pin / })).toBeNull();
    view.unmount();
    api.bizPage.mockResolvedValue(CHAIN);
    show();
    expect(await screen.findByRole("button", { name: "Pin TAKE 5 to Home" })).toBeTruthy();
  });
});

describe("Section columns", () => {
  it("leaves out a column no row has a value in, and keeps every row", () => {
    const page = { kind: "purchase_order", title: "PO1", subtitle: null, facts: [], sources: "Sage", took_ms: 1, as_of: null, sections: [
      { key: "lines", title: "Lines", columns: ["Item", "Description", "Unit price"], total: 2, rows: [
        [{}, { text: "Towels" }, { hidden: true }], [{}, { text: "Soap" }, {}]] }] };
    render(<BizArticle page={page as never} />);
    expect(screen.queryByRole("columnheader", { name: "Item" })).toBeNull();
    expect(screen.getByRole("columnheader", { name: "Description" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "Unit price" })).toBeTruthy();   // a hidden value is not empty
    expect(screen.getByText("Towels")).toBeTruthy();
    expect(screen.getByText("Soap")).toBeTruthy();
  });
});
