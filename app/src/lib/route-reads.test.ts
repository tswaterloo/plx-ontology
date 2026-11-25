import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const readPath = vi.fn(() => Promise.resolve({}));
vi.mock("./api", () => ({ readPath: (...a: unknown[]) => (readPath as (...x: unknown[]) => unknown)(...a) }));

import { aheadProps, HOVER_MS, readAheadRoute, STORED_ROUTES } from "./route-reads";
import { EARLY_ROUTE_READS } from "./ticket-boot";
import { forgetKeptReads } from "./use-read";

beforeEach(() => {
  vi.useFakeTimers();
  forgetKeptReads();
  readPath.mockClear();
});
afterEach(() => vi.useRealTimers());

describe("the route table", () => {
  it("every stored route is in the one table, and its page reads exactly that URL through useRead", () => {
    const dir = path.join(__dirname, "..", "app", "(app)");
    const lists: Record<string, string> = {
      "/orders": "/api/records/list?view=purchase_orders&q=&offset=0&facets=true&sort=days_late&direction=desc",
      "/customers": "/api/records/list?view=customers&q=&offset=0&facets=true",
      "/suppliers": "/api/records/list?view=suppliers&q=&offset=0&facets=true",
      "/products": "/api/records/list?view=products&q=&offset=0&facets=true",
      "/more": "/api/records/list?view=sales_invoices&q=&offset=0&facets=true",
    };
    for (const route of STORED_ROUTES) {
      const url = EARLY_ROUTE_READS[route];
      expect(url, route).toBeTruthy();
      if (route in lists) {
        // these pages' main read is parameterized: the exact first-read URL is in the table,
        // and RecordList makes the same path with recordListPath before handing it to useRead
        expect(url, route).toBe(lists[route]);
        const file = readFileSync(path.join(__dirname, "..", "components", "record-list.tsx"), "utf8");
        expect(file, route).toContain(`useRead<`);
        expect(file, route).toContain(`recordListPath(view, options)`);
        continue;
      }
      // the page itself, except where its main read lives in a component the page shows
      const where = route === "/meetings" ? path.join(__dirname, "..", "components", "meetings", "upcoming.tsx") : path.join(dir, route.slice(1), "page.tsx");
      const file = readFileSync(where, "utf8");
      expect(file, route).toContain(`useRead<`);
      expect(file, route).toContain(`"${url}"`);
    }
  });
});

describe("read-ahead on a link", () => {
  it("pointer-down reads the page at once; hover reads it after a rest, not on a pass-over", () => {
    const p = aheadProps("/data-health");
    p.onPointerDown();
    expect(readPath).toHaveBeenCalledTimes(1);
    expect(readPath).toHaveBeenCalledWith("/api/trust", expect.anything(), true);
    forgetKeptReads();
    readPath.mockClear();
    p.onPointerEnter();
    vi.advanceTimersByTime(HOVER_MS - 10);
    p.onPointerLeave();
    vi.advanceTimersByTime(1000);
    expect(readPath).not.toHaveBeenCalled(); // passed over
    p.onPointerEnter();
    vi.advanceTimersByTime(HOVER_MS + 1);
    expect(readPath).toHaveBeenCalledTimes(1);
  });

  it("a page that does not draw from the kept reads is never read ahead (no wasted read)", () => {
    for (const route of ["/records", "/work-log", "/object/PurchaseOrder/PO1", "/nothing"]) readAheadRoute(route);
    expect(readPath).not.toHaveBeenCalled();
  });

  it("a link with a query or fragment finds its route", () => {
    readAheadRoute("/ops?x=1#a");
    expect(readPath).toHaveBeenCalledWith("/api/ops/status", expect.anything(), true);
  });
});
