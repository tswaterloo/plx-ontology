import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// the sidebar's own reads (late orders by supplier) on a page that is not Home start only
// once that page's reads are answered, never beside them on the server
const nav = vi.hoisted(() => ({ path: "/records" }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.path, useRouter: () => ({ push: () => undefined }), useSearchParams: () => new URLSearchParams() }));
const auth = vi.hoisted(() => ({ status: "signed-in" }));
vi.mock("./auth", () => ({ useAuth: () => ({ state: { status: auth.status } }), RequireAuth: ({ children }: { children: unknown }) => children }));

import { getMe } from "@/lib/api";
import { resetSessionCaches } from "@/lib/session-cache";
import { LateBySupplier } from "./shell";

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}
const side = { late_total: 1, supplier_total: 1, partial: false, late_suppliers: [{ supplier: "Acme", late: 1 }], recent_questions: [] };
const tick = () => new Promise((r) => setTimeout(r, 5));

describe("the sidebar's reads yield to the page's", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    resetSessionCaches();   // nothing held from the last test
  });

  it("on another page its numbers are read only after the page's own read is answered", async () => {
    nav.path = "/records";
    const asked: string[] = [];
    let answerPage: (r: Response) => void = () => undefined;
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      asked.push(String(url));
      if (String(url).startsWith("/api/me")) return new Promise<Response>((r) => (answerPage = r));
      return Promise.resolve(json(side));
    }));
    // before the person is known nothing is read (the sign-in check leaves a moment with
    // no request in flight, before the page's own reads start)
    auth.status = "loading";
    const view = render(<LateBySupplier />);
    await tick();
    await tick();
    expect(asked).toEqual([]);
    view.unmount();
    auth.status = "signed-in";
    const page = getMe();   // the page's read, sent in the same render
    render(<LateBySupplier />);
    await tick();
    await tick();
    expect(asked.filter((u) => u.startsWith("/api/sidebar"))).toEqual([]);
    answerPage(json({ principal: "p" }));
    await page;
    await vi.waitFor(() => expect(asked.some((u) => u.startsWith("/api/sidebar"))).toBe(true));
    // one small read: never Home's whole read nor the late list
    expect(asked.filter((u) => u.startsWith("/api/home") || u.startsWith("/api/late"))).toEqual([]);
  });

  it("on Home it waits for Home's own reads too", async () => {
    nav.path = "/";
    const asked: string[] = [];
    let answerPage: (r: Response) => void = () => undefined;
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      asked.push(String(url));
      if (String(url).startsWith("/api/me")) return new Promise<Response>((r) => (answerPage = r));
      return Promise.resolve(json(side));
    }));
    const page = getMe();   // Home's read still open
    render(<LateBySupplier />);
    await tick();
    await tick();
    expect(asked.some((u) => u.startsWith("/api/sidebar"))).toBe(false);
    answerPage(json({ principal: "p" }));
    await page;
    await vi.waitFor(() => expect(asked.some((u) => u.startsWith("/api/sidebar"))).toBe(true));
  });
});
