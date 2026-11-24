import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SuggestResponse } from "@/lib/types";

const nav = vi.hoisted(() => ({ params: new URLSearchParams(""), push: vi.fn(), replace: vi.fn() }));
const api = vi.hoisted(() => ({ suggest: vi.fn(), views: vi.fn(), list: vi.fn() }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => nav.params,
  useRouter: () => ({ push: nav.push, replace: nav.replace }),
  usePathname: () => "/records",
}));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), suggest: api.suggest, getRecordViews: api.views, getRecordList: api.list }));

import RecordsPage from "./page";

const row = (type: string, object_id: string, label: string, open_url: string) => ({ object_id, type, label, match: "exact" as const, score: 1, open_url, same_as: [] });

function found(query: string): SuggestResponse {
  return {
    query,
    source: "business",
    took_ms: 3,
    suggestions: [
      row("purchase_order", "po-1", "106093", "/order?kind=purchase_order&handle=po-1"),
      row("supplier", "sup-1", "Croda Canada", "/account?kind=supplier&handle=sup-1"),
    ],
  };
}

describe("Records page", () => {
  beforeEach(() => {
    nav.params = new URLSearchParams("");
    nav.push.mockReset();
    api.suggest.mockReset();
    api.suggest.mockImplementation((q: string) => Promise.resolve(found(q)));
    // a tab shows its list of records under the box
    api.list.mockReset();
    api.list.mockImplementation((view: string) =>
      Promise.resolve({ view, title: view, total: 0, offset: 0, more: false, next_offset: 0, table: { key: view, title: view, columns: ["Name"], rows: [], total: 0 }, column_keys: ["name"], sort: "name", direction: "asc", searchable: true, filters: [] }),
    );
  });

  it("opens on the All tab, which lists every kind the box found", async () => {
    render(<RecordsPage />);
    const tabs = screen.getAllByRole("tab");
    expect(tabs[0]).toHaveTextContent("All");
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
    await userEvent.type(screen.getByTestId("records-search"), "106093");
    const list = await screen.findByTestId("records-found");
    expect(list).toHaveTextContent("PO 106093");
    expect(list).toHaveTextContent("Croda Canada");
  });

  it("Enter opens the best match", async () => {
    render(<RecordsPage />);
    const box = screen.getByTestId("records-search");
    await userEvent.type(box, "106093");
    await screen.findByTestId("records-found");
    await userEvent.type(box, "{Enter}");
    expect(nav.push).toHaveBeenCalledWith("/order?kind=purchase_order&handle=po-1");
  });

  it("a narrower tab keeps only its own kind, and Enter opens the best of those", async () => {
    nav.params = new URLSearchParams("kind=suppliers");
    render(<RecordsPage />);
    const box = screen.getByTestId("records-search");
    await userEvent.type(box, "106093");
    const list = await screen.findByTestId("records-found");
    expect(list).not.toHaveTextContent("106093");
    expect(list).toHaveTextContent("Croda Canada");
    await userEvent.type(box, "{Enter}");
    expect(nav.push).toHaveBeenCalledWith("/account?kind=supplier&handle=sup-1");
  });

  it("an empty search box says what to type, and a kind's tab lists its newest records", async () => {
    render(<RecordsPage />);
    expect(screen.getByTestId("records-empty-help")).toHaveTextContent("Type a name or a number above");
    nav.params = new URLSearchParams("kind=orders");
    render(<RecordsPage />);
    await waitFor(() => expect(api.list).toHaveBeenCalledWith("purchase_orders", expect.anything(), expect.anything()));
  });

  it("the answer for the earlier words stays up, dimmed, until the next one lands, and Enter never opens a stale one", async () => {
    let release: (r: SuggestResponse) => void = () => undefined;
    const one = { query: "cro", source: "business", took_ms: 1, suggestions: [{ object_id: "s1", type: "Supplier", label: "Croda Canada", match: "prefix", score: 0.9, open_url: "/account?kind=supplier&handle=s1" }] } as SuggestResponse;
    api.suggest.mockReset();
    api.suggest.mockResolvedValueOnce(one).mockImplementationOnce(() => new Promise<SuggestResponse>((r) => (release = r)));
    render(<RecordsPage />);
    const box = screen.getByTestId("records-search");
    await userEvent.type(box, "cro");
    expect(await screen.findByTestId("records-found")).toHaveTextContent("Croda Canada");
    await userEvent.type(box, "d");
    await waitFor(() => expect(api.suggest).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("records-found").className).toContain("opacity-60");
    nav.push.mockClear();
    await userEvent.type(box, "{Enter}");
    expect(nav.push).not.toHaveBeenCalled();
    release({ ...one, query: "crod" });
    await waitFor(() => expect(screen.getByTestId("records-found").className).not.toContain("opacity-60"));
  });

  it("a good answer is not replaced by 'stay good later, and a search that never answers gives up", async () => {
    vi.useFakeTimers();
    try {
      api.suggest.mockReset();
      api.suggest.mockImplementation((q: string) => Promise.resolve(found(q)));
      render(<RecordsPage />);
      const box = screen.getByTestId("records-search");
      fireEvent.change(box, { target: { value: "106093" } });
      await vi.advanceTimersByTimeAsync(200);
      expect(screen.getByTestId("records-found")).toBeTruthy();
      await vi.advanceTimersByTimeAsync(30000);
      expect(screen.getByTestId("records-found")).toBeTruthy();
      expect(screen.queryByText(/taking too long/)).toBeNull();
      // one that never answers
      api.suggest.mockImplementation(() => new Promise(() => undefined));
      fireEvent.change(box, { target: { value: "never" } });
      await vi.advanceTimersByTimeAsync(9000);
      expect(screen.getByTestId("records-found").className).toContain("opacity-60");   // the earlier answer, dimmed
      await vi.advanceTimersByTimeAsync(12000);
      expect(screen.getByText(/taking too long/)).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it("Enter with nothing found goes nowhere", async () => {
    api.suggest.mockImplementation((q: string) => Promise.resolve({ query: q, source: "business", took_ms: 1, suggestions: [] } satisfies SuggestResponse));
    render(<RecordsPage />);
    const box = screen.getByTestId("records-search");
    await userEvent.type(box, "zzz");
    expect(await screen.findByTestId("records-none")).toHaveTextContent("No match for “zzz”. Try a different spelling, or a number.");
    await userEvent.type(box, "{Enter}");
    expect(nav.push).not.toHaveBeenCalled();
  });
});

describe("the Lists tab", () => {
  it("offers a list for every view the server names and opens the one chosen", async () => {
    api.views.mockResolvedValue({ views: [{ view: "batches", title: "Batches" }, { view: "sales_orders", title: "Sales orders" }, { view: "suppliers", title: "Suppliers" }] });
    api.list.mockImplementation((view: string) =>
      Promise.resolve({ view, title: view, total: 0, offset: 0, more: false, next_offset: 0, table: { key: view, title: view, columns: ["Name"], rows: [], total: 0 }, column_keys: ["name"], sort: "name", direction: "asc", searchable: true, filters: [] }),
    );
    nav.params = new URLSearchParams("kind=lists&view=sales_orders");
    render(<RecordsPage />);
    const picker = await screen.findByRole("combobox", { name: "List" });
    expect([...picker.querySelectorAll("option")].map((o) => o.textContent)).toEqual(["Batches", "Sales orders", "Suppliers"]);
    expect(await screen.findByTestId("record-list-sales_orders")).toBeInTheDocument();
    expect(await screen.findByTestId("record-list-empty")).toHaveTextContent("Nothing on record here that you can see.");
    await userEvent.selectOptions(picker, "suppliers");
    expect(nav.replace).toHaveBeenCalledWith("/records?kind=lists&view=suppliers");
    nav.params = new URLSearchParams("");
  });
});

describe("a Records tab before any search", () => {
  it("lists the records of its kind this person opened lately, never another person's", async () => {
    const { setSavedUser, rememberOpened, clearSavedFor } = await import("@/lib/saved");
    setSavedUser("employee:a");
    rememberOpened({ type: "Customer", label: "Soapbox Soaps", href: "/object/Customer/Customer%3As1" });
    rememberOpened({ type: "PurchaseOrder", label: "PO 106001", href: "/object/PurchaseOrder/PurchaseOrder%3Ap1" });
    rememberOpened({ type: "Customer", label: "Soapbox Soaps", href: "/object/Customer/Customer%3As1" }); // once
    nav.params = new URLSearchParams("kind=customers");
    const { unmount } = render(<RecordsPage />);
    const list = await screen.findByTestId("records-recent-list");
    expect([...list.querySelectorAll("a")].map((a) => a.textContent?.replace(/\d.*$/, ""))).toEqual(["Soapbox Soaps"]);
    unmount();
    // the next person on this browser sees none of it; sign-out deletes it
    setSavedUser("employee:b");
    const { unmount: u2 } = render(<RecordsPage />);
    expect(screen.queryByTestId("records-recent-list")).toBeNull();
    u2();
    clearSavedFor("employee:a");
    setSavedUser("employee:a");
    render(<RecordsPage />);
    expect(screen.queryByTestId("records-recent-list")).toBeNull();
    nav.params = new URLSearchParams("");
  });
});
