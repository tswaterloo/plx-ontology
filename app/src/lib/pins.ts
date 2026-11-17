"use client";

// Pinned pages: the places a person keeps on My work and in the sidebar. They live in the
// saved list (one per signed-in person in this browser, deleted on sign-out) as kind
// "page". Until the person pins or unpins anything, their role's defaults show; the first
// change writes those defaults down as real pins, so unpinning a default sticks.

import { useSaved, writePages, type SavedItem } from "./saved";

export interface PinnedPage {
  href: string;
  label: string;
}

/** Written once the person has changed their pins: from then on only their own list shows. */
const SET = "p:#set";

/** Who the defaults are for: the owner, and the reader's work roles (team snapshot keys). */
export interface PinReader {
  owner?: boolean;
  roles?: string[] | null;
}

/**
 * Per-role defaults. The owner sees what waits on him (approvals) and what is going wrong
 * (alerts); purchasing sees its POs and supplier issues; the other roles their own desk.
 * A reader with no known role gets alerts and the Ask page.
 */
export const ROLE_PINS: Record<string, PinnedPage[]> = {
  owner: [
    { href: "/team#approvals", label: "Approvals" },
    { href: "/autopilot", label: "Autopilot approvals" },
    { href: "/watching#alerts", label: "Alerts" },
  ],
  purchasing: [
    { href: "/late", label: "Late POs" },
    { href: "/watching?for=purchasing#alerts", label: "Supplier issues" },
  ],
  customer_service: [
    { href: "/?view=reply", label: "Replies to send" },
    { href: "/watching?for=customer_service#alerts", label: "Customer issues" },
  ],
  quality: [{ href: "/qc", label: "Quality" }],
  production_planning: [
    { href: "/late", label: "Late POs" },
    { href: "/qc", label: "Quality" },
  ],
  receivables: [{ href: "/watching?for=finance#alerts", label: "Finance alerts" }],
  payables: [{ href: "/watching?for=finance#alerts", label: "Finance alerts" }],
  finance: [{ href: "/watching?for=finance#alerts", label: "Finance alerts" }],
  rnd: [{ href: "/rd-projects", label: "R&D and sample projects" }],
};
const FALLBACK: PinnedPage[] = [
  { href: "/watching#alerts", label: "Alerts" },
  { href: "/ask", label: "Ask" },
];

/** The defaults for this reader, owner first, then each role in order, no href twice. */
export function defaultPins(reader: PinReader | null | undefined): PinnedPage[] {
  const keys = [...(reader?.owner ? ["owner"] : []), ...(reader?.roles ?? []).filter((r) => r in ROLE_PINS)];
  const out: PinnedPage[] = [];
  for (const k of keys) for (const p of ROLE_PINS[k]) if (!out.some((o) => o.href === p.href)) out.push(p);
  return out.length ? out : FALLBACK;
}

/** A page href that may be pinned: this app's own path, never another origin. */
export function pinnable(href: string): boolean {
  return href.startsWith("/") && !href.startsWith("//") && !href.includes("\\") && href.length <= 300;
}

function pagesOf(items: SavedItem[]): SavedItem[] {
  return items.filter((s) => s.kind === "page");
}

/** The person's pinned pages (their own list once they changed it, else their role's defaults). */
export function pinsFrom(items: SavedItem[], reader: PinReader | null | undefined): { pins: PinnedPage[]; defaults: boolean } {
  const pages = pagesOf(items);
  if (!pages.some((p) => p.key === SET)) return { pins: defaultPins(reader), defaults: true };
  return { pins: pages.filter((p) => p.key !== SET && pinnable(p.href)).map((p) => ({ href: p.href, label: p.label })), defaults: false };
}

export function usePins(reader: PinReader | null | undefined) {
  return pinsFrom(useSaved(), reader);
}

/** Pin or unpin a page. Returns whether it is now pinned; false (and no change) when nobody is signed in. */
export function togglePin(page: PinnedPage, items: SavedItem[], reader: PinReader | null | undefined): boolean {
  if (!pinnable(page.href)) return false;
  const { pins } = pinsFrom(items, reader);
  const on = !pins.some((p) => p.href === page.href);
  const next = on ? [...pins, { href: page.href, label: page.label.slice(0, 80) || "Page" }] : pins.filter((p) => p.href !== page.href);
  const at = new Date().toISOString();
  const wrote = writePages([
    { key: SET, kind: "page", label: "", href: "/", at },
    ...next.slice(0, 20).map((p) => ({ key: `p:${p.href}`, kind: "page" as const, label: p.label, href: p.href, at })),
  ]);
  return wrote && on;
}
