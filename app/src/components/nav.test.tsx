import { readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, beforeEach, vi } from "vitest";

const nav = vi.hoisted(() => ({ path: "/" }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.path, useRouter: () => ({ push: () => undefined }), useSearchParams: () => new URLSearchParams() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), logSaved: () => Promise.resolve() }));
import { currentChild, LISTS, ListsNav, NAV_SECTIONS, navFor, navSectionsFor, sectionOf, seesQuality, yoursFor } from "./shell";
import { defaultPins, pinsFrom, pinnable, togglePin } from "@/lib/pins";
import { clearSavedFor, setSavedUser, toggleSaved, useSaved } from "@/lib/saved";
import { render, renderHook, screen } from "@testing-library/react";

describe("the nav: six sections and Records", () => {
  it("a plain reader sees Home, Ask, Watching, Autopilot, Work and Records, no Admin", () => {
    expect(navSectionsFor({}).map((s) => s.label)).toEqual(["Home", "Ask", "Watching", "Autopilot", "Work", "Records"]);
  });

  it("Admin shows to an ops reader (Reliability, Trust) and to a feedback admin (triage only)", () => {
    const ops = navSectionsFor({ ops_reader: true }).find((s) => s.key === "admin")!;
    expect(ops.children.map((c) => c.href)).toEqual(["/ops", "/trust"]);
    const fb = navSectionsFor({ feedback_admin: true }).find((s) => s.key === "admin")!;
    expect(fb.children.map((c) => c.href)).toEqual(["/feedback"]);
    expect(fb.href).toBe("/feedback"); // its link goes where this reader may go
    expect(navFor({ access_label: "Viewing with full-read access" }).map((n) => n.href)).not.toContain("/ops");
  });

  it("Memory and Templates sit under Ask; Late POs and Data health under Watching; Meetings, Work log and Team under Work", () => {
    const of = (key: string) => NAV_SECTIONS.find((s) => s.key === key)!.children.map((c) => c.href);
    expect(of("ask")).toEqual(expect.arrayContaining(["/memory", "/templates"]));
    expect(of("watching")).toEqual(expect.arrayContaining(["/late", "/watching#data-health"]));
    expect(of("work")).toEqual(expect.arrayContaining(["/meetings", "/work-log", "/team"]));
    expect(of("autopilot")).toEqual(expect.arrayContaining(["/autopilot", "/team#approvals"]));
  });

  it("no page was dropped: every route under (app) belongs to a section", () => {
    const routes = readdirSync(join(__dirname, "../app/(app)"), { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((d) => `/${d.name}`);
    expect(routes.length).toBeGreaterThan(15);
    // the lists are drawn on every route under "Lists" (ListsNav), so they need no section
    const lists = new Set(LISTS.map((p) => p.href));
    expect(routes.filter((r) => !sectionOf(r) && !lists.has(r))).toEqual([]);
    expect([...lists].every((h) => routes.includes(h))).toBe(true);
  });

  it("a record page is in Records, a Memory page in Ask, the Late list in Watching", () => {
    expect(sectionOf("/supplier/S-1")?.key).toBe("records");
    expect(sectionOf("/object/PurchaseOrder/x")?.key).toBe("records");
    expect(sectionOf("/memory")?.key).toBe("ask");
    expect(sectionOf("/late")?.key).toBe("watching");
    expect(sectionOf("/")?.key).toBe("home");
    expect(sectionOf("/signin")).toBeNull();
  });

  it("a #fragment place wins over its page: /team#workload is Workload, not Team", () => {
    const work = NAV_SECTIONS.find((s) => s.key === "work")!.children;
    expect(currentChild(work, "/team", "#workload")).toBe("/team#workload");
    expect(currentChild(work, "/team", "")).toBe("/team");
    expect(currentChild(work, "/meetings/abc", "")).toBe("/meetings");
  });

  it("Quality shows to quality and production-planning readers, ops readers and the owner only", () => {
    const has = (me: Record<string, unknown>) => navFor(me).some((n) => n.href === "/qc");
    expect(has({})).toBe(false);
    expect(has({ roles: ["purchasing"] })).toBe(false);
    expect(has({ roles: ["quality"] })).toBe(true);
    expect(has({ roles: ["production_planning"] })).toBe(true);
    expect(has({ owner: true })).toBe(true);
    expect(seesQuality(null)).toBe(false);
  });
});

describe("memory, templates and logs from every section", () => {
  it("each section offers Memory, Templates and Work log, except what the open section already lists", () => {
    for (const s of navSectionsFor({ ops_reader: true, feedback_admin: true })) {
      const hrefs = [...s.children.map((c) => c.href), ...yoursFor({}, s).map((p) => p.href)];
      expect(hrefs, s.key).toEqual(expect.arrayContaining(["/memory", "/templates", "/work-log"]));
    }
    const ask = NAV_SECTIONS.find((s) => s.key === "ask")!;
    expect(yoursFor({}, ask).map((p) => p.href)).toEqual(["/work-log"]);
  });

  it("the autopilot's run log is the owner's only", () => {
    expect(yoursFor({ owner: true }, null).map((p) => p.href)).toContain("/autopilot?tab=audit");
    expect(yoursFor({ ops_reader: true, feedback_admin: true }, null).map((p) => p.href)).not.toContain("/autopilot?tab=audit");
  });
});

describe("Lists: the same on every route (owner, 11-16: it went away on some pages)", () => {
  it("draws the list for the owner, by its plain name, on every section's pages", () => {
    const paths = ["/", "/ask", "/watching", "/autopilot", "/team", "/meetings", "/work-log", "/records", "/ops", "/rd-projects"];
    for (const p of paths) {
      nav.path = p;
      const { unmount } = render(<ListsNav me={{ owner: true }} />);
      const block = screen.getByTestId("nav-lists");
      expect(block, p).toHaveTextContent("Lists");
      expect([...block.querySelectorAll("a")].map((a) => [a.getAttribute("href"), a.textContent]), p).toEqual([
        ["/rd-projects", "R&D and sample projects"]]);
      unmount();
    }
  });

  it("no section lists them as well, and the nav and phone menu still reach them", () => {
    const listed = new Set(LISTS.map((p) => p.href));
    for (const s of navSectionsFor({ owner: true, ops_reader: true, feedback_admin: true })) {
      expect(s.children.filter((c) => listed.has(c.href)), s.key).toEqual([]);
    }
    expect(navFor({ owner: true }).map((n) => n.href)).toContain("/rd-projects");
  });

  it("Team opens Work; only Approvals (/team#approvals) opens Autopilot", () => {
    expect(sectionOf("/team")?.key).toBe("work");
    expect(sectionOf("/team", NAV_SECTIONS, "#workload")?.key).toBe("work");
    expect(sectionOf("/team", NAV_SECTIONS, "#approvals")?.key).toBe("autopilot");
    expect(sectionOf("/autopilot")?.key).toBe("autopilot");
  });
});

describe("pinned pages", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setSavedUser("employee:a");
  });

  it("the owner's defaults are approvals and alerts; purchasing's are POs and supplier issues", () => {
    expect(defaultPins({ owner: true }).map((p) => p.label)).toEqual(["Approvals", "Autopilot approvals", "Alerts"]);
    expect(defaultPins({ roles: ["purchasing"] }).map((p) => p.href)).toEqual(["/late", "/watching?for=purchasing#alerts"]);
    expect(defaultPins({ roles: ["made_up"] }).length).toBeGreaterThan(0); // an unknown role still gets a start
  });

  it("the first pin writes the defaults down, so an unpinned default stays gone", () => {
    const me = { roles: ["purchasing"] };
    const items = () => renderHook(() => useSaved()).result.current;
    expect(pinsFrom(items(), me).defaults).toBe(true);
    expect(togglePin({ href: "/late", label: "Late POs" }, items(), me)).toBe(false); // unpinned
    const after = pinsFrom(items(), me);
    expect(after.defaults).toBe(false);
    expect(after.pins.map((p) => p.href)).toEqual(["/watching?for=purchasing#alerts"]);
    expect(togglePin({ href: "/qc?view=audit", label: "Quality: Audit pack" }, items(), me)).toBe(true);
    expect(pinsFrom(items(), me).pins.map((p) => p.href)).toContain("/qc?view=audit");
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
