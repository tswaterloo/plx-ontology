import { afterEach, describe, expect, it, vi } from "vitest";
import { capNotices, desktopBridge, freshOnes, menuTarget } from "./desktop";

afterEach(() => vi.unstubAllGlobals());

describe("desktop bridge", () => {
  it("is absent in a browser, and present only with the full bridge", () => {
    expect(desktopBridge()).toBeNull();
    (window as unknown as { plxDesktop: unknown }).plxDesktop = { app: true };
    expect(desktopBridge()).toBeNull();
    (window as unknown as { plxDesktop: unknown }).plxDesktop = { app: true, version: "1", notify: () => undefined, onMenu: () => () => undefined };
    expect(desktopBridge()).not.toBeNull();
    delete (window as unknown as { plxDesktop?: unknown }).plxDesktop;
  });

  it("a menu choice asks for feedback or a known page; a clicked notification only for a path of this app", () => {
    expect(menuTarget({ action: "feedback" })).toEqual({ kind: "feedback" });
    expect(menuTarget({ action: "my-work" })).toEqual({ kind: "go", href: "/my-work" });
    expect(menuTarget({ href: "/autopilot?item=i1" })).toEqual({ kind: "go", href: "/autopilot?item=i1" });
    for (const bad of [null, "x", 5, {}, { action: "rm -rf" }, { action: "constructor" }, { action: "__proto__" }, { href: "https://evil.example/" }, { href: "//evil.example" }, { href: "javascript:alert(1)" }, { href: "/a b" }, { href: "/\\evil" }, { href: 5 }]) {
      expect(menuTarget(bad)).toBeNull();
    }
  });

  it("tells the person only about what is new, never on the first read, and caps a burst", () => {
    const p = (id: string) => ({ id, title: `t ${id}` });
    expect(freshOnes(null, [p("a"), p("b")])).toEqual([]);
    expect(freshOnes(new Set(["a"]), [p("a"), p("b")]).map((x) => x.id)).toEqual(["b"]);
    expect(freshOnes(new Set(["a", "b"]), [p("a"), p("b")])).toEqual([]);
    const burst = capNotices(["1", "2", "3", "4", "5"].map(p), 3);
    expect(burst.map((n) => n.title)).toEqual(["t 1", "t 2", "t 3", "2 more need you"]);
    expect(capNotices([p("1")])).toHaveLength(1);
    expect(capNotices([])).toEqual([]);
  });
});
