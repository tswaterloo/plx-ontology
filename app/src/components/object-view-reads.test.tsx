import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ObjectView } from "./object-view";

const nav = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  useParams: () => ({}),
  useSearchParams: () => null,
  usePathname: () => "/",
  useRouter: () => ({ push: vi.fn(), replace: nav.replace }),
}));
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const page = (id: string, extra: Record<string, unknown> = {}) => ({
  object_id: `PurchaseOrder:${id}`, type: "PurchaseOrder", label: `PO ${id}`, summary: "", status: { state: "read", text: "Read", sources: [] },
  freshness: {}, complete: true, ...extra,
});
const objectReads = (f: { mock: { calls: unknown[][] } }) => f.mock.calls.filter((c) => String(c[0]).startsWith("/api/object/") && !String(c[0]).includes("core=1")).length;
const isCore = (url: string) => String(url).includes("core=1");
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("record page reads", () => {
  it("the record's own details show first; the walk fills in when the quick read lands, sections kept (C1)", async () => {
    let quick: (r: Response) => void = () => {};
    const core = { ...page("A", { complete: false, core: true }), object_id: "Customer:c1", type: "Customer", label: "Northwind",
      status: { state: "reading", text: "", sources: [] }, facts: [{ text: "City: Montreal", sources: [{ system: "BC", label: "row" }] }] };
    const f = vi.fn((url: string) => {
      if (isCore(url)) return Promise.resolve(json(core));
      const m = String(url).match(/object-section\/(\w+)\//);
      if (m) return Promise.resolve(json(m[1] === "people"
        ? { object_id: "Customer:c1", section: "people", people: [{ name: "Ana Diaz", role: "buyer", sources: [] }] }
        : { object_id: "Customer:c1", section: m[1], applies: false }));
      if (String(url).startsWith("/api/object/")) return new Promise<Response>((resolve) => (quick = resolve));
      return Promise.resolve(json({ error: { code: "not_found" } }, 404));
    });
    vi.stubGlobal("fetch", f);
    render(<ObjectView type="Customer" id="Customer:c1" />);
    expect(await screen.findByTestId("object-title")).toHaveTextContent("Northwind");
    // the walk's parts are read, never said absent; no status badge while it is read
    expect(screen.getByTestId("walk-loading-timeline")).toBeInTheDocument();
    expect(screen.getByTestId("walk-loading-links")).toBeInTheDocument();
    expect(screen.getByTestId("walk-loading-orders")).toBeInTheDocument();
    expect(screen.queryByTestId("object-status")).toBeNull();
    expect(screen.getByTestId("checking")).toHaveTextContent("its status, history and links");
    // the sections started on the core read, before the quick read landed
    await waitFor(() => expect(f.mock.calls.some((c) => String(c[0]).includes("/object-section/people/"))).toBe(true));
    await waitFor(() => expect(screen.getByText("Ana Diaz")).toBeInTheDocument());
    await act(async () => quick(json({ ...core, core: undefined, status: { state: "open", text: "Open", sources: [] }, label: "Northwind Ltd" })));
    await waitFor(() => expect(screen.getByTestId("object-title")).toHaveTextContent("Northwind Ltd"));
    expect(screen.queryByTestId("walk-loading-timeline")).toBeNull();
    expect(screen.getByText("Ana Diaz")).toBeInTheDocument();   // merged again into the quick read
  });

  it("a PO number two suppliers' orders share lists both to choose from; one alone opens at once", async () => {
    const choices = [
      { object_id: "PurchaseOrder:bc:1", type: "PurchaseOrder", label: "PO7770001 · Supplier One (BC, open)", open_url: "/order?kind=purchase_order&h=a" },
      { object_id: "PurchaseOrder:portal:2", type: "PurchaseOrder", label: "PO7770001 · Supplier Two (FileMaker, Portal, closed)", open_url: "/order?kind=purchase_order&h=b" },
    ];
    const choose = (list: typeof choices) => ({ ...page("x"), object_id: "PO7770001", label: "PO7770001", summary: "2 records have the number PO7770001.", status: { state: "choose", text: "", sources: [] }, choices: list });
    vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(isCore(url) ? json({ error: { code: "not_found" } }, 404) : json(choose(choices)))));
    const { unmount } = render(<ObjectView type="PurchaseOrder" id="PO7770001" />);
    const list = await screen.findByTestId("record-choices");
    expect(list).toHaveTextContent("Supplier One (BC, open)");
    expect(list).toHaveTextContent("Supplier Two");
    expect(screen.getByRole("link", { name: /Supplier Two/ })).toHaveAttribute("href", "/order?kind=purchase_order&h=b");
    expect(screen.queryByTestId("object-title")).toBeNull();
    expect(nav.replace).not.toHaveBeenCalled();
    unmount();
    vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(isCore(url) ? json({ error: { code: "not_found" } }, 404) : json(choose(choices.slice(1))))));
    render(<ObjectView type="PurchaseOrder" id="PO7770001" />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/order?kind=purchase_order&h=b"));
  });

  it("a core read that lands after the quick read never replaces it", async () => {
    let late: (r: Response) => void = () => {};
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      if (isCore(url)) return new Promise<Response>((resolve) => (late = resolve));
      return Promise.resolve(json(page("A", { label: "PO A (walked)" })));
    }));
    render(<ObjectView type="PurchaseOrder" id="A" />);
    expect(await screen.findByTestId("object-title")).toHaveTextContent("walked");
    await act(async () => late(json(page("A", { core: true, complete: false, label: "PO A (core)" }))));
    expect(screen.getByTestId("object-title")).toHaveTextContent("walked");
    expect(screen.queryByTestId("checking")).toBeNull();
  });


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
      if (isCore(url)) return Promise.resolve(json({ error: { code: "core_unread" } }, 503));
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

  it("a page served stale sends no read on a timer; it is read again when the window is returned to and replaced when the fresh page lands (WEB-032)", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let n = 0;
    const f = vi.fn(async (url: string) => isCore(url) ? json({ error: { code: "core_unread" } }, 503) : json(++n === 1 ? page("A", { stale_since: "2026-11-12T10:00:00Z", label: "PO A (old)" }) : page("A", { label: "PO A (fresh)" })));
    vi.stubGlobal("fetch", f);
    render(<ObjectView type="PurchaseOrder" id="A" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByTestId("stale-page")).toBeInTheDocument();
    expect(screen.getByTestId("object-title")).toHaveTextContent("old");
    expect(screen.getByTestId("stale-page")).toHaveTextContent("come back to this window");
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(objectReads(f)).toBe(1);   // no timed re-read
    await act(async () => { window.dispatchEvent(new Event("focus")); await vi.advanceTimersByTimeAsync(0); });
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

  it("a product's formula section lists each sheet's lines, a part linked to its raw material", async () => {
    const formula = {
      sheets: [{
        document: { object_id: "SharePointFile:f1", type: "SharePointFile", label: "F-26-0001 Calm Lotion.pdf", open_url: "/object/SharePointFile/f1" },
        formula_code: "F-26-0001", dated: "2026-10-02", basis: "code", status: "partial", share_total: "99.5", missing_lines: 1,
        material_cost: { per_kg: "1.6000", currency: "CAD", priced_share: "80.0", missing: ["R-9999: no purchase price you can read"], complete: false },
        components: [
          { no: "1", part: "R-0101", description: "Water", share_percent: "80.0", item: { object_id: "Item:r1", type: "Item", label: "Water", open_url: "/object/Item/r1" }, supplier_items: 1, last_price: { price: "2.00", currency: "CAD", uom: "kg", dated: "2026-10-01", po: { object_id: "PurchaseOrder:p9", type: "PurchaseOrder", label: "PO 9001", open_url: "/object/PurchaseOrder/p9" } } },
          { no: "2", part: "R-9999", description: "Unknown oil", share_percent: "19.5", check: "share x size does not match", supplier_items: 0 },
        ],
      }],
      unresolved_parts: 1, withheld: 2, other_products: 0, portal_lines_served: false, complete: false,
    };
    const f = vi.fn(async (url: string) => {
      const m = String(url).match(/object-section\/(\w+)\//);
      if (m) return json(m[1] === "formula" ? { object_id: "Product:p1", section: "formula", formula } : { object_id: "Product:p1", section: m[1], applies: false });
      return json({ ...page("A"), object_id: "Product:p1", type: "Product", label: "Calm Lotion", complete: false });
    });
    vi.stubGlobal("fetch", f);
    render(<ObjectView type="Product" id="Product:p1" />);
    const box = await screen.findByTestId("formula");
    expect(f.mock.calls.some((c) => String(c[0]).includes("/api/object-section/formula/Product/"))).toBe(true);
    expect(screen.getByRole("link", { name: "R-0101" })).toHaveAttribute("href", "/object/Item/r1");
    expect(screen.queryByRole("link", { name: "R-9999" })).toBeNull();
    expect(box).toHaveTextContent("1 line unreadable");
    expect(box).toHaveTextContent("share x size does not match");
    expect(box).toHaveTextContent("1 part code name no record");
    expect(box).toHaveTextContent("2 lines or sheets you can't open");
    expect(box).toHaveTextContent("99.5");
    expect(screen.getByRole("link", { name: "2.00 CAD / kg" })).toHaveAttribute("href", "/object/PurchaseOrder/p9");
    const cost = screen.getByTestId("material-cost");
    expect(cost).toHaveTextContent("Materials 1.6000 CAD per kg at the last PO prices · 80.0% of the formula priced");
    expect(cost).toHaveTextContent("R-9999: no purchase price you can read");
  });

  it("a purchase order never reads a formula section", async () => {
    const f = vi.fn(async (url: string) => {
      const m = String(url).match(/object-section\/(\w+)\//);
      if (m) return json({ object_id: "PurchaseOrder:A", section: m[1], applies: false });
      return json(page("A", { complete: false }));
    });
    vi.stubGlobal("fetch", f);
    render(<ObjectView type="PurchaseOrder" id="A" />);
    await screen.findByTestId("object-title");
    await waitFor(() => expect(f.mock.calls.some((c) => String(c[0]).includes("/object-section/late/"))).toBe(true));
    expect(f.mock.calls.some((c) => String(c[0]).includes("/object-section/formula/"))).toBe(false);
  });

  it("a stale page whose refresh read fails stays on screen", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let n = 0;
    vi.stubGlobal("fetch", vi.fn(async () => (++n === 1 ? json(page("A", { stale_since: "2026-11-12T10:00:00Z" })) : json({ error: { code: "internal" } }, 500))));
    render(<ObjectView type="PurchaseOrder" id="A" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { window.dispatchEvent(new Event("focus")); await vi.advanceTimersByTimeAsync(6100); });
    expect(screen.getByTestId("object-title")).toHaveTextContent("PO A");
    expect(screen.getByTestId("stale-page")).toBeInTheDocument();
  });
});
