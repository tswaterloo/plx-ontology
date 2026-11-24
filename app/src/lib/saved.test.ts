import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({ logSaved: () => Promise.resolve() }));

import { togglePin } from "./pins";
import { PINS_MAX, SAVED_MAX, setSavedNotice, setSavedUser, toggleSaved, writePages } from "./saved";
import { renderHook } from "@testing-library/react";
import { useSavedNotice } from "./saved";

const stored = () => JSON.parse(window.localStorage.getItem(Object.keys(window.localStorage).find((k) => k.includes("u1"))!) ?? "[]") as { kind: string; href: string }[];

describe("saved items and pinned pages", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setSavedUser(null);
    setSavedUser("u1");
  });

  it("keeps 500 saved items and 100 pinned pages; a full list refuses the next one and says so, never dropping an older one", () => {
    setSavedNotice(null);
    const said = renderHook(() => useSavedNotice());
    expect([SAVED_MAX, PINS_MAX]).toEqual([500, 100]);
    for (let i = 0; i < SAVED_MAX + 5; i++) toggleSaved({ key: `q:${i}`, kind: "question", label: `Q${i}`, href: `/ask?q=${i}` });
    const kept = stored().filter((s) => s.kind !== "page");
    expect(kept).toHaveLength(SAVED_MAX);
    expect(kept[0].href).toBe(`/ask?q=${SAVED_MAX - 1}`);   // the 501st was refused, the first kept
    expect(kept[kept.length - 1].href).toBe("/ask?q=0");
    said.rerender();
    expect(said.result.current).toContain("500 saved items");
    expect(toggleSaved({ key: "q:0", kind: "question", label: "Q0", href: "/ask?q=0" })).toBe(false);   // removing one still works
    expect(toggleSaved({ key: "q:new", kind: "question", label: "new", href: "/ask?q=new" })).toBe(true);
    said.rerender();
    expect(said.result.current).toBeNull();
    let items = stored() as never[];
    for (let i = 0; i < PINS_MAX + 5; i++) {
      togglePin({ href: `/po/${i}`, label: `PO ${i}` }, items, null);
      items = stored() as never[];
    }
    const pins = stored().filter((s) => s.kind === "page" && s.href !== "/");
    expect(pins).toHaveLength(PINS_MAX);
    expect(pins.map((x) => x.href)).toContain("/po/0");
    expect(pins.map((x) => x.href)).not.toContain(`/po/${PINS_MAX}`);
    said.rerender();
    expect(said.result.current).toContain("100 pinned pages");
    expect(stored().filter((s) => s.kind !== "page")).toHaveLength(SAVED_MAX);   // pins never push out saved items
    expect(writePages([])).toBe(true);
  });
});
