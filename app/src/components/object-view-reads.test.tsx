import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ObjectView } from "./object-view";

vi.mock("next/navigation", () => ({
  useParams: () => ({}),
  useSearchParams: () => null,
  usePathname: () => "/",
  useRouter: () => ({ push: vi.fn() }),
}));
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const page = (id: string, extra: Record<string, unknown> = {}) => ({
  object_id: `PurchaseOrder:${id}`, type: "PurchaseOrder", label: `PO ${id}`, summary: "", status: { state: "read", text: "Read", sources: [] },
  freshness: {}, complete: true, ...extra,
});
const objectReads = (f: { mock: { calls: unknown[][] } }) => f.mock.calls.filter((c) => String(c[0]).startsWith("/api/object/")).length;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("record page reads", () => {
  it("a record still reading after a minute says so instead of promising a minute", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
    render(<ObjectView type="PurchaseOrder" id="A" />);
    await act(async () => { vi.advanceTimersByTime(25_000); });
    expect(screen.getByTestId("record-slow")).toHaveTextContent("take up to a minute");
    await act(async () => { vi.advanceTimersByTime(40_000); });
    expect(screen.getByTestId("record-slow")).toHaveTextContent("Taking longer than usual. Still reading.");
  });

  it("a Retry that is still running when you move to another record cannot overwrite it (WEB-038)", async () => {
    // A fails once, then its retry hangs; B answers first; A's late answer arrives after
    let calls: Record<string, number> = {};
    const late: Record<string, (r: Response) => void> = {};
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      const id = String(url).match(/PurchaseOrder\/(\w+)/)![1];
      calls[id] = (calls[id] ?? 0) + 1;
      if (id === "A" && calls[id] === 1) return Promise.resolve(json({ error: { code: "internal" } }, 500));
      if (id === "A") return new Promise<Response>((resolve) => (late.A = resolve)); // ignores abort, like a slow network
      return Promise.resolve(json(page(id)));
    }));
    const { rerender } = render(<ObjectView type="PurchaseOrder" id="A" />);
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    await waitFor(() => expect(calls.A).toBe(2));
    rerender(<ObjectView type="PurchaseOrder" id="B" />);
    expect(await screen.findByTestId("object-title")).toHaveTextContent("PO B");
    await act(async () => late.A(json(page("A"))));
    expect(screen.getByTestId("object-title")).toHaveTextContent("PO B");
    calls = {};
  });

  it("a page served stale is read again by itself and replaced when the fresh page lands (WEB-032)", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let n = 0;
    const f = vi.fn(async () => json(++n === 1 ? page("A", { stale_since: "2026-11-12T10:00:00Z", label: "PO A (old)" }) : page("A", { label: "PO A (fresh)" })));
    vi.stubGlobal("fetch", f);
    render(<ObjectView type="PurchaseOrder" id="A" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByTestId("stale-page")).toBeInTheDocument();
    expect(screen.getByTestId("object-title")).toHaveTextContent("old");
    await act(async () => { await vi.advanceTimersByTimeAsync(6100); });
    expect(objectReads(f)).toBe(2);
    expect(screen.getByTestId("object-title")).toHaveTextContent("fresh");
    expect(screen.queryByTestId("stale-page")).toBeNull();
    // fresh: no more reads
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(objectReads(f)).toBe(2);
  });

  it("same-named customers no identity link joins are each a choice, never one margin (API-018)", async () => {
    const choices = [
      { object_id: 'Customer:bc.customers:{"id":"b1"}', type: "Customer", label: "Northwind (C-50)", open_url: "/object/Customer/b1" },
      { object_id: 'Customer:bc.customers:{"id":"b2"}', type: "Customer", label: "Northwind (C-53)", open_url: "/object/Customer/b2" },
    ];
    vi.stubGlobal("fetch", vi.fn(async () => json({
      ...page("A"), object_id: "Customer:portal.app_customer:c1", type: "Customer", label: "Northwind",
      profitability: null, profitability_choices: choices,
    })));
    render(<ObjectView type="Customer" id="Customer:portal.app_customer:c1" />);
    const box = await screen.findByTestId("margin-choices");
    expect(box).toHaveTextContent("not added together");
    expect(screen.getByRole("link", { name: "Northwind (C-50)" })).toHaveAttribute("href", "/object/Customer/b1");
    expect(screen.getByRole("link", { name: "Northwind (C-53)" })).toHaveAttribute("href", "/object/Customer/b2");
  });

  it("a stale page whose refresh read fails stays on screen", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let n = 0;
    vi.stubGlobal("fetch", vi.fn(async () => (++n === 1 ? json(page("A", { stale_since: "2026-11-12T10:00:00Z" })) : json({ error: { code: "internal" } }, 500))));
    render(<ObjectView type="PurchaseOrder" id="A" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(6100); });
    expect(screen.getByTestId("object-title")).toHaveTextContent("PO A");
    expect(screen.getByTestId("stale-page")).toBeInTheDocument();
  });
});
