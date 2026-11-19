import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ homes: [] as unknown[], homeCalls: 0, total: 0, lateCalls: [] as number[], sidebar: {} as unknown, sidebarCalls: 0 }));
vi.mock("next/navigation", () => ({ usePathname: () => "/", useRouter: () => ({ push: () => undefined }), useSearchParams: () => new URLSearchParams() }));
vi.mock("./auth", () => ({ useAuth: () => ({ state: { status: "signed-in" } }), RequireAuth: ({ children }: { children: unknown }) => children }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getHome: () => Promise.resolve(api.homes[Math.min(api.homeCalls++, api.homes.length - 1)]),
  getSidebar: () => {
    api.sidebarCalls += 1;
    return Promise.resolve(api.sidebar);
  },
  getLateList: (offset: number, _s: unknown, limit: number) => {
    api.lateCalls.push(offset);
    const n = Math.min(limit, Math.max(0, api.total - offset));
    return Promise.resolve({
      items: Array.from({ length: n }, (_, i) => ({ object_id: `PurchaseOrder:${offset + i}`, label: `PO ${offset + i}`, supplier: "Acme", reason: "late", open_url: "/x", sources: [] })),
      total: api.total,
      more: offset + n < api.total,
      next_offset: offset + n,
      offset,
    });
  },
}));

import { LateBySupplier, loadHome, loadLateRead, loadSidebar, onHomeRead } from "./shell";

describe("Today's shared reads", () => {
  beforeEach(() => {
    api.homeCalls = 0;
    api.lateCalls = [];
  });

  it("a fresh home read reaches every consumer that subscribed, not only the caller", async () => {
    api.homes = [{ stale_since: "2026-11-12T10:00:00Z" }, { stale_since: null }];
    const seen: unknown[] = [];
    const off = onHomeRead((h) => seen.push(h));
    expect((await loadHome()) as unknown).toEqual(api.homes[0]);
    expect(await loadHome()).toEqual(api.homes[0]); // shared: one network read
    expect(api.homeCalls).toBe(1);
    expect(await loadHome(true)).toEqual(api.homes[1]); // asked again: a second network read
    await Promise.resolve();
    expect(api.homeCalls).toBe(2);
    expect(seen).toEqual(api.homes);
    off();
  });

  it("Today still reads up to 300 late orders, with the count the server holds", async () => {
    api.total = 501;
    const read = await loadLateRead(true);
    expect(read.items).toHaveLength(300);
    expect(read.total).toBe(501);
  });
});

describe("the sidebar's late suppliers", () => {
  beforeEach(() => {
    api.lateCalls = [];
    api.sidebarCalls = 0;
  });

  it("come from the sidebar's own small read, never the late list: 7 named, the rest counted", async () => {
    api.sidebar = {
      late_total: 501,
      supplier_total: 12,
      partial: false,
      late_suppliers: Array.from({ length: 8 }, (_, i) => ({ supplier: `S${i}`, late: 40 - i })),
      recent_questions: [],
    };
    await loadSidebar(true);
    render(<LateBySupplier />);
    await waitFor(() => expect(screen.getByTestId("late-by-supplier")).toBeInTheDocument());
    expect(screen.getAllByRole("link").map((a) => a.textContent)).toEqual([
      ...Array.from({ length: 7 }, (_, i) => `S${i}${40 - i}`),
      "5 more",
    ]);
    expect(screen.queryByTestId("late-by-supplier-partial")).toBeNull();
    expect(api.lateCalls).toEqual([]);
    expect(api.sidebarCalls).toBe(1);
  });

  it("counts of the longest late only say so; none late shows nothing", async () => {
    api.sidebar = { late_total: 3, supplier_total: 1, partial: true, late_suppliers: [{ supplier: "Acme", late: 3 }], recent_questions: [] };
    await loadSidebar(true);
    const view = render(<LateBySupplier />);
    const note = await screen.findByTestId("late-by-supplier-partial");
    expect(note).toHaveAttribute("href", "/?show=late");
    view.unmount();
    api.sidebar = { late_total: 0, supplier_total: 0, partial: false, late_suppliers: [], recent_questions: [] };
    await loadSidebar(true);
    render(<LateBySupplier />);
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByTestId("late-by-supplier")).toBeNull();
  });
});
