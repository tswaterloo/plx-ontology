import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RecordsWorkspace } from "./records-workspace";

const nav = vi.hoisted(() => ({
  params: new URLSearchParams(),
  replace: vi.fn(),
  push: vi.fn(),
}));

const api = vi.hoisted(() => ({
  suggest: vi.fn(),
  getRecordViews: vi.fn(),
}));

const listCalls = vi.hoisted(() => [] as Array<{ view: string; initialSort?: unknown; externalQuery?: string }>);

vi.mock("next/navigation", () => ({
  usePathname: () => "/records",
  useSearchParams: () => nav.params,
  useRouter: () => ({ replace: nav.replace, push: nav.push }),
}));

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  suggest: api.suggest,
  getRecordViews: api.getRecordViews,
}));

vi.mock("@/components/record-list", () => ({
  RecordList: ({ view, initialSort, externalQuery }: { view: string; initialSort?: unknown; externalQuery?: string }) => {
    listCalls.push({ view, initialSort, externalQuery });
    return (
      <div
        data-testid="mock-record-list"
        data-view={view}
        data-sort={initialSort ? JSON.stringify(initialSort) : ""}
        data-query={externalQuery ?? ""}
      />
    );
  },
}));

beforeEach(() => {
  nav.params = new URLSearchParams();
  nav.replace.mockReset();
  nav.push.mockReset();
  api.suggest.mockReset();
  api.getRecordViews.mockReset();
  listCalls.length = 0;
  window.localStorage.clear();
});

describe("RecordsWorkspace", () => {
  it("opens directly into live Orders with newest-first default (NOT days_late desc) and switches tabs cleanly", () => {
    render(<RecordsWorkspace />);
    expect(screen.queryByTestId("records-browse")).toBeNull();
    expect(screen.getByRole("tab", { name: "Orders" })).toHaveAttribute("aria-selected", "true");
    const list = screen.getByTestId("mock-record-list");
    expect(list).toHaveAttribute("data-view", "purchase_orders");
    expect(list).toHaveAttribute("data-sort", "");
    expect(listCalls[0]?.initialSort).toBeUndefined();
    fireEvent.click(screen.getByRole("tab", { name: "Customers" }));
    expect(nav.replace).toHaveBeenCalledWith("/records?kind=customers");
  });

  it("hides the Recently opened strip when empty and shows it compactly when non-empty", async () => {
    const { setSavedUser, rememberOpened, clearSavedFor } = await import("@/lib/saved");
    clearSavedFor("employee:records-test");
    setSavedUser("employee:records-test");

    const { unmount } = render(<RecordsWorkspace />);
    expect(screen.queryByTestId("records-recent-saved-strip")).toBeNull();
    unmount();

    rememberOpened({ type: "PurchaseOrder", label: "PO 106093", href: "/order?kind=purchase_order&h=po-1" });
    render(<RecordsWorkspace />);
    expect(screen.getByTestId("records-recent-saved-strip")).toBeInTheDocument();
    expect(screen.getByTestId("records-recent-list")).toHaveTextContent("PO 106093");
    clearSavedFor("employee:records-test");
  });

  it("clears search input with Escape or the clear button", async () => {
    api.suggest.mockResolvedValue({ query: "106093", suggestions: [] });
    render(<RecordsWorkspace />);
    const input = screen.getByTestId("records-search") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "106093" } });
    expect(input.value).toBe("106093");
    fireEvent.click(screen.getByTestId("records-search-clear"));
    expect(input.value).toBe("");
    fireEvent.change(input, { target: { value: "PO" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(input.value).toBe("");
  });

  it("offers Purchase orders and Sales orders toggle on the Orders tab", () => {
    nav.params = new URLSearchParams("kind=orders");
    render(<RecordsWorkspace />);
    expect(screen.getByTestId("mock-record-list")).toHaveAttribute("data-view", "purchase_orders");
    fireEvent.click(screen.getByRole("button", { name: "Sales orders" }));
    expect(nav.replace).toHaveBeenCalledWith("/records?kind=orders&list=sales_orders");
  });
});
