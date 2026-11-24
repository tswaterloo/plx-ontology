import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// W4: the sidebar's small read starts with the page's own reads (one Promise.all at mount),
// never after the sign-in check or after the page's read has ended
const nav = vi.hoisted(() => ({ path: "/records" }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.path, useRouter: () => ({ push: () => undefined }), useSearchParams: () => new URLSearchParams() }));
const auth = vi.hoisted(() => ({ status: "loading" }));
vi.mock("./auth", () => ({ useAuth: () => ({ state: { status: auth.status } }), RequireAuth: ({ children }: { children: unknown }) => children }));

import { getMe } from "@/lib/api";
import { resetSessionCaches } from "@/lib/session-cache";
import { LateBySupplier } from "./shell";

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}
const side = { late_total: 1, supplier_total: 1, partial: false, late_suppliers: [{ supplier: "Acme", late: 1 }], recent_questions: [] };

describe("the sidebar's read starts with the page's own", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    resetSessionCaches();
  });

  for (const path of ["/records", "/"]) {
    it(`on ${path} it is sent at mount, while the page's read and the sign-in check are still open`, async () => {
      nav.path = path;
      const asked: string[] = [];
      vi.stubGlobal("fetch", vi.fn((url: string) => {
        asked.push(String(url));
        if (String(url).startsWith("/api/me")) return new Promise<Response>(() => undefined);   // never answered
        return Promise.resolve(json(side));
      }));
      void getMe().catch(() => undefined);   // the page's read, still open
      render(<LateBySupplier />);
      await vi.waitFor(() => expect(asked.filter((u) => u.startsWith("/api/sidebar"))).toHaveLength(1));
      // one small read: never Home's whole read nor the late list
      expect(asked.filter((u) => u.startsWith("/api/home") || u.startsWith("/api/late"))).toEqual([]);
    });
  }
});
