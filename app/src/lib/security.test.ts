import { afterEach, describe, expect, it } from "vitest";
import { buildCsp } from "./csp";
import { checkEntraConfig, EntraConfigError } from "./entra-config";
import { plainError } from "./errors";
import { clearSavedFor, setSavedUser, toggleSaved } from "./saved";
import { captureTicket, takeTicket } from "./ticket";
import { TICKET_BOOT_SCRIPT } from "./ticket-boot";
import type { EntraConfig } from "./types";

const NONCE = "bm9uY2Utbm9uY2Utbm9uY2U=";

describe("Content-Security-Policy", () => {
  it("allows only nonce-stamped inline code and the API plus Microsoft sign-in", () => {
    const csp = buildCsp(NONCE);
    const d = Object.fromEntries(csp.split("; ").map((x) => [x.split(" ")[0], x.split(" ").slice(1).join(" ")]));
    expect(d["default-src"]).toBe("'self'");
    expect(d["script-src"]).toBe(`'self' 'nonce-${NONCE}' 'strict-dynamic'`);
    expect(d["style-src"]).toBe(`'self' 'nonce-${NONCE}'`);
    expect(d["connect-src"]).toBe("'self' https://login.microsoftonline.com");
    expect(d["frame-ancestors"]).toBe("'none'");
    expect(d["frame-src"]).toBe("https://login.microsoftonline.com");
    expect(d["base-uri"]).toBe("'self'");
    expect(d["form-action"]).toBe("'self'");
    expect(d["img-src"]).toBe("'self' data:");
    expect(d["object-src"]).toBe("'none'");
    expect(csp).not.toContain("unsafe-inline");
    expect(csp).not.toContain("unsafe-eval");
  });

  it("adds unsafe-eval only in development and refuses a malformed nonce", () => {
    expect(buildCsp(NONCE, { dev: true })).toContain("'unsafe-eval'");
    for (const bad of ["", "short", "abc'; script-src *", "x y z 1234567890123456"]) {
      expect(() => buildCsp(bad)).toThrow();
    }
  });
});

const entra = (authority: string, tenant_id = "3f1c2b4a-1111-2222-3333-444455556666"): EntraConfig => ({
  authority,
  tenant_id,
  client_id: "00000000-aaaa-bbbb-cccc-000000000000",
  scopes: ["api://plx-workbench/access"],
  redirect_uri: null,
});

describe("Entra configuration", () => {
  it("accepts the tenant's own authority", () => {
    const t = "3f1c2b4a-1111-2222-3333-444455556666";
    expect(() => checkEntraConfig(entra(`https://login.microsoftonline.com/${t}`))).not.toThrow();
    expect(() => checkEntraConfig(entra(`https://login.microsoftonline.com/${t}/v2.0`))).not.toThrow();
    expect(() => checkEntraConfig(entra("https://login.microsoftonline.com/plx.example.com", "plx.example.com"))).not.toThrow();
  });

  it("rejects multi-tenant, foreign-host, mismatched or malformed authorities", () => {
    const t = "3f1c2b4a-1111-2222-3333-444455556666";
    for (const a of [
      "https://login.microsoftonline.com/common",
      "https://login.microsoftonline.com/organizations",
      "https://login.microsoftonline.com/consumers/v2.0",
      "https://login.microsoftonline.com/Common",
      `http://login.microsoftonline.com/${t}`,
      `https://evil.example/${t}`,
      `https://login.microsoftonline.com.evil.example/${t}`,
      "https://login.microsoftonline.com/",
      `https://login.microsoftonline.com/${t}/oauth2/v2.0/authorize`,
      `https://login.microsoftonline.com/${t}?x=1`,
      "https://login.microsoftonline.com/aaaaaaaa-0000-0000-0000-000000000000",
      "not a url",
    ]) {
      expect(() => checkEntraConfig(entra(a)), a).toThrow(EntraConfigError);
    }
    expect(() => checkEntraConfig({ ...entra(`https://login.microsoftonline.com/${t}`), scopes: [] })).toThrow(EntraConfigError);
  });

  it("explains a misconfiguration in plain words", () => {
    let err: unknown;
    try {
      checkEntraConfig(entra("https://login.microsoftonline.com/common"));
    } catch (e) {
      err = e;
    }
    expect(plainError(err)).toMatch(/isn't set up correctly/);
  });
});

describe("access-link ticket", () => {
  it("is removed from the URL on any page and handed out once", () => {
    window.history.replaceState(null, "", "/object/PurchaseOrder/x?y=1#ticket=abc%2Fdef");
    captureTicket();
    expect(window.location.href).not.toContain("ticket");
    expect(window.location.hash).toBe("");
    expect(window.location.pathname + window.location.search).toBe("/object/PurchaseOrder/x?y=1");
    expect(takeTicket()).toBe("abc/def");
    expect(takeTicket()).toBeNull();
  });

  it("strips the fragment even when other parameters come first", () => {
    window.history.replaceState(null, "", "/#a=1&ticket=zz");
    captureTicket();
    expect(window.location.hash).toBe("");
    expect(takeTicket()).toBe("zz");
  });

  it("the head script strips it before hydration and hands it over in memory only", () => {
    window.history.replaceState(null, "", "/ask?q=x#ticket=t%201");
    new Function(TICKET_BOOT_SCRIPT)();
    expect(window.location.hash).toBe("");
    expect(window.location.pathname + window.location.search).toBe("/ask?q=x");
    expect(Object.keys(sessionStorage).concat(Object.keys(localStorage)).join()).not.toContain("ticket");
    captureTicket();
    expect(takeTicket()).toBe("t 1");
    expect((window as unknown as { __plxTicket?: string }).__plxTicket).toBeUndefined();
  });

  it("leaves other fragments alone", () => {
    window.history.replaceState(null, "", "/ask#main");
    captureTicket();
    expect(window.location.hash).toBe("#main");
    expect(takeTicket()).toBeNull();
  });
});

describe("saved items", () => {
  afterEach(() => {
    setSavedUser(null);
    localStorage.clear();
  });
  const item = { key: "q:why late", kind: "question" as const, label: "why late", href: "/ask?q=why%20late" };

  it("are kept per signed-in person and deleted on sign-out", () => {
    setSavedUser("employee:a");
    expect(toggleSaved(item)).toBe(true);
    expect(localStorage.getItem("plx.saved.v2:employee:a")).toContain("why late");

    setSavedUser("employee:b");
    expect(toggleSaved({ ...item, key: "q:other", label: "other" })).toBe(true);
    expect(localStorage.getItem("plx.saved.v2:employee:b")).not.toContain("why late");

    clearSavedFor("employee:b");
    expect(localStorage.getItem("plx.saved.v2:employee:b")).toBeNull();
    expect(localStorage.getItem("plx.saved.v2:employee:a")).toContain("why late");
  });

  it("cannot be saved with nobody signed in, and the old shared list is removed", () => {
    localStorage.setItem("plx.saved.v1", JSON.stringify([item]));
    expect(toggleSaved(item)).toBe(false);
    setSavedUser("employee:a");
    expect(localStorage.getItem("plx.saved.v1")).toBeNull();
  });
});
