import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, beforeEach, vi } from "vitest";

const nav = vi.hoisted(() => ({ path: "/" }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.path, useRouter: () => ({ push: () => undefined }), useSearchParams: () => new URLSearchParams() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), logSaved: () => Promise.resolve() }));
import { currentChild, HELP, NAV_SECTIONS, navFor, navSectionsFor, sectionOf, SidebarNav, sidebarPins } from "./shell";
import { defaultPins, pinsFrom, pinnable, togglePin } from "@/lib/pins";
import { clearSavedFor, setSavedUser, toggleSaved, useSaved } from "@/lib/saved";
import { act, render, renderHook, screen } from "@testing-library/react";

const ROUTES = readdirSync(join(__dirname, "../app/(app)"), { withFileTypes: true })
  .filter((d) => d.isDirectory())
  .map((d) => `/${d.name}`);

describe("the sidebar: one fixed list (owner, 11-17: items moved depending on the page)", () => {
  it("a plain reader sees Home, Ask, Records, Meetings, Autopilot; no Team, no Admin; Memory, Templates and Work log in the account menu (simpler tabs 11-18)", () => {
    expect(navSectionsFor({}).filter((s) => s.group === "main").map((s) => s.label)).toEqual(["Home", "Ask", "Records", "Meetings", "Autopilot"]);
    expect(navSectionsFor({}).filter((s) => s.group === "account").map((s) => s.href)).toEqual(["/memory", "/templates", "/work-log"]);
    expect(navSectionsFor({}).some((s) => s.group === "last")).toBe(false);
  });

  it("Team shows only to a reader with a team grant: a full-read grant, an ops reader or the owner", () => {
    const team = (me: Record<string, unknown>) => navSectionsFor(me).some((s) => s.key === "team");
    expect(team({})).toBe(false);
    expect(team({ roles: ["purchasing"] })).toBe(false);
    expect(team({ access_label: "Viewing with full-read access" })).toBe(true);
    expect(team({ ops_reader: true })).toBe(true);
    expect(team({ owner: true })).toBe(true);
    expect(navFor({}).map((n) => n.href)).not.toContain("/team");
  });

  it("Admin shows to an ops reader (Reliability, Trust) and to a feedback admin (triage only)", () => {
    const ops = navSectionsFor({ ops_reader: true }).find((s) => s.key === "admin")!;
    expect(ops.children.map((c) => c.href)).toEqual(["/ops", "/?show=alerts#data-health", "/trust", "/autopilot-log"]);
    const fb = navSectionsFor({ feedback_admin: true }).find((s) => s.key === "admin")!;
    expect(fb.children.map((c) => c.href)).toEqual(["/feedback"]);
    expect(fb.href).toBe("/feedback"); // its link goes where this reader may go
    expect(navFor({ access_label: "Viewing with full-read access" }).map((n) => n.href)).not.toContain("/ops");
  });

  it("sub-pages are tabs of the page they belong to, never of another item", () => {
    const of = (key: string) => NAV_SECTIONS.find((s) => s.key === key)!.children.map((c) => c.href);
    expect(of("home")).toEqual(["/", "/?show=alerts", "/?show=late"]); // one page; Data health is Admin's
    expect(of("work-log")).toEqual(["/work-log", "/activity"]);
    expect(of("ask")).toEqual([]); // Memory and Templates are their own items, never under Ask
    expect(of("autopilot")).toEqual([]); // Team's approvals are Team's
    for (const s of NAV_SECTIONS) {
      for (const c of s.children) {
        const [base, frag] = c.href.split("#");
        const [path, query] = base.split("?");
        expect(sectionOf(path, NAV_SECTIONS, frag ? `#${frag}` : "", query ?? "")?.key, c.href).toBe(s.key);
      }
    }
  });

  it("no page was dropped: every route under (app) is an item, a tab, a list or Help", () => {
    expect(ROUTES.length).toBeGreaterThan(15);
    expect(ROUTES.filter((r) => !sectionOf(r) && r !== HELP.href)).toEqual([]);
    // the pages folded into Home are gone; their addresses redirect into its tabs (next.config)
    for (const gone of ["/watching", "/late", "/my-work"]) expect(ROUTES).not.toContain(gone);
  });

  it("the pages folded into Home redirect into its tabs, query kept (next.config)", async () => {
    const config = (await import("../../next.config")).default;
    const to = new Map((await config.redirects!()).map((r) => [r.source, r.destination]));
    expect(to.get("/watching")).toBe("/?show=alerts");
    expect(to.get("/late")).toBe("/?show=late");
    expect(to.get("/my-work")).toBe("/");
    expect(to.get("/briefing")).toBe("/?show=alerts#alerts");
    // every tab a redirect lands on is one Home draws
    const home = NAV_SECTIONS.find((s) => s.key === "home")!.children.map((c) => c.href);
    for (const d of to.values()) if (d.startsWith("/?show=")) expect(home).toContain(d.split("#")[0]);
  });

  it("each old place opens under the item that now holds it", () => {
    expect(sectionOf("/supplier/S-1")?.key).toBe("records");
    expect(sectionOf("/object/PurchaseOrder/x")?.key).toBe("records");
    expect(sectionOf("/memory")?.key).toBe("memory");
    expect(sectionOf("/templates")?.key).toBe("templates");
    expect(sectionOf("/late")?.key).toBe("home");
    expect(sectionOf("/watching")?.key).toBe("home");
    expect(sectionOf("/account/C-1")?.key).toBe("records");
    expect(sectionOf("/order/SO-1")?.key).toBe("records");
    expect(sectionOf("/my-work")?.key).toBe("home");
    expect(sectionOf("/activity")?.key).toBe("work-log");
    expect(sectionOf("/meetings/abc")?.key).toBe("meetings");
    expect(sectionOf("/team", NAV_SECTIONS, "#approvals")?.key).toBe("team");
    expect(sectionOf("/team", NAV_SECTIONS, "#workload")?.key).toBe("team");
    expect(sectionOf("/")?.key).toBe("home");
    expect(sectionOf("/signin")).toBeNull();
    expect(sectionOf("/help")).toBeNull(); // Help is the foot link, not an item
  });

  it("Home's tab follows its ?show=: plain / is what needs you, a #fragment tab (Data health) is Admin's", () => {
    const home = NAV_SECTIONS.find((s) => s.key === "home")!.children;
    const admin = NAV_SECTIONS.find((s) => s.key === "admin")!.children;
    expect(sectionOf("/", NAV_SECTIONS, "#data-health", "show=alerts")?.key).toBe("admin");
    expect(currentChild(admin, "/", "#data-health", "show=alerts")).toBe("/?show=alerts#data-health");
    expect(currentChild(home, "/", "", "")).toBe("/");
    expect(currentChild(home, "/", "", "supplier=Acme")).toBe("/"); // Home's own supplier filter
    expect(currentChild(home, "/", "", "show=alerts&for=purchasing")).toBe("/?show=alerts");
    expect(currentChild(home, "/", "#alerts", "show=alerts")).toBe("/?show=alerts");
    expect(currentChild(home, "/", "", "show=late&supplier=Acme&sel=PurchaseOrder%3A1")).toBe("/?show=late");
    expect(currentChild(home, "/", "", "show=nonsense")).toBe("/"); // an unknown tab opens what needs you
    expect(currentChild(home, "/ask", "", "show=alerts")).toBeNull();
  });

  it("Quality and R&D projects are gone: no nav place, no section, old pins are not shown", () => {
    const has = (me: Record<string, unknown>, href: string) => navFor(me).some((n) => n.href === href);
    for (const me of [{}, { roles: ["quality"] }, { owner: true, ops_reader: true }]) {
      expect(has(me, "/qc")).toBe(false);
      expect(has(me, "/rd-projects")).toBe(false);
    }
    expect(sectionOf("/qc")).toBeNull();
    expect(sectionOf("/rd-projects")).toBeNull();
    expect(pinnable("/qc?view=audit")).toBe(false);
    expect(pinnable("/rd-projects")).toBe(false);
  });

  it("the owner's sidebar is Home, Ask, Records, Meetings, Autopilot, Team, then Admin; Memory, Templates and Work log are in the account menu", () => {
    const secs = navSectionsFor({ owner: true, ops_reader: true, feedback_admin: true });
    expect(secs.filter((s) => s.group !== "account").map((s) => s.label)).toEqual(["Home", "Ask", "Records", "Meetings", "Autopilot", "Team", "Admin"]);
    expect(secs.filter((s) => s.group === "account").map((s) => s.key)).toEqual(["memory", "templates", "work-log"]);
  });
});

describe("the sidebar drawn on every page is identical", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setSavedUser("employee:a");
  });

  const owner = { owner: true, ops_reader: true, feedback_admin: true, roles: ["purchasing"] };
  const PAGES: [string, string, string][] = [
    // path, #fragment, the one row that is highlighted
    ["/", "", "/"], ["/?show=alerts", "", "/"], ["/?show=late", "", "/"], ["/?show=alerts", "#data-health", "/ops"],
    ["/ask", "", "/ask"], ["/memory", "", ""], ["/templates", "", ""],
    ["/autopilot", "", "/autopilot"], ["/meetings", "", "/meetings"], ["/team", "", "/team"], ["/team", "#approvals", "/team"],
    ["/records", "", "/records"], ["/supplier/S-1", "", "/records"], ["/object/PurchaseOrder/x", "", "/records"],
    ["/work-log", "", ""], ["/activity", "", ""], ["/ops", "", "/ops"], ["/trust", "", "/ops"],
    ["/feedback", "", "/ops"], ["/help", "", ""],
  ];

  it("the same rows in the same order on every page; only the current one is highlighted", () => {
    let first: string[][] | null = null;
    for (const [path, hash, current] of PAGES) {
      nav.path = path.split("?")[0];
      window.history.replaceState(null, "", `${path}${hash}`);
      const { container, unmount } = render(<SidebarNav me={owner} late={4} />);
      act(() => window.dispatchEvent(new HashChangeEvent("hashchange")));
      const links = [...container.querySelectorAll("a")];
      const rows = links.map((a) => [a.getAttribute("href") ?? "", a.textContent ?? ""]);
      if (!first) first = rows;
      expect(rows, `${path}${hash}`).toEqual(first);
      expect(links.filter((a) => a.getAttribute("aria-current") === "page").map((a) => a.getAttribute("href")), `${path}${hash}`).toEqual(current ? [current] : []);
      unmount();
    }
    // every item once, nothing twice: the owner's default pins (Team's approvals, Autopilot, Alerts) are not repeated
    const hrefs = first!.map(([h]) => h);
    expect(new Set(hrefs).size).toBe(hrefs.length);
    // the seven items, then the one pin that is a view of its own (purchasing's supplier alerts)
    expect(first!.map(([, l]) => l)).toEqual(["Home", "Ask", "Records", "Meetings", "Autopilot", "Team", "Admin", "Supplier issues"]);
    expect(first!.map(([, l]) => l).join("|")).not.toMatch(/Watching|Memory|Templates|Approvals|Autopilot review queue|Alerts|Autopilot log|Prepared for you|Your actions|Lists|ops targets? missed/);
  });

  it("a pin to a place the nav already names (an item or its tab) is left out; a filtered view stays", () => {
    const pins = [...defaultPins({ owner: true }), ...defaultPins({ roles: ["purchasing", "quality", "customer_service", "rnd"] })];
    expect(sidebarPins(pins, { owner: true }).map((p) => p.href)).toEqual(["/?show=alerts&for=purchasing#alerts", "/?view=reply", "/?show=alerts&for=customer_service#alerts"]);
    // Team is not in a plain reader's nav, so a pin to it stays theirs
    expect(sidebarPins([{ href: "/team#approvals", label: "Approvals" }], {}).map((p) => p.href)).toEqual(["/team#approvals"]);
    expect(sidebarPins([{ href: "/help", label: "Help" }, { href: "/ask", label: "Ask" }], {})).toEqual([]);
  });
});

describe("each page says what it is for", () => {
  it("every page outside a record opens with its one sentence (Memory and Templates have their own opening line)", () => {
    const records = new Set(["/account", "/order", "/customer", "/supplier", "/po", "/ao", "/object", "/source"]);
    const own = new Set(["/memory", "/templates"]);
    // /my-work only opens Home now
    const pages = ["/", ...ROUTES.filter((r) => !records.has(r) && !own.has(r))];
    for (const r of pages) {
      const src = readFileSync(join(__dirname, "../app/(app)", r === "/" ? "" : r, "page.tsx"), "utf8");
      expect(src, r).toContain(`<PageIntro path="${r}"`);
    }
    // Home's Alerts and Late POs tabs have their own line
    expect(readFileSync(join(__dirname, "home/alerts-view.tsx"), "utf8")).toContain(`<PageIntro path="/?show=alerts"`);
    expect(readFileSync(join(__dirname, "home/late-view.tsx"), "utf8")).toContain(`<PageIntro path="/?show=late"`);
    for (const r of own) expect(readFileSync(join(__dirname, "../app/(app)", r, "page.tsx"), "utf8"), r).toMatch(/text-muted">\s*\S/);
  });
});

describe("pinned pages", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setSavedUser("employee:a");
  });

  it("the owner's defaults are approvals and alerts; purchasing's are POs and supplier issues", () => {
    expect(defaultPins({ owner: true }).map((p) => p.label)).toEqual(["Approvals waiting on others", "Autopilot review queue", "Alerts"]);
    expect(defaultPins({ roles: ["purchasing"] }).map((p) => p.href)).toEqual(["/?show=late", "/?show=alerts&for=purchasing#alerts"]);
    expect(defaultPins({ roles: ["made_up"] }).length).toBeGreaterThan(0); // an unknown role still gets a start
  });

  it("the first pin writes the defaults down, so an unpinned default stays gone", () => {
    const me = { roles: ["purchasing"] };
    const items = () => renderHook(() => useSaved()).result.current;
    expect(pinsFrom(items(), me).defaults).toBe(true);
    expect(togglePin({ href: "/?show=late", label: "Late POs" }, items(), me)).toBe(false); // unpinned
    const after = pinsFrom(items(), me);
    expect(after.defaults).toBe(false);
    expect(after.pins.map((p) => p.href)).toEqual(["/?show=alerts&for=purchasing#alerts"]);
    expect(togglePin({ href: "/ask?q=late", label: "Late asks" }, items(), me)).toBe(true);
    expect(pinsFrom(items(), me).pins.map((p) => p.href)).toContain("/ask?q=late");
    expect(togglePin({ href: "/qc?view=audit", label: "Quality: Audit pack" }, items(), me)).toBe(false);
  });

  it("pins never take another origin, and are this person's alone: gone on sign-out", () => {
    expect(pinnable("//evil.example/x")).toBe(false);
    expect(pinnable("https://evil.example")).toBe(false);
    expect(pinnable("/\\evil")).toBe(false);
    const me = {};
    togglePin({ href: "/records", label: "Records" }, renderHook(() => useSaved()).result.current, me);
    clearSavedFor("employee:a");
    setSavedUser("employee:b");
    expect(pinsFrom(renderHook(() => useSaved()).result.current, me).defaults).toBe(true);
  });

  it("pins and saved questions keep to their own lists", () => {
    toggleSaved({ key: "q:why late", kind: "question", label: "why late", href: "/ask?q=why%20late" });
    const me = {};
    togglePin({ href: "/records", label: "Records" }, renderHook(() => useSaved()).result.current, me);
    const items = renderHook(() => useSaved()).result.current;
    expect(items.filter((s) => s.kind === "question").map((s) => s.key)).toEqual(["q:why late"]);
    expect(pinsFrom(items, me).pins.map((p) => p.href)).toContain("/records");
  });
});
