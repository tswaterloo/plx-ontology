import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SuggestResponse } from "@/lib/types";

const nav = vi.hoisted(() => ({ params: new URLSearchParams(""), push: vi.fn(), replace: vi.fn() }));
const api = vi.hoisted(() => ({ suggest: vi.fn() }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => nav.params,
  useRouter: () => ({ push: nav.push, replace: nav.replace }),
  usePathname: () => "/records",
}));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), suggest: api.suggest }));

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

  it("Enter with nothing found goes nowhere", async () => {
    api.suggest.mockImplementation((q: string) => Promise.resolve({ query: q, source: "business", took_ms: 1, suggestions: [] } satisfies SuggestResponse));
    render(<RecordsPage />);
    const box = screen.getByTestId("records-search");
    await userEvent.type(box, "zzz");
    await screen.findByTestId("records-none");
    await userEvent.type(box, "{Enter}");
    expect(nav.push).not.toHaveBeenCalled();
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
