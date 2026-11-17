"use client";

import * as Menu from "@radix-ui/react-dropdown-menu";
import { Activity, BellRing, Bookmark, Bot, Briefcase, Database, Inbox, List, MessageCircle, MoreHorizontal, Pin, RefreshCw, Search, TriangleAlert, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { lazy, Suspense, useEffect, useState } from "react";
import { getHome, getLateList, getOpsAlarms, getOpsHealth, type OpsAlarms, type OpsCheck, type OpsHealth } from "@/lib/api";
import { DesktopBridge } from "./desktop-bridge";
import { CommandPaletteProvider, useCommandPalette } from "./command-palette";
import { LISTS as ALL_LISTS, listsFor } from "@/lib/lists";
import { FeedbackButton, FeedbackProvider, useFeedbackDialog } from "./feedback-dialog";
import { eastern, opsHeadline, plainServerNote, SYSTEM_LABEL } from "@/lib/format";
import { cn } from "@/lib/cn";
import { stashPending } from "@/lib/pending";
import { useShortcut } from "@/lib/platform";
import { useSaved } from "@/lib/saved";
import { usePins, type PinReader } from "@/lib/pins";
import { readAllLate } from "@/lib/late";
import { readAheadRoute } from "@/lib/route-reads";
import { onSessionReset } from "@/lib/session-cache";
import type { HomeResponse, LatePo } from "@/lib/types";
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
        <span className={compact ? "text-faint" : undefined}>{compact ? "Access link" : "Signed in with your access link"}</span>
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
/** The late POs you may see (up to HOME_LATE_MOST, a page or three), read once per page load: Today's list and the sidebar's suppliers. `total` says when that is only part. */
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

let homeOnce: Promise<HomeResponse> | null = null;
const homeListeners = new Set<(h: HomeResponse) => void>();
// read for the person signed in: dropped when they sign out or someone else signs in
onSessionReset(() => {
  lateOnce = null;
  homeOnce = null;
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

function useHome(): HomeResponse | null {
  const [home, setHome] = useState<HomeResponse | null>(null);
  useEffect(() => {
    let live = true;
    loadHome().then((h) => live && setHome(h), () => undefined);
    const off = onHomeRead((h) => live && setHome(h));
    return () => {
      live = false;
      off();
    };
  }, []);
  return home;
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

function lateOf(home: HomeResponse | null): number | null {
  const na = home?.needs_attention;
  return na?.status === "read" ? (na.late_total ?? (na.late_pos ?? []).length) : null;
}

/** One place in the nav. `ops`: an ops reader only; `admin`: the owner or a feedback admin. */
export interface NavPlace {
  href: string;
  label: string;
  late?: boolean;
  ops?: boolean;
  admin?: boolean;
  /** Quality and production-planning readers, ops readers and the owner (seesQuality). */
  quality?: boolean;
}

/** A top-level section: where its own link goes, the places under it, and the paths it holds. */
export interface NavSection extends NavPlace {
  key: string;
  /** PlaceIcon's key for the section (its own icon). */
  place: string;
  icon: LucideIcon;
  children: NavPlace[];
  /** Path prefixes that belong to the section (its children's paths are added). */
  holds: string[];
}

/**
 * The places, in six sections and Records. Every old page is still here, under the section it
 * belongs to; nothing was removed and every old URL still opens. Team shows to every reader
 * (their own row at least); Admin only to an ops reader or a feedback admin, each of its
 * places to the readers it serves.
 */
export const NAV_SECTIONS: NavSection[] = [
  {
    key: "home", href: "/", label: "Home", place: "/", icon: Inbox, holds: ["/my-work", "/activity"],
    children: [
      { href: "/", label: "My work" },
      { href: "/my-work", label: "Prepared for you" },
      { href: "/activity", label: "Your actions" },
    ],
  },
  {
    key: "ask", href: "/ask", label: "Ask", place: "/ask", icon: MessageCircle, holds: ["/help"],
    children: [
      { href: "/ask", label: "Ask" },
      { href: "/memory", label: "Memory" },
      { href: "/templates", label: "Templates" },
    ],
  },
  {
    key: "watching", href: "/watching", label: "Watching", place: "/watching", icon: BellRing, holds: [],
    children: [
      { href: "/watching", label: "Alerts" },
      { href: "/late", label: "Late POs", late: true },
      { href: "/watching#data-health", label: "Data health" },
      { href: "/qc", label: "Quality", quality: true },
    ],
  },
  {
    key: "autopilot", href: "/autopilot", label: "Autopilot", place: "/autopilot", icon: Bot, holds: [],
    children: [
      { href: "/autopilot", label: "Autopilot" },
      { href: "/team#approvals", label: "Approvals" },
    ],
  },
  {
    key: "work", href: "/meetings", label: "Work", place: "/work", icon: Briefcase, holds: [],
    children: [
      { href: "/meetings", label: "Meetings" },
      { href: "/work-log", label: "Work log" },
      { href: "/team", label: "Team" },
      { href: "/team#workload", label: "Workload" },
    ],
  },
  {
    key: "records", href: "/records", label: "Records", place: "/records", icon: Database,
    holds: ["/customer", "/supplier", "/po", "/ao", "/object", "/source"],
    children: [],
  },
  {
    key: "admin", href: "/ops", label: "Admin", place: "/admin", icon: Activity, holds: [],
    children: [
      { href: "/ops", label: "Reliability", ops: true },
      { href: "/trust", label: "Trust", ops: true },
      { href: "/feedback", label: "Feedback triage", admin: true },
    ],
  },
];

type NavReader = (Parameters<typeof hasTeamGrant>[0] & { ops_reader?: boolean; feedback_admin?: boolean; owner?: boolean; roles?: string[] | null }) | null | undefined;

/** Whether the QC packs are for this reader: quality or production planning by role, an ops
 * reader or the owner. The nav's gate only; the server shows the lines to quality_controlled readers. */
export function seesQuality(me: NavReader): boolean {
  return !!me && (isOpsReader(me) || !!me.owner || (me.roles ?? []).some((r) => r === "quality" || r === "production_planning"));
}

function placeFor(p: NavPlace, me: NavReader): boolean {
  if (p.quality && !seesQuality(me)) return false;
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
  for (const p of listsFor(me)) if (!out.some((o) => o.href === p.href)) out.push(p);
  return out;
}

/** The section a path belongs to (null: none, e.g. the sign-in page). */
export function sectionOf(path: string, sections: NavSection[] = NAV_SECTIONS, hash = ""): NavSection | null {
  const under = (prefix: string) => (prefix === "/" ? path === "/" : path === prefix || path.startsWith(`${prefix}/`));
  // a #fragment place names its section only on that exact fragment (/team#approvals is
  // Autopilot's); otherwise the page's own section wins (/team is Work's, not Autopilot's)
  const exact = sections.find((s) => s.children.some((c) => c.href.includes("#") && isOn(path, c.href, hash)));
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

/** Phone tabs: Home, Ask, Watching, Work, then More for the rest. */
export const TABS = ["home", "ask", "watching", "work"];

function isOn(path: string, href: string, hash = "") {
  const [base, frag] = href.split("#");
  if (frag !== undefined) return (base === "/" ? path === "/" : path === base) && hash === `#${frag}`;
  return href === "/" ? path === "/" : path === href || path.startsWith(`${href}/`);
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

/** The sidebar's sync line: when the data was read, the systems that are behind, each system on click. */
function SyncStatus({ home }: { home: HomeResponse | null }) {
  const f = home?.freshness;
  if (!f?.as_of) return null;
  const sources = f.sources ?? [];
  const behind = sources.filter((s) => s.stale || s.capture === "failed");
  const name = (s: string) => (s === "File" ? "Files" : s === "Email" ? "Mail" : (SYSTEM_LABEL as Record<string, string>)[s] ?? s);
  return (
    <Menu.Root>
      <Menu.Trigger
        className="group flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left hover:bg-hover max-xl:justify-center max-xl:px-0 cursor-pointer"
        title={`Updated ${eastern(f.as_of)}`}
        data-testid="home-freshness"
      >
        <RefreshCw className={cn("size-[14px] shrink-0", behind.length ? "text-faint" : "text-live")} strokeWidth={2} aria-hidden />
        <span className="min-w-0 text-[12px] leading-4 max-xl:hidden">
          <span className="block text-muted">Updated {eastern(f.as_of)}</span>
          {behind.length > 0 && <span className="block truncate text-faint">{behind.map((s) => name(s.system)).join(" and ")} behind</span>}
        </span>
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Content side="top" align="start" sideOffset={6} className="z-50 w-72 rounded-xl bg-raised p-1.5 shadow-pop" data-testid="sync-detail">
          {sources.map((s) => (
            <div key={s.system} className="flex items-center gap-2.5 rounded-md px-2 py-1.5 text-row">
              <SystemCode system={s.system} size={18} />
              <span className="flex-1 truncate">{name(s.system)}</span>
              {!(s.stale || s.capture === "failed") && s.as_of && <span className="size-1.5 shrink-0 bg-live" aria-hidden data-testid="fresh-mark" />}
              <span className={cn("text-meta tabular-nums", s.stale || s.capture === "failed" ? "text-fg" : "text-muted")}>
                {s.as_of ? eastern(s.as_of) : "never"}
                {(s.stale || s.capture === "failed") && " · behind"}
              </span>
            </div>
          ))}
        </Menu.Content>
      </Menu.Portal>
    </Menu.Root>
  );
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

/** The ops line: any freshness target breached (delta, captures, Postgres, proactive,
 * truth check, AI watcher, page time), polled from the public health route. Shown only to an
 * ops reader; nothing when every target holds. */
export function OpsStatus({ every = 60_000 }: { every?: number }) {
  const { state } = useAuth();
  const owner = state.status === "signed-in" && isOpsReader(state.me);
  const [ops, setOps] = useState<OpsHealth | null>(null);
  useEffect(() => {
    if (!owner) return;
    let live = true;
    const ctl = new AbortController();
    const load = () => getOpsHealth(ctl.signal).then((o) => live && setOps(o)).catch(() => undefined);
    load();
    const id = setInterval(load, every);
    return () => {
      live = false;
      ctl.abort();
      clearInterval(id);
    };
  }, [owner, every]);
  if (!owner || !ops || ops.breaches.length === 0) return null;
  const breached = ops.checks.filter((c) => c.state === "breach");
  return (
    <Menu.Root>
      <Menu.Trigger
        className="group flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left hover:bg-hover max-xl:justify-center max-xl:px-0 cursor-pointer"
        title={`${breached.length} ops target${breached.length === 1 ? "" : "s"} missed`}
        data-testid="ops-status"
      >
        <TriangleAlert className="size-[14px] shrink-0 text-fg" strokeWidth={2} aria-hidden />
        <span className="min-w-0 text-[12px] leading-4 max-xl:hidden">
          <span className="block text-fg">{breached.length} ops target{breached.length === 1 ? "" : "s"} missed</span>
          <span className="block truncate text-faint">{breached.map((c) => opsLabel(c.name)).join(", ")}</span>
        </span>
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Content side="top" align="start" sideOffset={6} className="z-50 w-80 rounded-xl bg-raised p-1.5 shadow-pop" data-testid="ops-detail">
          {breached.map((c) => (
            <div key={c.name} className="flex items-center gap-2.5 rounded-md px-2 py-1.5 text-row">
              <span className="flex-1 truncate">{opsLabel(c.name)}</span>
              <span className="text-meta tabular-nums text-fg">{opsValue(c)}</span>
            </div>
          ))}
        </Menu.Content>
      </Menu.Portal>
    </Menu.Root>
  );
}

export { eastern };

/** The red alarm line across the top of every page, the only place alarms reach anyone (no
 * paging): open reliability incidents (a stopped refresh, a failed or
 * OOM-killed unit, Postgres, disk, memory, backups), or the checks themselves not running.
 * Ops readers only (the owner and the Team readers); nothing for anyone else (the route
 * answers 403) or when all is well. */
export function OpsAlarmBanner({ every = 60_000, className }: { every?: number; className?: string }) {
  const { state } = useAuth();
  const owner = state.status === "signed-in" && isOpsReader(state.me);
  const [alarms, setAlarms] = useState<OpsAlarms | null>(null);
  // the last poll failed: the alarms shown (if any) are the last ones read, not current
  const [unavailable, setUnavailable] = useState(false);
  useEffect(() => {
    if (!owner) return;
    let live = true;
    const ctl = new AbortController();
    const load = () =>
      getOpsAlarms(ctl.signal).then(
        (a) => {
          if (!live) return;
          setAlarms(a);
          setUnavailable(false);
        },
        (e) => {
          if (!live) return;
          // a refusal means "not an ops reader": say nothing and stop asking; only a failed
          // read (network, 5xx) is "Monitoring unavailable"
          if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
            clearInterval(id);
            return;
          }
          setUnavailable(true);
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
  // no poll has succeeded yet: say the monitoring cannot be read rather than show nothing
  const seen = alarms ?? (unavailable ? ({ configured: true, open: [], running: true } as unknown as OpsAlarms) : null);
  if (!owner || !seen || !seen.configured) return null;
  const open = seen.open ?? [];
  if (open.length === 0 && seen.running && !unavailable) return null;
  // one line: the count first, so a narrow window that cuts the line keeps it, then every
  // problem by its headline (the server's line up to its first "; "); the detail is in the
  // tooltip and on the Reliability page. Never "A · B · N more".
  const headline = opsHeadline;
  const base = open.length > 0
    ? `${open.length} reliability problem${open.length === 1 ? "" : "s"}: ${open.map((i) => headline(i.line)).join(" · ")}`
    : seen.running
      ? ""
      : `Reliability checks are not running${seen.checked_at ? ` (last ${eastern(seen.checked_at)})` : ""}; a new problem would not show here.`;
  // a failed poll keeps the last alarms on screen and says they may be out of date
  const lost = `Monitoring unavailable: the last check could not be read${seen.checked_at ? ` (alarms shown are from ${eastern(seen.checked_at)})` : ""}.`;
  // the failed poll is said first: a long problem line must not truncate it away
  const text = unavailable ? (base ? `${lost} ${base}` : lost) : base;
  const full = open.length > 0 ? [`${open.length} reliability problem${open.length === 1 ? "" : "s"}:`, ...open.map((i) => i.line)].join("\n") : base;
  return (
    <Link
      href="/ops"
      role="alert"
      title={unavailable && full ? `${lost}\n${full}` : unavailable ? lost : full}
      className={cn("flex items-center gap-1.5 border-b border-late-line bg-late-soft px-5 py-1 text-[12px] leading-4 text-late hover:underline", className)}
      data-testid="ops-alarm-banner"
    >
      <TriangleAlert className="size-3 shrink-0" strokeWidth={2} aria-hidden />
      <span className="min-w-0 truncate">{text}</span>
    </Link>
  );
}

function NavItem({ href, label, place, count, on, onNavigate }: { href: string; label: string; place?: string; count?: number | null; on?: boolean; onNavigate?: () => void }) {
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
        <span className="num text-[12px] text-muted max-xl:hidden" aria-label={`${count} late`}>
          {count}
        </span>
      )}
    </Link>
  );
}

/** The place under a section that this location is: a #fragment place first, else the longest path match. */
export function currentChild(children: NavPlace[], path: string, hash: string): string | null {
  const exact = children.find((c) => c.href.includes("#") && isOn(path, c.href, hash));
  if (exact) return exact.href;
  const plain = children.filter((c) => !c.href.includes("#") && isOn(path, c.href.split("?")[0]));
  plain.sort((a, b) => b.href.length - a.href.length);
  return plain[0]?.href ?? null;
}

/** The sidebar's sections; the open section lists its places under it (wide sidebar only). */
function SectionNav({ me, late }: { me: NavReader; late: number | null }) {
  const path = usePathname();
  const hash = useHash();
  const sections = navSectionsFor(me);
  const open = sectionOf(path, sections, hash);
  return (
    <div className="space-y-0.5" data-testid="nav-sections">
      {sections.map((s) => {
        const isOpen = open?.key === s.key;
        const child = isOpen ? currentChild(s.children, path, hash) : null;
        const lateChild = s.children.find((c) => c.late);
        return (
          <div key={s.key}>
            <NavItem href={s.href} label={s.label} place={s.place} on={isOpen && (!s.children.length || child === s.href)} count={lateChild && !isOpen ? late : undefined} />
            {isOpen && s.children.length > 1 && (
              <ul className="mt-px mb-1 space-y-px pl-[26px] max-xl:hidden" aria-label={s.label} data-testid={`nav-${s.key}`}>
                {s.children.filter((c) => c.href !== s.href).map((c) => {
                  const on = child === c.href;
                  return (
                    <li key={c.href}>
                      <Link
                        href={c.href}
                        onPointerEnter={() => readAheadRoute(c.href)}
                        aria-current={on ? "page" : undefined}
                        className={cn("flex h-7 items-center rounded-[2px] px-2 text-[13px]", on ? "bg-nav-on font-medium text-fg" : "text-muted hover:bg-hover hover:text-fg")}
                      >
                        <span className="flex-1 truncate">{c.label}</span>
                        {c.late && typeof late === "number" && late > 0 && <span className="num text-[12px] text-muted" aria-label={`${late} late`}>{late}</span>}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** A section's places as tabs: Watching's own pages draw it under their title at every width
 * (Alerts, Late POs, Data health, Quality: the owner's "Late POs as a tab"); the shell draws
 * it for the other sections where the sidebar is a rail and cannot list them (1024 to 1279). */
export function SectionTabs({ section, className }: { section: string; className?: string }) {
  const { state } = useAuth();
  const me = state.status === "signed-in" ? state.me : null;
  const path = usePathname() ?? "";
  const hash = useHash();
  const s = navSectionsFor(me).find((x) => x.key === section);
  if (!s || s.children.length < 2) return null;
  const current = currentChild(s.children, path, hash);
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

/** The shell's strip of the open section's places, on the rail widths only (Watching draws its own). */
function RailSectionTabs() {
  const { state } = useAuth();
  const me = state.status === "signed-in" ? state.me : null;
  const path = usePathname() ?? "";
  const hash = useHash();
  const open = sectionOf(path, navSectionsFor(me), hash);
  if (!open || open.key === "watching") return null;
  return <SectionTabs section={open.key} className="hidden lg:flex xl:hidden" />;
}

/** What is the reader's own, a click away from every section (the owner's nav decision,
 * 11-14): memory, templates and the logs. A place the open section already lists is left
 * out there; the autopilot's run log is the owner's only. */
export const YOURS: NavPlace[] = [
  { href: "/memory", label: "Memory" },
  { href: "/templates", label: "Templates" },
  { href: "/work-log", label: "Work log" },
];

/** The lists, under "Lists" on every page (the owner, 11-16: the block went away on the Work
 * pages, which also named them, so it seemed to come and go). No section names them, so the
 * sidebar is the same on every route; each page shows only the items its reader may open.
 * The MRP testing list is gone (the owner, 11-17: not ours). */
export const LISTS: NavPlace[] = ALL_LISTS;

/** No search params read. Each list only for its readers (lib/lists); none: no block at all. */
export function ListsNav({ me }: { me: NavReader }) {
  const path = usePathname() ?? "";
  const lists = listsFor(me);
  if (!lists.length) return null;
  return (
    <div className="mt-4 space-y-px max-xl:mt-3 max-xl:border-t max-xl:border-border max-xl:pt-3" data-testid="nav-lists">
      <div className="px-2 pb-1 text-[11px] font-medium leading-4 tracking-[0.01em] text-faint max-xl:sr-only">Lists</div>
      {lists.map((p) => <NavItem key={p.href} href={p.href} label={p.label} place={p.href} on={isOn(path, p.href)} />)}
    </div>
  );
}

export function yoursFor(me: NavReader, open: NavSection | null): NavPlace[] {
  const listed = new Set((open?.children ?? []).map((c) => c.href));
  const all = me?.owner ? [...YOURS, { href: "/autopilot?tab=audit", label: "Autopilot log" }] : YOURS;
  return all.filter((p) => !listed.has(p.href));
}

function YoursNav({ me }: { me: NavReader }) {
  const path = usePathname() ?? "";
  const params = useSearchParams();
  const places = yoursFor(me, sectionOf(path, navSectionsFor(me)));
  if (!places.length) return null;
  return (
    <div className="mt-4 space-y-px max-xl:mt-3 max-xl:border-t max-xl:border-border max-xl:pt-3" data-testid="nav-yours">
      <div className="px-2 pb-1 text-[11px] font-medium leading-4 tracking-[0.01em] text-faint max-xl:sr-only">Yours</div>
      {places.map((p) => {
        const [base, query] = p.href.split("?");
        // a place with a query (the run log, a tab of Autopilot) never claims the page: its section does
        const on = !query && isOn(path, base) && !params.get("tab");
        return <NavItem key={p.href} href={p.href} label={p.label} place={base} on={on} />;
      })}
    </div>
  );
}

/** The person's pinned pages in the sidebar (their role's defaults until they change them). */
function PinnedNav({ me }: { me: PinReader | null }) {
  const { pins, defaults } = usePins(me);
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
  const [read, setRead] = useState<LateRead | null>(null);
  const late = read?.items ?? null;
  const partial = !!read && read.total > read.items.length;
  const path = usePathname();
  const params = useSearchParams();
  useEffect(() => {
    let live = true;
    loadLateRead().then((l) => live && setRead(l), () => undefined);
    return () => {
      live = false;
    };
  }, []);
  if (!late?.length) return null;
  const counts = new Map<string, number>();
  for (const p of late) if (p.supplier) counts.set(p.supplier, (counts.get(p.supplier) ?? 0) + 1);
  const rows = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  const current = path === "/" ? params.get("supplier") : null;
  return (
    <SidebarSection title="Late by supplier">
      <div className="space-y-px" data-testid="late-by-supplier">
        {partial && read && (
          <Link href="/late" className="block px-2 pb-1 text-[12px] leading-4 text-muted hover:text-fg" data-testid="late-by-supplier-partial">
            Counts from the first {read.items.length} of {read.total} late orders; all are on Late orders
          </Link>
        )}
        {rows.slice(0, 7).map(([name, n]) => {
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
        {rows.length > 7 && (
          <Link href="/late" className="flex h-7 items-center rounded-md px-2 pl-[40px] text-[12px] text-muted hover:text-fg">
            {rows.length - 7} more
          </Link>
        )}
      </div>
    </SidebarSection>
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
  const home = useHome();
  const saved = useSaved();
  const router = useRouter();
  const { open } = useCommandPalette();
  const { shortcut } = useShortcut();
  const late = lateOf(home);
  const { state } = useAuth();
  const signedIn = state.status === "signed-in" ? state.me : null;
  const savedOnly = saved.filter((s) => s.kind !== "page");
  const recent = (home?.recent_questions ?? []).filter(
    (r, i, all) => all.findIndex((x) => x.question.trim().toLowerCase() === r.question.trim().toLowerCase()) === i,
  );
  return (
    <aside className="hidden w-[60px] shrink-0 flex-col border-r border-border bg-sidebar lg:flex xl:w-[240px]" aria-label="Sidebar">
      <div className="flex h-[52px] shrink-0 items-center justify-center xl:justify-start xl:px-5">
        <Link href="/" aria-label="PLX Workbench, My work">
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
        <SectionNav me={signedIn} late={late} />
        <ListsNav me={signedIn} />
        <Suspense>
          <YoursNav me={signedIn} />
        </Suspense>
        <PinnedNav me={signedIn} />
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
        <SyncStatus home={home} />
        <OpsStatus />
        <UserMenu />
      </div>
    </aside>
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
  const home = useHome();
  const late = lateOf(home);
  const { state } = useAuth();
  const reader = state.status === "signed-in" ? state.me : null;
  const sections = navSectionsFor(reader);
  const lists = listsFor(reader);
  const open = sectionOf(path, sections);
  const tabs = TABS.map((key) => sections.find((s) => s.key === key)).filter((s): s is NavSection => !!s)
    .map((s) => ({ ...s, late: s.children.some((c) => c.late) }));
  // More: every other section, and each place under a section that is not its own link
  const rest = sections.flatMap((s) => (TABS.includes(s.key) ? s.children.filter((c) => c.href !== s.href).map((c) => ({ ...c, section: s })) : [s, ...s.children.filter((c) => c.href !== s.href).map((c) => ({ ...c, section: s }))]))
    .map((p) => ({ href: p.href, label: p.label, icon: ("icon" in p && p.icon) || ("section" in p ? p.section.icon : Inbox), sub: "section" in p }))
    .concat(lists.map((p) => ({ href: p.href, label: p.label, icon: List, sub: false })));
  const moreOn = (!!open && !TABS.includes(open.key)) || lists.some((p) => isOn(path ?? "", p.href));
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
      <Link href="/" aria-label="My work">
        <Logo />
      </Link>
      <div className="flex-1" />
      <MobileSearch />
      <MobileAccount />
    </header>
  );
}

/** Pages that draw their own Toolbar; the shell adds no phone title bar over them.*/
export const TOOLBAR_PAGES: string[] = ["/late", "/meetings", "/team", "/ask", "/po", "/ao", "/customer", "/supplier", "/object", "/source", "/activity", "/feedback", "/watching", "/my-work", "/rd-projects", "/autopilot", "/ops", "/trust", "/work-log", "/templates", "/memory", "/help", "/records", "/qc"];

function ShellSkeleton() {
  return (
    <div className="mx-auto w-full max-w-[1200px] px-4 pt-8 sm:px-8">
      <Pending label="Signing in" after={600} />
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
          <OpsAlarmBanner />
          <Suspense>
            <RailSectionTabs />
          </Suspense>
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
