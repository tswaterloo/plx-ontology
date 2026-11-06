import { describe, expect, it } from "vitest";
import type { LatePo } from "@/lib/types";
import { matches, readAllLate, sortLate, supplierCounts, type DayBand, type ValueBand, type LatePage } from "./late";

const po = (n: number, extra: Partial<LatePo> = {}): LatePo => ({
  object_id: `PurchaseOrder:${n}`, label: `PO ${n}`, reason: "late", open_url: `/object/PurchaseOrder/${n}`, sources: [], ...extra,
});
const ROWS: LatePo[] = [
  po(1, { supplier: "DSM", days_late: 130, due_date: "2026-06-24", amount: { amount: "1993.00", currency: "CAD" } }),
  po(2, { supplier: "Acme", days_late: 30, due_date: "2026-10-01", amount: { amount: "1000.00", currency: "USD" } }),
  po(3, { supplier: "DSM", days_late: 31, amount: { amount: "999999.00", currency: "CAD" }, amount_suspect: "10x its history" }),
  po(4, { days_late: 91, amount: { amount: "50000.00", currency: "CAD" } }),
  po(5, { supplier: "Acme", days_late: 5 }),
];
const ids = (xs: LatePo[]) => xs.map((p) => p.object_id.split(":")[1]).join(",");
const all = { supplier: null as string | null, days: "all" as DayBand, value: "all" as ValueBand };

describe("late list filters and sort", () => {
  it("bands days and values at their edges; a suspect or unshown total is never banded as a value", () => {
    const pick = (f: Partial<typeof all>) => ids(ROWS.filter((p) => matches(p, { ...all, ...f })));
    expect(pick({ days: "d30" })).toBe("2,5");
    expect(pick({ days: "d90" })).toBe("3");
    expect(pick({ days: "dmore" })).toBe("1,4");
    expect(pick({ value: "v1k" })).toBe("");
    expect(pick({ value: "v10k" })).toBe("1,2"); // 1,000.00 is in 1,000 to 10,000
    expect(pick({ value: "vmore" })).toBe(""); // the suspect 999,999 is not a fact
    expect(pick({ value: "none" })).toBe("3,5");
    expect(pick({ supplier: "DSM", days: "dmore" })).toBe("1");
    expect(pick({ supplier: "" })).toBe("4"); // no supplier shown
    expect(pick({ days: "bogus" as DayBand })).toBe(""); // an unknown band shows nothing
    expect(supplierCounts(ROWS)).toEqual([["Acme", 2], ["DSM", 2], ["", 1]]);
  });

  it("sorts by days, value within each currency, supplier and due date, missing values last", () => {
    expect(ids(sortLate(ROWS, "days"))).toBe("1,4,3,2,5");
    expect(ids(sortLate(ROWS, "value"))).toBe("4,1,2,3,5");
    expect(ids(sortLate(ROWS, "value", true))).toBe("1,4,2,3,5");
    expect(ids(sortLate(ROWS, "supplier"))).toBe("2,5,1,3,4");
    expect(ids(sortLate(ROWS, "due"))).toBe("1,2,3,4,5");
    expect(ids(sortLate(ROWS, "po", true))).toBe("5,4,3,2,1");
    expect(ROWS.map((p) => p.object_id)).toEqual(["1", "2", "3", "4", "5"].map((n) => `PurchaseOrder:${n}`)); // never sorted in place
  });
});

describe("readAllLate", () => {
  const po = (n: number) => ({ object_id: `PurchaseOrder:t:${n}` }) as LatePo;
  // a made-up list of `total` orders, `size` a page, as /api/late pages it
  function server(total: number) {
    const asked: number[] = [];
    let open = 0;
    let most = 0;
    const get = async (offset: number, size: number): Promise<LatePage> => {
      asked.push(offset);
      open += 1;
      most = Math.max(most, open);
      await new Promise((r) => setTimeout(r, 1));
      open -= 1;
      const items = Array.from({ length: Math.max(0, Math.min(size, total - offset)) }, (_, i) => po(offset + i));
      const next = offset + items.length;
      return { items, total, more: next < total, next_offset: next, as_of: `cut-${offset}` };
    };
    return { get, asked, peak: () => most };
  }

  it("reads every page after the first at once, in order, each order once", async () => {
    const s = server(250);
    const got = await readAllLate(s.get, 100, 1000);
    expect(got.items.map((p) => p.object_id)).toEqual(Array.from({ length: 250 }, (_, i) => `PurchaseOrder:t:${i}`));
    expect(got.total).toBe(250);
    expect(got.asOf).toBe("cut-200");
    expect(s.asked).toEqual([0, 100, 200]);
    expect(s.peak()).toBe(2);
  });

  it("stops at the cap and at a single page", async () => {
    const big = server(5000);
    const got = await readAllLate(big.get, 100, 1000);
    expect(got.items).toHaveLength(1000);
    expect(got.total).toBe(5000);
    expect(big.asked).toHaveLength(10);
    const one = server(40);
    expect((await readAllLate(one.get, 100, 1000)).items).toHaveLength(40);
    expect(one.asked).toEqual([0]);
  });
});
