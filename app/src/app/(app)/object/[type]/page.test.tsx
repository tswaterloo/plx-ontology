import { render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ segment: "", redirect: vi.fn(), notFound: vi.fn() }));
vi.mock("next/navigation", () => ({
  useParams: () => ({ type: nav.segment }),
  redirect: (to: string) => { nav.redirect(to); throw new Error("NEXT_REDIRECT"); },
  notFound: () => { nav.notFound(); throw new Error("NEXT_NOT_FOUND"); },
}));

import Page from "./page";

function open(segment: string) {
  nav.segment = segment;
  try { render(<Page />); } catch { /* redirect and notFound end the render */ }
}

describe("a record address without its type", () => {
  beforeEach(() => { nav.redirect.mockClear(); nav.notFound.mockClear(); });

  it("opens the record's page, the type read from the id's own prefix", () => {
    const id = 'SalesOrder:portal.app_sales_order:{"id":"s1"}';
    open(encodeURIComponent(id).replace(/%3A/g, ":"));
    expect(nav.redirect).toHaveBeenCalledWith(`/object/SalesOrder/${encodeURIComponent(id)}`);
    expect(nav.notFound).not.toHaveBeenCalled();
  });

  it("is Page not found for an address that names no record", () => {
    for (const bad of ["inbox", "", "bad type:x", encodeURIComponent("../x:1")]) {
      nav.notFound.mockClear();
      open(bad);
      expect(nav.notFound).toHaveBeenCalled();
    }
    expect(nav.redirect).not.toHaveBeenCalled();
  });
});
