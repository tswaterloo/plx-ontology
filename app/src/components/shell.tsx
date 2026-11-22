"use client";

import * as Menu from "@radix-ui/react-dropdown-menu";
import { Activity, BellRing, BookMarked, Bookmark, CalendarClock, ChartColumn, CircleHelp, Database, FileText, History, Inbox, MessageCircle, MoreHorizontal, Pin, RefreshCw, Search, TriangleAlert, Users, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { lazy, Suspense, useEffect, useState } from "react";
import { afterPageReads, getHome, getLateList, getOpsAlarms, getSidebar, type OpsAlarms, type OpsCheck } from "@/lib/api";
import { DesktopBridge } from "./desktop-bridge";
import { CommandPaletteProvider, useCommandPalette } from "./command-palette";
import { FeedbackButton, FeedbackProvider, useFeedbackDialog } from "./feedback-dialog";
import { eastern, plainServerNote, SYSTEM_LABEL } from "@/lib/format";
import { cn } from "@/lib/cn";
import { stashPending } from "@/lib/pending";
import { useShortcut } from "@/lib/platform";
import { useSaved } from "@/lib/saved";
import { usePins, type PinnedPage, type PinReader } from "@/lib/pins";
import { readAllLate } from "@/lib/late";
import { readAheadRoute } from "@/lib/route-reads";
import { onSessionReset } from "@/lib/session-cache";
import type { HomeResponse, LatePo, SidebarResponse } from "@/lib/types";
import { PlaceIcon, SystemCode } from "./ui/glyphs";
import { RequireAuth, useAuth } from "./auth";
import { ThemeToggle } from "./theme";
import { Pending } from "./ui/skeleton";
import { ViewAsBar } from "./view-as";

/** The owner's view-as picker loads when it is first opened. */
const ViewAsPicker = lazy(() => import("./view-as-picker"));
import { ErrorNotice } from "./ui/notice";
import { ApiError } from "@/lib/errors";

/**
 * The cube mark: a block with one corner cut away, in the app's three accents (cyan top,
 * blue lit face, navy shaded face). Flat polygons on no tile; the same drawing as the
 * favicon and install icons. Sized in em so it follows the wordmark.
 */
export function CubeMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 20.78 24" className={cn("h-[1.2em] w-auto shrink-0", className)} aria-hidden focusable="false" data-testid="cube-mark">
      <polygon points="10.39,12 15.59,15 10.39,18 5.2,15" fill="#4FDCEA" />
      <polygon points="10.39,12 5.2,15 5.2,9 10.39,6" fill="var(--cube-right)" />
      <polygon points="10.39,12 15.59,15 15.59,9 10.39,6" fill="#4DA8FF" />
      <polygon points="10.39,0 20.78,6 15.59,9 10.39,6 5.2,9 0,6" fill="#4FDCEA" />
      <polygon points="20.78,18 10.39,24 10.39,18 15.59,15 15.59,9 20.78,6" fill="var(--cube-right)" />
      <polygon points="0,18 10.39,24 10.39,18 5.2,15 5.2,9 0,6" fill="#4DA8FF" />
    </svg>
  );
}

/**
 * The wordmark: the cube, then PLX/workbench in Geist Mono with the slash in the bright blue
 * (--brand-slash: #4DA8FF on dark, #2E8FF0 on light, 3:1 or better either way). Every part
 * after the cube carries data-wm="rest", so the collapsed sidebar can show the cube alone.
 * Sizes are in em, so the sign-in page can scale it.
 */
export function Logo({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-[0.5em] font-mark text-[14px] leading-5 text-fg", className)}>
      <CubeMark />
      <span data-wm="rest" className="inline-flex items-baseline">
        <span className="font-semibold tracking-[-0.04em]">PLX</span>
        <span className="px-[0.12em] font-medium text-brand-slash" aria-hidden>/</span>
        <span className="text-[0.93em] font-normal tracking-[-0.01em] text-muted">workbench</span>
      </span>
    </span>
  );
}

type SignInKind = "microsoft" | "access_link" | "demo";

function useBanner() {
  const { state } = useAuth();
  const me = state.status === "signed-in" ? state.me : null;
  const mode = me?.auth ?? state.config?.mode;
  // The server says how this reader signed in; an older server (no sign_in) falls back to
  // the mode, so a session that is not a Microsoft sign-in still reads as a demo.
  const said = me ? me.sign_in : state.config?.sign_in;
  const kind: SignInKind =
    said === "microsoft" || said === "access_link" || said === "demo"
      ? said
      : (me ? me.dev_mode || me.auth !== "entra" : !!mode && mode !== "entra")
        ? "demo"
        : "microsoft";
  // A server that says microsoft for a session it marks dev is not believed.
  const dev = kind === "demo" || (kind === "microsoft" && !!me && (me.dev_mode || me.auth !== "entra"));
  return { dev, link: !dev && kind === "access_link" && !!me, me, access: me?.access_label ?? null };
}

/**
 * How this session signed in, said once in the sidebar. A demo (mock or fixture data, or a
 * test identity): "Demo sign-in", with the full-read label under it when the reader holds
 * the grant; the data label (e.g. FIXTURE) is its tooltip and screen-reader text. A console
 * access link: "Signed in with your access link". Microsoft sign-in says nothing here.
 */
export function DevBanner({ className, compact }: { className?: string; compact?: boolean }) {
  const { dev, link, me, access } = useBanner();
  if (link) {
    return (
      <div role="note" title="Signed in with your access link" className={cn("text-meta text-muted", className)} data-testid="link-banner">
        <span className={compact ? "text-faint" : undefined}>{compact ? "Signed in by link" : "Signed in with your access link"}</span>
        {compact && <span className="sr-only">: signed in with your access link</span>}
        {access && (
          <span className={compact ? "sr-only" : "block"} data-testid="access-banner">
            <span className="sr-only">. </span>
            {access}
          </span>
        )}
      </div>
    );
  }
  if (!dev) return null;
  const label = plainServerNote(me?.data_label);
  return (
    <div
      role="note"
      title={["Demo sign-in, not Microsoft: not for staff use", label, access].filter(Boolean).join(". ")}
      className={cn("text-meta text-muted", className)}
      data-testid="dev-banner"
    >
      <span className={compact ? "text-faint" : "font-medium text-fg"}>Demo sign-in</span>
      {label && <span className="sr-only">: {label}</span>}
      {access && (
        <span className={compact ? "sr-only" : "block"} data-testid="access-banner">
          <span className="sr-only">. </span>
          {access}
        </span>
      )}
    </div>
  );
}

/** The full-read label under Microsoft sign-in (DEV mode says it in its own line). */
export function AccessBanner({ className }: { className?: string }) {
  const { dev, link, access } = useBanner();
  if (!access || dev || link) return null;
  return (
    <div role="note" className={cn("text-meta text-muted", className)} data-testid="access-banner">
      <span>{access}</span>
    </div>
  );
}

/** Home reads at most this many late orders; the rest are on the Late orders page. */
export const HOME_LATE_MOST = 300;
export interface LateRead {
  items: LatePo[];
  /** The server's count of every late order this reader may see (can exceed items). */
  total: number;
}
let lateOnce: Promise<LateRead> | null = null;
/** The late POs you may see (up to HOME_LATE_MOST, a page or three), read once per page load for Today's list (the sidebar reads its counts from /api/sidebar). `total` says when that is only part. */
export function loadLateRead(fresh = false): Promise<LateRead> {
  if (fresh || !lateOnce) {
    lateOnce = readAllLate((offset, size) => getLateList(offset, undefined, size), 100, HOME_LATE_MOST).then((r) => ({
      items: r.items,
      total: Math.max(r.total, r.items.length),
    }));
    lateOnce.catch(() => (lateOnce = null));
  }
  return lateOnce;
}
export function loadLate(fresh = false): Promise<LatePo[]> {
  return loadLateRead(fresh).then((r) => r.items);
}

/** Home's one count (late, alerts, replies and prepared items, ARCH 16): published by Home when
 * every part of it was read, shown by the sidebar's Home item; never a partial or guessed number
 * (null: unknown, nothing shown). */
let needsCount: number | null = null;
const needsListeners = new Set<(n: number | null) => void>();
// Home's last full count, kept for this browser tab so every page's sidebar shows it (it showed
// only while Home was open); kept 30 minutes, dropped when the person signs out or changes
const NEEDS_KEY = "plx.needs-count";
export const NEEDS_KEPT_MS = 30 * 60 * 1000;
function keptNeeds(now = Date.now()): number | null {
  try {
    const kept = JSON.parse(window.sessionStorage.getItem(NEEDS_KEY) ?? "null") as { n?: unknown; at?: unknown } | null;
    return kept && typeof kept.n === "number" && typeof kept.at === "number" && now - kept.at < NEEDS_KEPT_MS ? kept.n : null;
  } catch {
    return null;
  }
}
// whether Home published in this page load: then its own count (or its unknown) is said, never a kept one
let needsPublished = false;
export function publishNeedsCount(n: number | null, reset = false) {
  needsCount = n;
  needsPublished = !reset;
  try {
    if (reset) window.sessionStorage.removeItem(NEEDS_KEY);
    else if (n !== null) window.sessionStorage.setItem(NEEDS_KEY, JSON.stringify({ n, at: Date.now() }));
  } catch {
    // no storage (a private window): the count shows while Home is open, as before
  }
  for (const f of needsListeners) f(n);
}
export function useNeedsCount(): number | null {
  const [n, setN] = useState<number | null>(needsCount);
  useEffect(() => {
    setN(needsPublished ? needsCount : keptNeeds());
    needsListeners.add(setN);
    return () => void needsListeners.delete(setN);
  }, []);
  return n;
}

let homeOnce: Promise<HomeResponse> | null = null;
const homeListeners = new Set<(h: HomeResponse) => void>();
// read for the person signed in: dropped when they sign out or someone else signs in
onSessionReset(() => {
  lateOnce = null;
  homeOnce = null;
  sidebarOnce = null;
  publishNeedsCount(null, true);
});
/** /api/home read once per page load, shared by Today, the sidebar and Ask: never tied to one
 * caller's abort (a caller that leaves ignores the answer), so leaving Today early, or React
 * mounting it twice, never fails the read for the others. A fresh read (Today asking again
 * while the server refreshes) is handed to every consumer that subscribed. */
export function loadHome(fresh = false): Promise<HomeResponse> {
  if (fresh || !homeOnce) {
    const read = getHome();
    homeOnce = read;
    read.then(
      (h) => {
        if (homeOnce === read) for (const f of homeListeners) f(h);
      },
      () => {
        if (homeOnce === read) homeOnce = null;
      },
    );
  }
  return homeOnce;
}
/** Calls `f` with each later fresh home read; returns the unsubscribe. */
export function onHomeRead(f: (h: HomeResponse) => void): () => void {
  homeListeners.add(f);
  return () => void homeListeners.delete(f);
}

/** Home's own read, once the Home page made one (on this page load): the sidebar shows its late
 * count so the two agree. Never reads /api/home itself. */
function useHomeRead(): HomeResponse | null {
  const [home, setHome] = useState<HomeResponse | null>(null);
  useEffect(() => {
    let live = true;
    homeOnce?.then((h) => live && setHome(h), () => undefined);
    const off = onHomeRead((h) => live && setHome(h));
    return () => {
      live = false;
      off();
    };
  }, []);
  return home;
}

let sidebarOnce: Promise<SidebarResponse> | null = null;
/** The sidebar's numbers (/api/sidebar: the late count, the suppliers with the most late
 * orders, recent questions, the data's as-of), read once per page load and shared by the
 * sidebar and the tab bar. Small and kept per reader on the server: never Home's lists. */
export function loadSidebar(fresh = false): Promise<SidebarResponse> {
  if (fresh || !sidebarOnce) {
    const read = getSidebar();
    sidebarOnce = read;
    read.catch(() => {
      if (sidebarOnce === read) sidebarOnce = null;
    });
  }
  return sidebarOnce;
}

/** How long the sidebar yields to the page's own reads (it follows them, never waits on a slow one). */
const SIDEBAR_YIELD_MS = 300;

/** The sidebar's numbers once the person is known and the page's own reads are answered
 * or SIDEBAR_YIELD_MS have passed (`afterPageReads`, Home's too). */
function useSidebar(): SidebarResponse | null {
  const [side, setSide] = useState<SidebarResponse | null>(null);
  const signedIn = useAuth().state.status === "signed-in";
  useEffect(() => {
    // wait until the person is known, so the page's own reads are under way (between the
    // sign-in check's two requests nothing is in flight yet)
    if (!signedIn) return;
    let live = true;
    afterPageReads(SIDEBAR_YIELD_MS)
      .then(() => (live ? loadSidebar() : Promise.reject(new Error("left"))))
      .then((s) => live && setSide(s), () => undefined);
    return () => {
      live = false;
    };
  }, [signedIn]);
  return side;
}

/** True from 1024 px (the sidebar shows); false before mount. */
function useDesktopShell(): boolean | null {
  const [on, setOn] = useState<boolean | null>(null);
  useEffect(() => {
    const m = window.matchMedia("(min-width: 1024px)");
    const f = () => setOn(m.matches);
    f();
    m.addEventListener("change", f);
    return () => m.removeEventListener("change", f);
  }, []);
  return on;
}

function lateOf(home: HomeResponse | null, side: SidebarResponse | null = null): number | null {
  const na = home?.needs_attention;
  if (na?.status === "read") return na.late_total ?? (na.late_pos ?? []).length;
  return side?.late_total ?? null;
}

/** One place in the nav. `ops`: an ops reader only; `admin`: the owner or a feedback admin. */
export interface NavPlace {
  href: string;
  label: string;
  late?: boolean;
  ops?: boolean;
  admin?: boolean;
  /** The owner only (a page whose API refuses everyone else: the Autopilot log). */
  owner?: boolean;
  /** Only for a reader with a team grant (hasTeamGrant, an ops reader or the owner): Team. */
  grant?: boolean;
}

/** A sidebar item: where its own link goes, its tabs (the places drawn as tabs under the
 * page's title, never in the sidebar) and the paths it holds. */
export interface NavSection extends NavPlace {
  key: string;
  /** PlaceIcon's key for the item (its own icon). */
  place: string;
  icon: LucideIcon;
  /** The item's tabs; none or one: the page has no tab strip. */
  children: NavPlace[];
  /** Path prefixes that belong to the item (its tabs' paths are added). */
  holds: string[];
  /** Where the sidebar draws it: the main list, last (Admin), or not at all ("account":
   * Memory, Templates and Work log are in the account menu beside the person's name). */
  group: "main" | "last" | "account";
}

/**
 * The sidebar (the owner, 11-17: "items move depending on the page"; 11-18: "simpler tabs"):
 * one fixed list, the same on every page, the current page highlighted: Home, Ask, Insights,
 * Records, Meetings, Team, then Admin last. A page's sub-pages are tabs under its title
 * (SectionTabs), never sidebar rows. Home is the queue (One engine, one queue)
 * with four tabs: what needs you (the prepared items first), the alerts (was Watching), the
 * late POs (was its own page) and the switches (/autopilot, was its own sidebar entry);
 * /watching, /late and /my-work redirect into it (next.config). Memory, Templates and Work log are in the account menu.
 * Team shows only to a reader with a team grant (the owner, a full-read grant, an ops
 * reader); Admin only to an ops reader or a feedback admin, each of its tabs to the readers
 * it serves.
 */
export const ALERTS_HREF = "/?show=alerts";
export const LATE_HREF = "/?show=late";
export const NAV_SECTIONS: NavSection[] = [
  {
    key: "home", href: "/", label: "Home", place: "/", icon: Inbox, holds: ["/my-work", "/watching", "/late", "/autopilot"], group: "main",
    children: [
      { href: "/", label: "Needs you" },
      { href: ALERTS_HREF, label: "Alerts" },
      { href: LATE_HREF, label: "Late POs", late: true },
      { href: "/autopilot", label: "Switches" },
    ],
  },
  { key: "ask", href: "/ask", label: "Ask", place: "/ask", icon: MessageCircle, holds: [], group: "main", children: [] },
  { key: "insights", href: "/insights", label: "Insights", place: "/insights", icon: ChartColumn, holds: [], group: "main", children: [] },
  {
    key: "records", href: "/records", label: "Records", place: "/records", icon: Database, group: "main",
    holds: ["/customer", "/supplier", "/po", "/ao", "/object", "/source", "/account", "/order"],
    children: [],
  },
  { key: "meetings", href: "/meetings", label: "Meetings", place: "/meetings", icon: CalendarClock, holds: [], group: "main", children: [] },
  { key: "team", href: "/team", label: "Team", place: "/team", icon: Users, holds: [], group: "main", grant: true, children: [] },
  { key: "memory", href: "/memory", label: "Memory", place: "/memory", icon: BookMarked, holds: [], group: "account", children: [] },
  { key: "templates", href: "/templates", label: "Templates", place: "/templates", icon: FileText, holds: [], group: "account", children: [] },
  {
    key: "work-log", href: "/work-log", label: "Work log", place: "/work-log", icon: History, holds: [], group: "account",
    children: [
      { href: "/work-log", label: "Everything" },
      { href: "/activity", label: "Messages and tasks" },
    ],
  },
  {
    key: "admin", href: "/ops", label: "Admin", place: "/admin", icon: Activity, holds: [], group: "last",
    children: [
      { href: "/ops", label: "Reliability", ops: true },
      { href: "/data-health", label: "Data health", ops: true },
      { href: "/trust", label: "Trust", ops: true },
      { href: "/autopilot-log", label: "Autopilot log", ops: true, owner: true },
      { href: "/feedback", label: "Feedback triage", admin: true },
    ],
  },
];

/** Linked at the sidebar's foot (and in the phone's More menu), beside Give feedback. */
export const HELP: NavPlace = { href: "/help", label: "Help" };

type NavReader = (Parameters<typeof hasTeamGrant>[0] & { ops_reader?: boolean; feedback_admin?: boolean; owner?: boolean; roles?: string[] | null }) | null | undefined;

function placeFor(p: NavPlace, me: NavReader): boolean {
  if (p.owner) return !!me?.owner;
  if (p.ops && p.admin) return isOpsReader(me) || !!me?.feedback_admin;
  if (p.ops) return isOpsReader(me);
  if (p.admin) return !!me?.feedback_admin;
  return true;
}

/** The sections this reader sees, each with the places under it they may open; a section
 * with none of its gated places left is dropped, and its link goes to the first place left. */
export function navSectionsFor(me: NavReader): NavSection[] {
  const out: NavSection[] = [];
  for (const s of NAV_SECTIONS) {
    if (s.grant && !(hasTeamGrant(me) || isOpsReader(me) || !!me?.owner)) continue;
    const children = s.children.filter((c) => placeFor(c, me));
    if (s.children.length && !children.length) continue;
    const gated = s.children.length > 0 && s.children.every((c) => c.ops || c.admin);
    out.push({ ...s, href: gated ? children[0].href : s.href, children });
  }
  return out;
}

/** Every place this reader may open from the nav (each section, then the places under it). */
export function navFor(me: NavReader): NavPlace[] {
  const out: NavPlace[] = [];
  for (const s of navSectionsFor(me)) {
    for (const p of [s, ...s.children]) if (!out.some((o) => o.href === p.href)) out.push(p);
  }
  return out;
}

/** The section a path belongs to (null: none, e.g. the sign-in page). `search` (the
 * location's ?query) is checked against a tab's own query when given. */
export function sectionOf(path: string, sections: NavSection[] = NAV_SECTIONS, hash = "", search?: string): NavSection | null {
  const under = (prefix: string) => (prefix === "/" ? path === "/" : path === prefix || path.startsWith(`${prefix}/`));
  // a #fragment tab names its item on that exact fragment; otherwise the page's own item wins
  const exact = sections.find((s) => s.children.some((c) => c.href.includes("#") && isOn(path, c.href, hash, search)));
  if (exact) return exact;
  for (const s of sections) {
    const prefixes = [s.href, ...s.holds, ...s.children.filter((c) => !c.href.includes("#")).map((c) => c.href.split("?")[0])];
    if (prefixes.some(under)) return s;
  }
  for (const s of sections) {
    if (s.children.some((c) => under(c.href.split(/[?#]/)[0]))) return s;
  }
  return null;
}

/** Whether this reader holds a team grant (a declared full-read grant or the owner): Team approvals. */
export function hasTeamGrant(me: { access_label?: string | null; feedback_admin?: boolean } | null | undefined): boolean {
  return !!(me && (me.access_label || me.feedback_admin));
}

/** Whether this reader sees the reliability alarms and /ops: the owner and the Team readers
 * (the server decides; /api/ops/status answers 403 to anyone else). */
export function isOpsReader(me: { ops_reader?: boolean } | null | undefined): boolean {
  return !!me?.ops_reader;
}

/** Phone tabs: the sidebar's first items (Home, Ask, Records, Meetings), in its order,
 * then More for the rest in the same order. */
export const TABS = ["home", "ask", "records", "meetings"];

/** Whether the location (path, #fragment, ?query) is the place `href`: its path (or a page
 * under it), its #fragment when it names one, and every key of its ?query when `search` is
 * given (Home's tabs: /?show=alerts is Alerts, plain / is what needs you). */
function isOn(path: string, href: string, hash = "", search?: string) {
  const [rest, frag] = href.split("#");
  const [base, query] = rest.split("?");
  if (query !== undefined && search !== undefined) {
    const have = new URLSearchParams(search);
    for (const [k, v] of new URLSearchParams(query)) if (have.get(k) !== v) return false;
  }
  if (frag !== undefined) return (base === "/" ? path === "/" : path === base) && hash === `#${frag}`;
  return base === "/" ? path === "/" : path === base || path.startsWith(`${base}/`);
}

/** The location's #fragment, kept current as it changes (a sub-place like /team#workload). */
export function useHash(): string {
  const [hash, setHash] = useState("");
  const path = usePathname();
  useEffect(() => {
    const read = () => setHash(window.location.hash);
    read();
    window.addEventListener("hashchange", read);
    return () => window.removeEventListener("hashchange", read);
  }, [path]);
  return hash;
}

/** The systems each page reads, by route prefix: the "Data as of" line turns amber only for a
 * source the open page uses. Pages that read every system (Ask, records) list them all; a page
 * not listed reads no captured source (Memory, Templates, Help...). */
export const PAGE_SOURCES: [string, readonly string[]][] = [
  ["/ask", ["BC", "Portal", "Sage", "FileMaker", "Email", "Teams", "File", "MC"]],
  ["/records", ["BC", "Portal", "Sage", "FileMaker", "Email", "Teams", "File", "MC"]],
  ["/object", ["BC", "Portal", "Sage", "FileMaker", "Email", "Teams", "File", "MC"]],
  ["/source", ["BC", "Portal", "Sage", "FileMaker", "Email", "Teams", "File", "MC"]],
  ["/customer", ["BC", "Sage", "Portal", "Email", "File"]],
  ["/supplier", ["BC", "Email", "File"]],
  ["/po", ["BC", "Email", "File"]],
  ["/ao", ["BC", "MC"]],
  ["/meetings", ["Teams", "Email"]],
  ["/team", ["Email", "Teams", "BC"]],
  ["/", ["Email", "Teams", "BC", "Sage", "MC"]],
];

/** The systems the page at `path` reads (the longest matching prefix; "/" only for Home itself). */
export function pageSources(path: string): readonly string[] {
  for (const [prefix, systems] of PAGE_SOURCES) {
    if (prefix === "/" ? path === "/" : path === prefix || path.startsWith(`${prefix}/`)) return systems;
  }
  return [];
}

/** "Behind" on the quiet line: the server's own verdict for the source (its age past that
 * system's capture bound, the rule Trust and the answers use), or a capture that failed or
 * never ran. A server that sends no verdict: more than an hour old. */
export const BEHIND_SECONDS = 3600;
type SourceAge = { system: string; age_seconds?: number | null; capture?: string | null; as_of?: string | null; stale?: boolean | null };
export function isBehind(s: SourceAge): boolean {
  if (s.capture === "failed" || s.capture === "never_captured" || !s.as_of) return true;
  return typeof s.stale === "boolean" ? s.stale : (s.age_seconds ?? 0) > BEHIND_SECONDS;
}

/** A source system as the app names it to a person ("Files", "Mail", "Business Central"). */
export const sourceName = (s: string) => (s === "File" ? "Files" : s === "Email" ? "Mail" : (SYSTEM_LABEL as Record<string, string>)[s] ?? s);

/** The one quiet line under the sidebar: "Data as of 10:42 AM ET"; amber, naming them, only when
 * a source the open page reads is more than an hour behind. Each system's time on click; every
 * reliability detail is on the Admin page. */
export function DataAsOf({ home }: { home: { freshness?: HomeResponse["freshness"] } | null }) {
  const path = usePathname() ?? "/";
  const f = home?.freshness;
  if (!f?.as_of) return null;
  const sources = f.sources ?? [];
  const used = new Set(pageSources(path));
  const behind = sources.filter((s) => used.has(s.system) && isBehind(s));
  const late = behind.length > 0;
  return (
    <Menu.Root>
      <Menu.Trigger
        className="group flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left hover:bg-hover max-xl:justify-center max-xl:px-0 cursor-pointer"
        title={late ? `Data as of ${eastern(f.as_of)}; over an hour behind: ${behind.map((s) => sourceName(s.system)).join(", ")}` : `Data as of ${eastern(f.as_of)}`}
        data-testid="home-freshness"
        data-behind={late ? "" : undefined}
      >
        <RefreshCw className={cn("size-[14px] shrink-0", late ? "text-risk" : "text-faint")} strokeWidth={2} aria-hidden />
        <span className={cn("min-w-0 line-clamp-2 text-[12px] leading-4 max-xl:hidden", late ? "text-risk" : "text-muted")}>
          Data as of {eastern(f.as_of)}
          {late && ` · ${behind.map((s) => sourceName(s.system)).join(" and ")} behind`}
        </span>
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Content side="top" align="start" sideOffset={6} className="z-50 w-72 rounded-xl bg-raised p-1.5 shadow-pop" data-testid="sync-detail">
          {sources.map((s) => (
            <div key={s.system} className="flex items-center gap-2.5 rounded-md px-2 py-1.5 text-row">
              <SystemCode system={s.system} size={18} />
              <span className="flex-1 truncate">{sourceName(s.system)}</span>
              <span className={cn("text-meta tabular-nums", isBehind(s) ? "text-fg" : "text-muted")}>
                {s.as_of ? eastern(s.as_of) : "never"}
                {isBehind(s) && " · behind"}
              </span>
            </div>
          ))}
        </Menu.Content>
      </Menu.Portal>
    </Menu.Root>
  );
}

/** Whether a reliability problem is open, for an ops reader only (the Admin item's dot); polled
 * like the Admin banner. Anyone else: never asked, false. */
export function useOpsProblem(ops: boolean, every = 60_000): boolean {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!ops) return setOpen(false);
    let live = true;
    const ctl = new AbortController();
    const load = () =>
      getOpsAlarms(ctl.signal).then(
        (a) => live && setOpen(!!a?.configured && (a.open ?? []).length > 0),
        (e) => {
          if (live && e instanceof ApiError && (e.status === 401 || e.status === 403)) clearInterval(id);
        },
      );
    load();
    const id: ReturnType<typeof setInterval> = setInterval(load, every);
    return () => {
      live = false;
      ctl.abort();
      clearInterval(id);
    };
  }, [ops, every]);
  return open;
}

/** True on the Admin screens (Reliability, Trust, Feedback triage): the only place ops detail shows. */
export function useAdminScreen(): boolean {
  const path = usePathname() ?? "/";
  const hash = useHash();
  return sectionOf(path, NAV_SECTIONS, hash)?.key === "admin";
}

const OPS_LABEL: Record<string, string> = {
  delta_last_cycle: "Last update cycle",
  delta_cycle_runtime: "Running update cycle",
  pg_follow: "Postgres copy",
  proactive_last_pass: "Proactive alerts",
  truth_contradicted_share: "Alerts contradicted",
  ai_watcher_last_run: "AI watcher",
  api_page_p95: "Page time (p95)",
  api_reachable: "API",
  ingest_start: "Update restarts",
  config_keys_ignored: "Ignored settings",
  search_follow_last_pass: "Search index",
  capture_undecided_list: "Mail addresses the directory can't place",
};

const CAPTURE_LABEL: Record<string, string> = {
  bc: "Business Central",
  mail: "Mail",
  mc: "Mission Control",
  meetings: "Meetings",
  portal: "Customer Portal",
  sharepoint: "SharePoint files",
  sharepoint_lists: "SharePoint lists",
  teams: "Teams",
  sage: "Sage",
  filemaker: "FileMaker",
  calendar: "Calendar",
  planner: "Planner",
};

/** "Last update cycle", "SharePoint lists capture"; an unknown name in words, never its raw id. */
export function opsLabel(name: string) {
  const words = (s: string) => {
    const w = s.replace(/[_:-]+/g, " ").trim();
    return w ? w[0].toUpperCase() + w.slice(1) : s;
  };
  if (name.startsWith("capture:")) {
    const sys = name.slice(8);
    return `${CAPTURE_LABEL[sys] ?? words(sys)} capture`;
  }
  return OPS_LABEL[name] ?? words(name);
}

/**
 * An open problem's line in words: a check's raw name in front ("capture_undecided_list: 1
 * undecided address") becomes its label, a service unit ("plx-reply-drafts.timer (system):
 * stopped") its job's name ("Reply drafts schedule: stopped"). The rest of the line, which
 * the check already words, stays as it is.
 */
export function opsLine(key: string, line: string): string {
  const unit = /^(?:plx-)?([a-z0-9-]+)\.(timer|service|path|socket)(?: \((?:system|user)\))?: (.*)$/i.exec(line);
  if (key.startsWith("unit:") && unit) {
    const job = unit[1].replace(/-/g, " ");
    const kind = unit[2] === "timer" ? "schedule" : unit[2] === "service" ? "job" : unit[2];
    return `${job[0].toUpperCase()}${job.slice(1)} ${kind}: ${unit[3]}`;
  }
  const lead = /^([a-z0-9]+(?:[_:][a-z0-9/.-]+)+): (.*)$/.exec(line);
  if (lead && (lead[1] === key || key.startsWith(lead[1]))) return `${opsLabel(lead[1])}: ${lead[2]}`;
  return line;
}

/** 45 min, 5 h, 2 days: the size a reader takes in at a glance. */
function minutes(m: number): string {
  if (m < 120) return `${Math.round(m)} min`;
  if (m < 36 * 60) return `${Math.round(m / 60)} h`;
  return `${Math.round(m / 1440)} days`;
}

export function opsValue(c: OpsCheck) {
  if (c.value === null || typeof c.value === "boolean") return c.detail || "unknown";
  if (c.unit === "share") return `${Math.round(c.value * 100)}% (target ${Math.round(c.target * 100)}%)`;
  if (c.unit === "min") return `${minutes(c.value)} (target ${minutes(c.target)})`;
  return `${c.value} ${c.unit} (target ${c.target})`;
}

export { eastern };

/** What a person calls each captured source, for the one banner line. */
const SOURCE_WORDS: Record<string, string> = {
  mail: "Mail", teams: "Teams messages", portal: "Customer Portal records", bc: "Business Central records",
  mc: "Mission Control", meetings: "Meetings", sharepoint: "SharePoint files", sharepoint_lists: "SharePoint lists",
};
const SINGULAR = new Set(["mail", "mc"]);

/** "40 minutes", "2 hours", "3 days": how far behind, in words. */
function behindBy(hours: number): string {
  if (hours < 1) {
    const m = Math.max(1, Math.round(hours * 60));
    return `${m} minute${m === 1 ? "" : "s"}`;
  }
  if (hours < 48) {
    const h = Math.round(hours);
    return `${h} hour${h === 1 ? "" : "s"}`;
  }
  const d = Math.round(hours / 24);
  return `${d} days`;
}

/** The one plain line a person sees at the top of the page, or null: only problems that change
 * what they see (a source behind or not updating, alerts or records not updating, search behind)
 * and planned maintenance. Everything else (units, disk, memory, Postgres, backups, the job queue,
 * the checks themselves) stays on the Admin page. `calm` marks planned maintenance. */
export function personNotice(alarms: OpsAlarms | null): { text: string; calm: boolean } | null {
  if (!alarms || !alarms.configured) return null;
  const checks = new Map((alarms.checks ?? []).map((c) => [c.name, c]));
  const hoursOf = (key: string): number | null => {
    const c = checks.get(key);
    if (!c || typeof c.value !== "number") return null;
    return c.unit === "h" ? c.value : c.unit === "min" ? c.value / 60 : null;
  };
  // per source the worst: how far behind (hours), or not updating at all (Infinity)
  const behind = new Map<string, number>();
  const note = (source: string, hours: number) => behind.set(source, Math.max(behind.get(source) ?? 0, hours));
  let records = false;
  let alerts = false;
  let search = false;
  for (const i of alarms.open ?? []) {
    const [kind, source] = i.key.split(":");
    if ((kind === "capture_newest" || kind === "served_lag" || kind === "capture") && source && SOURCE_WORDS[source]) {
      note(source, hoursOf(i.key) ?? 0);
    } else if (kind === "capture_failing" && source && SOURCE_WORDS[source]) {
      note(source, Number.POSITIVE_INFINITY);
    } else if (kind === "delta_last_cycle") {
      records = true;
    } else if (kind === "proactive_last_pass" || kind === "proactive_detectors" || /^unit:plx-proactive/.test(i.key)) {
      alerts = true;
    } else if (kind === "search_follow_lag" || kind === "search_follow_last_pass") {
      search = true;
    }
  }
  const parts: string[] = [];
  const sources = [...behind.entries()];
  if (sources.length > 2) {
    const names = sources.map(([s]) => SOURCE_WORDS[s]);
    parts.push(`${names.slice(0, -1).join(", ")} and ${names[names.length - 1]} are behind`);
  } else {
    for (const [s, h] of sources) {
      const name = SOURCE_WORDS[s];
      const are = SINGULAR.has(s) ? "is" : "are";
      parts.push(h === Number.POSITIVE_INFINITY ? `${name} ${are} not updating` : h > 0 ? `${name} ${are} ${behindBy(h)} behind` : `${name} ${are} behind`);
    }
  }
  if (records) parts.push("Records may be out of date");
  // what it means for the person and that it mends itself, never an alarm they can do nothing about
  if (alerts) parts.push("Alerts show the last check's findings; new ones follow when the check runs again");
  if (search) parts.push("Search may not find the newest items yet; it catches up on its own");
  const m = alarms.maintenance;
  if (parts.length === 0 && m?.state === "in_window") {
    return { text: `Planned maintenance until ${eastern(m.until)}: alerts and updates are paused.`, calm: true };
  }
  if (m?.state === "overrun") parts.unshift("Maintenance is taking longer than planned");
  if (parts.length === 0) return null;
  // alerts or search catching up mend themselves: a calm line, never the red one
  const urgent = sources.length > 0 || records || m?.state === "overrun";
  return { text: `${parts.join(". ")}.`, calm: !urgent };
}

/** The one line across the top of every page for an ops reader (the owner and the Team
 * readers): only problems that change what they see, said plainly ("Mail is 2 hours behind"),
 * or one calm line for planned maintenance; linked to Admin, where every check is listed.
 * Nothing for anyone else (the route answers 403) or when all is well. */
export function OpsAlarmBanner({ every = 60_000, className }: { every?: number; className?: string }) {
  const { state } = useAuth();
  const owner = state.status === "signed-in" && isOpsReader(state.me);
  const [alarms, setAlarms] = useState<OpsAlarms | null>(null);
  useEffect(() => {
    if (!owner) return;
    let live = true;
    const ctl = new AbortController();
    const load = () =>
      getOpsAlarms(ctl.signal).then(
        (a) => {
          if (live) setAlarms(a);
        },
        (e) => {
          // a refusal means "not an ops reader": say nothing and stop asking; a failed read keeps
          // the last line (the Admin page says when monitoring cannot be read)
          if (live && e instanceof ApiError && (e.status === 401 || e.status === 403)) clearInterval(id);
        },
      );
    load();
    const id: ReturnType<typeof setInterval> = setInterval(load, every);
    return () => {
      live = false;
      ctl.abort();
      clearInterval(id);
    };
  }, [owner, every]);
  const notice = owner ? personNotice(alarms) : null;
  if (!notice) return null;
  return (
    <Link
      href="/ops"
      role={notice.calm ? "status" : "alert"}
      title="Details on the Admin page"
      className={cn(
        "flex items-center gap-1.5 border-b px-5 py-1 text-[12px] leading-4 hover:underline",
        notice.calm ? "border-border bg-subtle text-muted" : "border-late-line bg-late-soft text-late",
        className,
      )}
      data-testid="ops-alarm-banner"
    >
      {!notice.calm && <TriangleAlert className="size-3 shrink-0" strokeWidth={2} aria-hidden />}
      <span className="min-w-0 truncate">{notice.text}</span>
    </Link>
  );
}

/** The reliability line, on the Admin screens only: everyone else sees the sidebar's quiet "Data as of". */
export function AdminAlarm() {
  return useAdminScreen() ? <OpsAlarmBanner /> : null;
}

function NavItem({ href, label, place, count, countWords = "late", dot, on, onNavigate }: { href: string; label: string; place?: string; count?: number | null; countWords?: string; dot?: string; on?: boolean; onNavigate?: () => void }) {
  const path = usePathname();
  const current = on ?? isOn(path, href);
  return (
    <Link
      href={href}
      onClick={onNavigate}
      onPointerEnter={() => readAheadRoute(href)}
      onFocus={() => readAheadRoute(href)}
      aria-current={current ? "page" : undefined}
      title={label}
      className={cn(
        "group flex h-7 items-center gap-2.5 rounded-r-[2px] border-l-2 px-2 text-[13px] leading-none max-xl:justify-center max-xl:px-0",
        current ? "border-accent bg-nav-on font-medium text-fg" : "border-transparent text-fg/90 hover:bg-hover",
      )}
    >
      <PlaceIcon href={place ?? href} className={current ? undefined : "opacity-85 group-hover:opacity-100"} />
      <span className="flex-1 truncate max-xl:sr-only">{label}</span>
      {typeof count === "number" && count > 0 && (
        <span className="num text-[12px] text-muted max-xl:hidden" aria-label={`${count} ${countWords}`}>
          {count}
        </span>
      )}
      {dot && <span className="size-1.5 shrink-0 rounded-full bg-dot-risk" role="img" aria-label={dot} title={dot} data-testid="admin-dot" />}
    </Link>
  );
}

/** The place under a section that this location is: a #fragment place first, else the
 * place whose ?query the location carries (most keys first), else the longest path match. */
export function currentChild(children: NavPlace[], path: string, hash: string, search = ""): string | null {
  const exact = children.find((c) => c.href.includes("#") && isOn(path, c.href, hash, search));
  if (exact) return exact.href;
  const keys = (href: string) => [...new URLSearchParams(href.split("?")[1] ?? "").keys()].length;
  const plain = children.filter((c) => !c.href.includes("#") && isOn(path, c.href, "", search));
  plain.sort((a, b) => keys(b.href) - keys(a.href) || b.href.length - a.href.length);
  return plain[0]?.href ?? null;
}

/** The sidebar's items in one group: the same rows on every page; only which one is
 * highlighted follows the page (never a row added, moved or dropped by the page). */
function SectionNav({ me, late, group }: { me: NavReader; late: number | null; group: NavSection["group"] }) {
  const path = usePathname();
  const hash = useHash();
  const sections = navSectionsFor(me);
  const open = sectionOf(path, sections, hash);
  const mine = sections.filter((s) => s.group === group);
  const needs = useNeedsCount();
  const problem = useOpsProblem(isOpsReader(me));
  if (!mine.length) return null;
  return (
    <div className={cn("space-y-0.5", group === "last" && "mt-0.5")} data-testid={group === "main" ? "nav-sections" : "nav-last"}>
      {mine.map((s) => (
        <NavItem
          key={s.key}
          href={s.href}
          label={s.label}
          place={s.place}
          on={open?.key === s.key}
          count={s.key === "home" ? needs : s.children.some((c) => c.late) ? late : undefined}
          countWords={s.key === "home" ? "need you" : "late"}
          dot={s.key === "admin" && problem ? "A reliability problem is open: see Admin" : undefined}
        />
      ))}
    </div>
  );
}

/** A sidebar item's tabs, drawn by each of its pages under the title at every width (Home:
 * Needs you, Alerts, Late POs; Work log: Everything, Messages and tasks; Admin: Reliability,
 * Data health, Trust, Autopilot log, Feedback triage). */
export function SectionTabs(props: { section: string; className?: string }) {
  // the ?query names Home's tab; until it is read (a static render) no tab is marked
  return (
    <Suspense fallback={<SectionTabsAt {...props} search={null} />}>
      <SectionTabsHere {...props} />
    </Suspense>
  );
}

function SectionTabsHere(props: { section: string; className?: string }) {
  const params = useSearchParams();
  return <SectionTabsAt {...props} search={params?.toString() ?? ""} />;
}

function SectionTabsAt({ section, className, search }: { section: string; className?: string; search: string | null }) {
  const { state } = useAuth();
  const me = state.status === "signed-in" ? state.me : null;
  const path = usePathname() ?? "";
  const hash = useHash();
  const s = navSectionsFor(me).find((x) => x.key === section);
  if (!s || s.children.length < 2) return null;
  const current = search === null ? null : currentChild(s.children, path, hash, search);
  return (
    <nav aria-label={s.label} className={cn("flex h-10 shrink-0 items-stretch gap-5 overflow-x-auto border-b border-border px-4 [scrollbar-width:none] lg:px-5 [&::-webkit-scrollbar]:hidden", className)} data-testid={`section-tabs-${s.key}`}>
      {s.children.map((c) => {
        const on = current === c.href;
        return (
          <Link
            key={c.href}
            href={c.href}
            onPointerEnter={() => readAheadRoute(c.href)}
            aria-current={on ? "page" : undefined}
            className={cn("relative flex shrink-0 items-center whitespace-nowrap text-[13px]", on ? "font-medium text-fg after:absolute after:inset-x-0 after:bottom-0 after:h-0.5 after:bg-accent" : "text-muted hover:text-fg")}
          >
            {c.label}
          </Link>
        );
      })}
    </nav>
  );
}

/** The pins the sidebar lists: a pin to a place the nav already names (an item or one of its
 * tabs, the #fragment aside) is not repeated (the owner, 11-17: "Approvals", "Autopilot
 * approvals" and "Alerts" were there twice; 11-18: simpler). A pin with its own filter
 * (?for=purchasing) is a view of its own and stays. */
export function sidebarPins(pins: PinnedPage[], me: NavReader): PinnedPage[] {
  const fixed = new Set([...navSectionsFor(me).flatMap((s) => [s.href, ...s.children.map((c) => c.href.split("#")[0])]), HELP.href]);
  return pins.filter((p) => !fixed.has(p.href.split("#")[0]));
}

/** The person's pinned pages in the sidebar (their role's defaults until they change them). */
function PinnedNav({ me }: { me: (PinReader & NonNullable<NavReader>) | null }) {
  const { pins: all, defaults } = usePins(me);
  const pins = sidebarPins(all, me);
  if (!pins.length) return null;
  return (
    <SidebarSection title={defaults ? "Pinned for your role" : "Pinned"}>
      <div className="space-y-px" data-testid="nav-pinned">
        {pins.slice(0, 8).map((p) => (
          <Link key={p.href} href={p.href} className="flex h-8 items-center gap-2.5 rounded-md px-2 text-[13px] text-fg/90 hover:bg-hover">
            <span className="flex size-5 shrink-0 items-center justify-center" aria-hidden>
              <Pin className="size-3.5 text-muted" strokeWidth={2} />
            </span>
            <span className="truncate">{p.label}</span>
          </Link>
        ))}
      </div>
    </SidebarSection>
  );
}

/** The suppliers with late POs, most first: each opens Today showing only that supplier's. */
export function LateBySupplier() {
  const side = useSidebar();
  const path = usePathname();
  const params = useSearchParams();
  const rows = side?.late_suppliers ?? [];
  if (!rows.length) return null;
  const shown = rows.slice(0, 7);
  const more = Math.max(0, (side?.supplier_total ?? rows.length) - shown.length);
  const current = path === "/" && !params.get("show") ? params.get("supplier") : null;
  return (
    <SidebarSection title="Late by supplier">
      <div className="space-y-px" data-testid="late-by-supplier">
        {side?.partial && (
          <Link href={LATE_HREF} className="block px-2 pb-1 text-[12px] leading-4 text-muted hover:text-fg" data-testid="late-by-supplier-partial">
            Counts from the longest late orders only; all are on Late orders
          </Link>
        )}
        {shown.map(({ supplier: name, late: n }) => {
          const on = current === name;
          return (
            <Link
              key={name}
              href={on ? "/" : `/?supplier=${encodeURIComponent(name)}`}
              aria-current={on ? "true" : undefined}
              className={cn("flex h-8 items-center gap-2.5 rounded-md px-2 text-[13px]", on ? "bg-nav-on font-medium" : "text-fg/90 hover:bg-hover")}
            >
              <span className="flex-1 truncate">{name}</span>
              <span className="num text-[12px] text-muted">{n}</span>
            </Link>
          );
        })}
        {more > 0 && (
          <Link href={LATE_HREF} className="flex h-7 items-center rounded-md px-2 pl-[40px] text-[12px] text-muted hover:text-fg">
            {more} more
          </Link>
        )}
      </div>
    </SidebarSection>
  );
}

/** The sidebar's places: the main items, Yours, Admin and the pins. The same rows in the
 * same order on every page; the page only decides which row is highlighted. */
export function SidebarNav({ me, late }: { me: (PinReader & NonNullable<NavReader>) | null; late: number | null }) {
  return (
    <>
      <SectionNav me={me} late={late} group="main" />
      <SectionNav me={me} late={late} group="last" />
      <PinnedNav me={me} />
    </>
  );
}

function SidebarSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mt-6 max-xl:hidden">
      <div className="px-2 pb-1.5 text-[11px] font-medium leading-4 tracking-[0.01em] text-faint">{title}</div>
      {children}
    </div>
  );
}

function Sidebar() {
  const home = useHomeRead();
  const side = useSidebar();
  const saved = useSaved();
  const router = useRouter();
  const { open } = useCommandPalette();
  const { shortcut } = useShortcut();
  const late = lateOf(home, side);
  const { state } = useAuth();
  const signedIn = state.status === "signed-in" ? state.me : null;
  const savedOnly = saved.filter((s) => s.kind !== "page");
  const recent = (side?.recent_questions ?? home?.recent_questions ?? []).filter(
    (r, i, all) => all.findIndex((x) => x.question.trim().toLowerCase() === r.question.trim().toLowerCase()) === i,
  );
  return (
    <aside className="hidden w-[60px] shrink-0 flex-col border-r border-border bg-sidebar lg:flex xl:w-[240px]" aria-label="Sidebar">
      <div className="flex h-[52px] shrink-0 items-center justify-center xl:justify-start xl:px-5">
        <Link href="/" aria-label="PLX Workbench, Home">
          <Logo className="max-xl:[&>[data-wm=rest]]:hidden max-xl:text-[16px]" />
        </Link>
      </div>
      <div className="px-3">
        <button
          type="button"
          onClick={open}
          title={`Search or ask (${shortcut("K")})`}
          className="flex h-8 w-full items-center gap-2.5 rounded-md bg-[color-mix(in_srgb,var(--fg)_5%,transparent)] px-2 text-left text-[13px] text-faint hover:bg-[color-mix(in_srgb,var(--fg)_8%,transparent)] hover:text-muted max-xl:justify-center max-xl:px-0 cursor-pointer"
          data-testid="global-search"
          aria-label={`Search records or ask (${shortcut("K")})`}
        >
          <span className="flex size-5 shrink-0 items-center justify-center" aria-hidden>
            <Search className="size-[15px]" strokeWidth={2} />
          </span>
          <span className="flex-1 truncate max-xl:hidden">Search or ask</span>
          <kbd className="font-sans text-[11px] leading-4 text-faint max-xl:hidden" aria-hidden>
            {shortcut("K")}
          </kbd>
        </button>
      </div>
      <nav aria-label="Main" className="mt-3 flex min-h-0 flex-1 flex-col overflow-y-auto px-3 pb-3">
        <SidebarNav me={signedIn} late={late} />
        <Suspense>
          <LateBySupplier />
        </Suspense>
        {savedOnly.length > 0 && (
          <SidebarSection title="Saved">
            <div className="space-y-px">
              {savedOnly.slice(0, 6).map((s) => (
                <Link key={s.key} href={s.href} className="flex h-8 items-center gap-2.5 rounded-md px-2 text-[13px] text-fg/90 hover:bg-hover">
                  <span className="flex size-5 shrink-0 items-center justify-center" aria-hidden>
                    <Bookmark className="size-3.5 text-muted" strokeWidth={2} />
                  </span>
                  <span className="truncate">{s.label}</span>
                </Link>
              ))}
            </div>
          </SidebarSection>
        )}
        {recent.length > 0 && (
          <SidebarSection title="Recent questions">
            <div className="space-y-px">
              {recent.slice(0, 4).map((r) => (
                <button
                  key={r.answer_id ?? r.question}
                  type="button"
                  title={r.question}
                  onClick={() => {
                    stashPending(r.question, []);
                    router.push(`/ask?q=${encodeURIComponent(r.question)}`);
                  }}
                  className="flex h-8 w-full items-center rounded-md px-2 text-left text-[13px] text-muted hover:bg-hover hover:text-fg cursor-pointer"
                >
                  <span className="truncate first-letter:uppercase">{r.question}</span>
                </button>
              ))}
            </div>
          </SidebarSection>
        )}
      </nav>
      <div className="shrink-0 space-y-0.5 border-t border-border px-3 py-2.5">
        <FeedbackButton />
        <HelpLink />
        <DataAsOf home={side ?? home} />
        <UserMenu />
      </div>
    </aside>
  );
}

/** Help, at the sidebar's foot on every page: the first-week guide for the reader's role. */
function HelpLink() {
  const path = usePathname() ?? "";
  const on = isOn(path, HELP.href);
  return (
    <Link
      href={HELP.href}
      aria-current={on ? "page" : undefined}
      className={cn("flex h-8 w-full items-center gap-2.5 rounded-md px-2 text-[13px] max-xl:justify-center max-xl:px-0", on ? "bg-nav-on font-medium text-fg" : "text-fg/90 hover:bg-hover")}
      title="Help"
      data-testid="nav-help"
    >
      <span className="flex size-5 shrink-0 items-center justify-center" aria-hidden>
        <CircleHelp className="size-[15px] text-muted" strokeWidth={2} />
      </span>
      <span className="flex-1 truncate max-xl:sr-only">Help</span>
    </Link>
  );
}

function initialsOf(name: string) {
  return name
    .split(/\s+/)
    .map((p) => p[0])
    .filter(Boolean)
    .slice(0, 2)
    .join("")
    .toUpperCase();
}

function Avatar({ name, className }: { name: string; className?: string }) {
  return (
    <span
      className={cn("flex size-6 shrink-0 items-center justify-center rounded-[3px] bg-surface-2 text-[10px] font-semibold text-muted", className)}
      aria-hidden
    >
      {initialsOf(name) || "?"}
    </span>
  );
}

function AccountMenuContent({ name, email, onViewAs }: { name: string; email?: string | null; onViewAs?: () => void }) {
  const { signOut } = useAuth();
  const router = useRouter();
  const feedback = useFeedbackDialog();
  return (
    <Menu.Portal>
      <Menu.Content side="top" align="start" sideOffset={6} collisionPadding={8} className="z-50 w-64 rounded-xl bg-raised p-1.5 shadow-pop">
        <div className="px-2 py-1.5">
          <p className="truncate text-row font-medium">{name}</p>
          {email && <p className="truncate text-meta text-muted">{email}</p>}
        </div>
        <AccessBanner className="px-2 py-1.5" />
        <DevBanner className="px-2 py-1.5" />
        <div className="flex items-center justify-between px-2 py-1.5">
          <span className="text-meta text-muted">Appearance</span>
          <ThemeToggle />
        </div>
        <Menu.Separator className="mx-2 my-1 h-px bg-border" />
        <Menu.Item onSelect={feedback.open} className="flex cursor-pointer items-center rounded-sm px-2 py-1.5 text-row outline-none data-[highlighted]:bg-hover">
          Give feedback
        </Menu.Item>
        <Menu.Item onSelect={() => router.push("/memory")} className="flex cursor-pointer items-center rounded-sm px-2 py-1.5 text-row outline-none data-[highlighted]:bg-hover" data-testid="memory-open">
          Memory
        </Menu.Item>
        <Menu.Item onSelect={() => router.push("/templates")} className="flex cursor-pointer items-center rounded-sm px-2 py-1.5 text-row outline-none data-[highlighted]:bg-hover" data-testid="templates-open">
          Templates
        </Menu.Item>
        <Menu.Item onSelect={() => router.push("/work-log")} className="flex cursor-pointer items-center rounded-sm px-2 py-1.5 text-row outline-none data-[highlighted]:bg-hover" data-testid="work-log-open">
          Work log
        </Menu.Item>
        <Menu.Item onSelect={() => router.push("/feedback")} className="flex cursor-pointer items-center rounded-sm px-2 py-1.5 text-row outline-none data-[highlighted]:bg-hover">
          Feedback list
        </Menu.Item>
        <Menu.Item onSelect={() => router.push("/help")} className="flex cursor-pointer items-center rounded-sm px-2 py-1.5 text-row outline-none data-[highlighted]:bg-hover" data-testid="help-open">
          Help for your first week
        </Menu.Item>
        {onViewAs && (
          <Menu.Item onSelect={onViewAs} className="flex cursor-pointer items-center rounded-sm px-2 py-1.5 text-row outline-none data-[highlighted]:bg-hover" data-testid="view-as-open">
            View as someone
          </Menu.Item>
        )}
        <Menu.Item
          onSelect={async () => {
            if (await signOut()) router.replace("/signin");
          }}
          className="flex cursor-pointer items-center rounded-sm px-2 py-1.5 text-row outline-none data-[highlighted]:bg-hover"
        >
          Sign out
        </Menu.Item>
      </Menu.Content>
    </Menu.Portal>
  );
}

function UserMenu() {
  const { state } = useAuth();
  const { dev, link } = useBanner();
  const desktop = useDesktopShell();
  const [picking, setPicking] = useState(false);
  if (state.status !== "signed-in") return null;
  const name = state.me.display_name || "Account";
  return (
    <>
    {state.me.can_view_as && picking && (
      <Suspense fallback={null}>
        <ViewAsPicker open={picking} onOpenChange={setPicking} />
      </Suspense>
    )}
    <Menu.Root>
      <Menu.Trigger aria-label={`Account: ${name}`} className="flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left hover:bg-hover max-xl:justify-center max-xl:px-0 cursor-pointer">
        <Avatar name={name} className="size-5 text-[9px]" />
        <span className="min-w-0 flex-1 max-xl:hidden">
          <span className="block truncate text-[13px] font-medium leading-4">{name}</span>
          {(dev || link) && desktop && <DevBanner compact className="truncate text-[11px] leading-4" />}
        </span>
        <MoreHorizontal className="size-4 shrink-0 text-faint max-xl:hidden" aria-hidden />
      </Menu.Trigger>
      <AccountMenuContent name={name} email={state.me.email} onViewAs={state.me.can_view_as ? () => setPicking(true) : undefined} />
    </Menu.Root>
    </>
  );
}

/** Phone: the tab bar along the bottom, as an iPhone app has it. */
export function TabBar() {
  const path = usePathname();
  const home = useHomeRead();
  const late = lateOf(home, useSidebar());
  const { state } = useAuth();
  const reader = state.status === "signed-in" ? state.me : null;
  const sections = navSectionsFor(reader);
  const open = sectionOf(path, sections);
  const tabs = TABS.map((key) => sections.find((s) => s.key === key)).filter((s): s is NavSection => !!s)
    .map((s) => ({ ...s, late: s.children.some((c) => c.late) }));
  // More: every other section, and each place under a section that is not its own link
  const rest = sections.flatMap((s) => (TABS.includes(s.key) ? s.children.filter((c) => c.href !== s.href).map((c) => ({ ...c, section: s })) : [s, ...s.children.filter((c) => c.href !== s.href).map((c) => ({ ...c, section: s }))]))
    .map((p) => ({ href: p.href, label: p.label, icon: ("icon" in p && p.icon) || ("section" in p ? p.section.icon : Inbox), sub: "section" in p }))
    .concat([{ href: HELP.href, label: HELP.label, icon: CircleHelp, sub: false }]);
  const moreOn = (!!open && !TABS.includes(open.key)) || isOn(path ?? "", HELP.href);
  return (
    <nav
      aria-label="Main"
      className="fixed inset-x-0 bottom-0 z-30 border-t border-border bg-bg-subtle pb-[env(safe-area-inset-bottom)] lg:hidden"
      data-testid="tab-bar"
    >
      <div className="mx-auto grid h-[52px] max-w-md grid-cols-5">
        {tabs.map(({ key, href, label, icon: Icon, late: showLate }) => {
          const on = open?.key === key;
          return (
            <Link key={href} href={href} onPointerDown={() => readAheadRoute(href)} aria-current={on ? "page" : undefined} className={cn("relative flex flex-col items-center justify-center gap-0.5 text-[10px] font-medium", on ? "text-accent" : "text-muted")}>
              <Icon className="size-[22px]" strokeWidth={on ? 2 : 1.7} aria-hidden />
              {label}
              {showLate && typeof late === "number" && late > 0 && <span className="sr-only">, {late} late</span>}
            </Link>
          );
        })}
        <Menu.Root>
          <Menu.Trigger aria-label="More" className={cn("relative flex flex-col items-center justify-center gap-0.5 text-[10px] font-medium cursor-pointer", moreOn ? "text-accent" : "text-muted")} data-testid="tab-more">
            <MoreHorizontal className="size-[22px]" strokeWidth={moreOn ? 2 : 1.7} aria-hidden />
            More
          </Menu.Trigger>
          <Menu.Portal>
            <Menu.Content side="top" align="end" sideOffset={8} collisionPadding={8} className="z-50 max-h-[70dvh] w-56 overflow-y-auto rounded-xl bg-raised p-1.5 shadow-pop" data-testid="more-menu">
              {rest.map(({ href, label, icon: Icon, sub }) => (
                <Menu.Item key={href} asChild>
                  <Link href={href} className={cn("flex items-center gap-2.5 rounded-md px-2 py-2 text-row outline-none data-[highlighted]:bg-hover", sub && "pl-8 text-muted")}>
                    {!sub && <Icon className="size-4 text-muted" aria-hidden />}
                    {label}
                  </Link>
                </Menu.Item>
              ))}
            </Menu.Content>
          </Menu.Portal>
        </Menu.Root>
      </div>
    </nav>
  );
}

/** Phone: the account button for a page's title bar. */
/** Phone: the account button; `quiet` (a title bar full of actions) keeps the demo-sign-in word for wider screens and the menu. */
export function MobileAccount({ quiet }: { quiet?: boolean } = {}) {
  const { state } = useAuth();
  const desktop = useDesktopShell();
  if (state.status !== "signed-in") return null;
  const name = state.me.display_name || "Account";
  return (
    <div className="flex items-center gap-1 lg:hidden">
      {desktop === false && <DevBanner compact className={cn("shrink-0 whitespace-nowrap text-[11px] leading-4", quiet && "max-sm:hidden")} />}
      <Menu.Root>
        <Menu.Trigger aria-label={`Account: ${name}`} className="flex size-9 items-center justify-center rounded cursor-pointer">
          <Avatar name={name} className="size-7 text-[11px]" />
        </Menu.Trigger>
        <AccountMenuContent name={name} email={state.me.email} />
      </Menu.Root>
    </div>
  );
}

/** Phone: the search button for a page's title bar. */
export function MobileSearch() {
  const { open } = useCommandPalette();
  return (
    <button type="button" onClick={open} aria-label="Search or ask" className="flex size-9 items-center justify-center rounded text-accent hover:bg-hover lg:hidden cursor-pointer">
      <Search className="size-5" strokeWidth={2} aria-hidden />
    </button>
  );
}

/** A page's title bar on a phone, for a page that has no Toolbar of its own yet. */
function MobileTitleBar() {
  const path = usePathname();
  if (path === "/" || TOOLBAR_PAGES.some((p) => path === p || path.startsWith(`${p}/`))) return null;
  return (
    <header className="sticky top-0 z-30 flex h-12 shrink-0 items-center gap-2 border-b border-border bg-bg-subtle px-4 lg:hidden">
      <Link href="/" aria-label="Home">
        <Logo />
      </Link>
      <div className="flex-1" />
      <MobileSearch />
      <MobileAccount />
    </header>
  );
}

/** Pages that draw their own Toolbar; the shell adds no phone title bar over them.*/
export const TOOLBAR_PAGES: string[] = ["/meetings", "/team", "/ask", "/po", "/ao", "/customer", "/supplier", "/object", "/source", "/activity", "/feedback", "/autopilot", "/autopilot-log", "/ops", "/trust", "/work-log", "/templates", "/memory", "/help", "/records", "/account", "/order", "/insights", "/data-health"];

/** What the content area holds until the sign-in is read: drawn in the server's HTML too, so a
 * slow first load never shows an empty page (it fades in after 600 ms, so a fast one never flickers). */
function ShellSkeleton() {
  return (
    <div className="mx-auto w-full max-w-[1200px] px-4 pt-8 sm:px-8 animate-fade-in [animation-delay:600ms]" data-testid="shell-opening">
      <Pending page label="Opening" />
    </div>
  );
}

/**
 * The window: a sidebar and one content area that fills the rest. The window never scrolls;
 * each page scrolls inside its own panes (Today) or inside the content area.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const { state, refresh, signOut, signOutError } = useAuth();
  const router = useRouter();
  return (
    <CommandPaletteProvider>
      <FeedbackProvider>
      <DesktopBridge />
      <div className="flex h-dvh overflow-hidden bg-bg">
        <a
          href="#main"
          className="sr-only z-50 rounded-sm bg-accent px-3 py-2 text-accent-fg focus:not-sr-only focus:fixed focus:left-3 focus:top-3"
        >
          Skip to content
        </a>
        <Sidebar />
        <main id="main" className="relative flex min-w-0 flex-1 flex-col overflow-y-auto max-lg:[&>*:not([data-own-scroll])]:basis-auto max-lg:[&>*:not([data-own-scroll])]:shrink-0 pb-[calc(52px+env(safe-area-inset-bottom))] lg:pb-0" data-app-main>
          <MobileTitleBar />
          <ViewAsBar />
          <AdminAlarm />
          {signOutError && (
            <div role="alert" className="border-b border-late-line bg-late-soft px-5 py-1.5 text-[13px] leading-5 text-late" data-testid="sign-out-failed">
              {signOutError}{" "}
              <button type="button" className="font-medium underline cursor-pointer" onClick={() => void signOut().then((ok) => ok && router.replace("/signin"))}>
                Try again
              </button>
            </div>
          )}
          {state.status === "error" ? (
            <div className="mx-auto w-full max-w-xl p-6">
              <ErrorNotice message={state.message} onRetry={refresh} />
            </div>
          ) : (
            <RequireAuth fallback={<ShellSkeleton />}>{children}</RequireAuth>
          )}
        </main>
        <TabBar />
      </div>
      </FeedbackProvider>
    </CommandPaletteProvider>
  );
}
