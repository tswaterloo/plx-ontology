import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ homes: [] as unknown[], homeCalls: 0, total: 0, lateCalls: [] as number[] }));
vi.mock("next/navigation", () => ({ usePathname: () => "/", useRouter: () => ({ push: () => undefined }), useSearchParams: () => new URLSearchParams() }));
vi.mock("./auth", () => ({ useAuth: () => ({ state: {} }), RequireAuth: ({ children }: { children: unknown }) => children }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getHome: () => Promise.resolve(api.homes[Math.min(api.homeCalls++, api.homes.length - 1)]),
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

import { LateBySupplier, loadHome, loadLateRead, onHomeRead } from "./shell";

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

  it("501 late orders: Today reads 300 and says so, with the count the server holds", async () => {
    api.total = 501;
    const read = await loadLateRead(true);
    expect(read.items).toHaveLength(300);
    expect(read.total).toBe(501);
    render(<LateBySupplier />);
    const note = await screen.findByTestId("late-by-supplier-partial");
    expect(note).toHaveTextContent("first 300 of 501");
    expect(note).toHaveAttribute("href", "/late");
  });

  it("a short list is not labelled partial", async () => {
    api.total = 40;
    await loadLateRead(true);
    render(<LateBySupplier />);
    await waitFor(() => expect(screen.getByTestId("late-by-supplier")).toBeInTheDocument());
    expect(screen.queryByTestId("late-by-supplier-partial")).toBeNull();
  });
});
