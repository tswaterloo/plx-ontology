import { existsSync } from "node:fs";
import { join } from "node:path";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

// The owner, 11-16: the lists should not clutter everyone's menu. R&D and sample projects:
// the owner, R&D and sales / customer service. Every menu that names it (sidebar, phone More
// menu, command bar) follows the same rule; the page stays reachable by URL. 11-17: the MRP
// testing list is not ours and is gone from every menu (and the app).

const nav = vi.hoisted(() => ({ path: "/" }));
const auth = vi.hoisted(() => ({ me: null as Record<string, unknown> | null }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.path, useRouter: () => ({ push: () => undefined }), useSearchParams: () => new URLSearchParams() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<object>()), logSaved: () => Promise.resolve(), suggest: () => Promise.resolve({ suggestions: [] }) }));
vi.mock("./auth", async (orig) => ({
  ...(await orig<object>()),
  useAuth: () => ({
    state: auth.me ? { status: "signed-in", me: auth.me, config: null } : { status: "loading", config: null },
    refresh: () => undefined,
    signOut: async () => true,
  }),
}));

import CommandPalette from "./command-palette-dialog";
import { ListsNav, navFor, TabBar } from "./shell";
import { LISTS, listShownTo } from "@/lib/lists";

const RD = "/rd-projects";
const MRP = "/uat-feedback";
const base = { display_name: "Reader", email: null, auth: "entra", dev_mode: false };

// who, the reader, what their menus should name
const READERS: [string, Record<string, unknown> | null, string[]][] = [
  ["owner", { ...base, owner: true, feedback_admin: true, roles: [] }, [RD]],
  ["owner flag alone", { ...base, owner: true }, [RD]],
  ["R&D", { ...base, roles: ["rnd"] }, [RD]],
  ["customer service", { ...base, roles: ["customer_service"] }, [RD]],
  ["sales", { ...base, roles: ["sales"] }, [RD]],
  ["feedback admin, purchasing", { ...base, feedback_admin: true, roles: ["purchasing"] }, []],
  ["feedback admin with an R&D role", { ...base, feedback_admin: true, roles: ["rnd", "purchasing"] }, [RD]],
  ["purchasing", { ...base, roles: ["purchasing"] }, []],
  ["ops reader, no role", { ...base, ops_reader: true, roles: [] }, []],
  ["full-read grant, no role", { ...base, access_label: "Viewing with full-read access" }, []],
  ["unknown role", { ...base, roles: ["RND", "r&d", "sales_ops"] }, []],
  ["roles null", { ...base, roles: null }, []],
  ["not signed in", null, []],
];

const lists = (hrefs: (string | null)[]) => hrefs.filter((h) => h === RD || h === MRP);

afterEach(() => {
  auth.me = null;
});

describe("the Lists entries follow the reader's role", () => {
  it.each(READERS)("sidebar: %s", (_who, me, want) => {
    nav.path = "/";
    render(<ListsNav me={me as never} />);
    const block = screen.queryByTestId("nav-lists");
    if (!want.length) {
      expect(block).toBeNull(); // no entry: the block and its heading are gone entirely
      return;
    }
    expect(block).toHaveTextContent("Lists");
    expect([...block!.querySelectorAll("a")].map((a) => a.getAttribute("href"))).toEqual(want);
  });

  it.each(READERS)("nav places (keyboard and More): %s", (_who, me, want) => {
    expect(lists(navFor(me as never).map((p) => p.href))).toEqual(want);
  });

  it.each(READERS)("phone More menu: %s", async (_who, me, want) => {
    auth.me = me;
    nav.path = "/";
    render(<TabBar />);
    await userEvent.click(screen.getByTestId("tab-more"));
    const menu = await screen.findByTestId("more-menu");
    expect(lists([...within(menu).queryAllByRole("menuitem")].map((a) => a.getAttribute("href")))).toEqual(want);
  });

  it.each(READERS)("command bar: %s", (_who, me, want) => {
    auth.me = me;
    render(<CommandPalette open onOpenChange={() => undefined} />);
    const hrefs = screen.queryAllByRole("option").map((o) => o.getAttribute("id") ?? "");
    expect(lists(hrefs.map((id) => (id.startsWith("palette-go-") ? id.slice(11) : null)))).toEqual(want);
  });

  it("a path that is not one of the lists is not gated", () => {
    expect(listShownTo("/ask", null)).toBe(true);
  });

  it("the MRP testing list is gone: no page, no menu entry for anyone", () => {
    expect(existsSync(join(__dirname, "..", "app", "(app)", "uat-feedback"))).toBe(false);
    expect(LISTS.map((p) => p.href)).toEqual([RD]);
  });
});
