import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ path: "/records" }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.path, useRouter: () => ({ push: () => undefined }), useSearchParams: () => new URLSearchParams() }));
const auth = vi.hoisted(() => ({ status: "loading" as "loading" | "signed-in" | "signed-out" }));
vi.mock("./auth", () => ({ useAuth: () => ({ state: { status: auth.status } }), RequireAuth: ({ children }: { children: unknown }) => children }));

import { resetSessionCaches } from "@/lib/session-cache";
import { LateBySupplier } from "./shell";

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}
const side = { late_total: 1, supplier_total: 1, partial: false, late_suppliers: [{ supplier: "Acme", late: 1 }], recent_questions: [] };

describe("the sidebar's read waits until auth is signed-in (no 401 race)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    resetSessionCaches();
    auth.status = "loading";
  });

  for (const status of ["loading", "signed-out"] as const) {
    it(`does not call /api/sidebar while auth is ${status}`, async () => {
      auth.status = status;
      const asked: string[] = [];
      vi.stubGlobal("fetch", vi.fn((url: string) => {
        asked.push(String(url));
        return Promise.resolve(json(side));
      }));
      render(<LateBySupplier />);
      await new Promise((r) => setTimeout(r, 20));
      expect(asked.filter((u) => u.startsWith("/api/sidebar"))).toHaveLength(0);
    });
  }

  for (const path of ["/records", "/"]) {
    it(`on ${path} calls /api/sidebar once auth resolves to signed-in`, async () => {
      nav.path = path;
      auth.status = "signed-in";
      const asked: string[] = [];
      vi.stubGlobal("fetch", vi.fn((url: string) => {
        asked.push(String(url));
        return Promise.resolve(json(side));
      }));
      render(<LateBySupplier />);
      await vi.waitFor(() => expect(asked.filter((u) => u.startsWith("/api/sidebar"))).toHaveLength(1));
      // one small read: never Home's whole read nor the late list
      expect(asked.filter((u) => u.startsWith("/api/home") || u.startsWith("/api/late"))).toEqual([]);
    });
  }
});
