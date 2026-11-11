"use client";

import * as Menu from "@radix-ui/react-dropdown-menu";
import { Activity, BellRing, Bookmark, Bot, CalendarClock, Clock3, Gauge, History, Inbox, MessageCircle, MoreHorizontal, RefreshCw, Search, TriangleAlert, Users, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { getHome, getLateList, getOpsAlarms, getOpsHealth, type OpsAlarms, type OpsCheck, type OpsHealth } from "@/lib/api";
import { CommandPaletteProvider, useCommandPalette } from "./command-palette";
import { FeedbackButton, FeedbackProvider, useFeedbackDialog } from "./feedback-dialog";
import { plainServerNote, SYSTEM_LABEL } from "@/lib/format";
import { cn } from "@/lib/cn";
import { stashPending } from "@/lib/pending";
import { useShortcut } from "@/lib/platform";
import { useSaved } from "@/lib/saved";
import type { HomeResponse, LatePo } from "@/lib/types";
import { PlaceIcon, SystemCode } from "./ui/glyphs";
import { RequireAuth, useAuth } from "./auth";
import { ThemeToggle } from "./theme";
import { Pending } from "./ui/skeleton";
import { ViewAsBar, ViewAsPicker } from "./view-as";
import { ErrorNotice } from "./ui/notice";

const TZ = "America/Toronto";

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

function useBanner() {
  const { state } = useAuth();
  const me = state.status === "signed-in" ? state.me : null;
  const mode = me?.auth ?? state.config?.mode;
  const dev = me ? me.dev_mode || me.auth !== "entra" : !!mode && mode !== "entra";
  return { dev, me, access: me?.access_label ?? null };
}

/**
 * DEV mode, said once in the sidebar: "Demo sign-in", with the full-read label under it when
 * the reader holds the grant. The data label (e.g. FIXTURE) is its tooltip and screen-reader text.
 */
export function DevBanner({ className, compact }: { className?: string; compact?: boolean }) {
  const { dev, me, access } = useBanner();
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
  const { dev, access } = useBanner();
  if (!access || dev) return null;
  return (
    <div role="note" className={cn("text-meta text-muted", className)} data-testid="access-banner">
      <span>{access}</span>
    </div>
  );
}

/** Today, the sidebar and the tab bar read /api/home once per page load; Today's retry reads it again. */
let lateOnce: Promise<LatePo[]> | null = null;
/** Every late PO you may see (a page or three), read once per page load: Today's list and the sidebar's suppliers. */
export function loadLate(fresh = false): Promise<LatePo[]> {
  if (fresh || !lateOnce) {
    lateOnce = (async () => {
      const first = await getLateList(0, undefined, 100);
      const all: LatePo[] = [...(first.items ?? [])];
      if (!first.more || first.next_offset == null) return all;
      // the next pages at once: a page ends where the next starts (offset + 100)
      const step = first.next_offset;
      const offsets = [step, 2 * step].filter((o) => o < (first.total ?? 0));
      const pages = await Promise.all(offsets.map((o) => getLateList(o, undefined, 100)));
      for (const page of pages) {
        all.push(...(page.items ?? []));
        if (!page.more) break;
      }
      return all;
    })();
    lateOnce.catch(() => (lateOnce = null));
  }
  return lateOnce;
}

let homeOnce: Promise<HomeResponse> | null = null;
/** /api/home read once per page load, shared by Today, the sidebar and Ask: never tied to one
 * caller's abort (a caller that leaves ignores the answer), so leaving Today early, or React
 * mounting it twice, never fails the read for the others. */
export function loadHome(fresh = false): Promise<HomeResponse> {
  if (fresh || !homeOnce) {
    homeOnce = getHome();
    homeOnce.catch(() => (homeOnce = null));
  }
  return homeOnce;
}

function useHome(): HomeResponse | null {
  const [home, setHome] = useState<HomeResponse | null>(null);
  useEffect(() => {
    let live = true;
    loadHome().then((h) => live && setHome(h), () => undefined);
    return () => {
      live = false;
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

/** The places. Team shows only to a reader with a team grant, Reliability only to an ops
 * reader (the owner and the Team readers), Trust likewise; Feedback triage is in the
 * account menu. */
export const NAV: { href: string; label: string; icon: LucideIcon; late?: boolean; grant?: boolean; ops?: boolean }[] = [
  { href: "/", label: "My work", icon: Inbox },
  { href: "/late", label: "Late POs", icon: Clock3, late: true },
  { href: "/autopilot", label: "Autopilot", icon: Bot },
  { href: "/meetings", label: "Meetings", icon: CalendarClock },
  { href: "/team", label: "Team", icon: Users, grant: true },
  { href: "/ask", label: "Ask", icon: MessageCircle },
  { href: "/watching", label: "Watching", icon: BellRing },
  { href: "/work-log", label: "Work log", icon: History },
  { href: "/ops", label: "Reliability", icon: Activity, ops: true },
  { href: "/trust", label: "Trust", icon: Gauge, ops: true },
];

/** Whether this reader sees Team: a declared full-read grant or the owner. */
export function hasTeamGrant(me: { access_label?: string | null; feedback_admin?: boolean } | null | undefined): boolean {
  return !!(me && (me.access_label || me.feedback_admin));
}

/** Whether this reader sees the reliability alarms and /ops: the owner and the Team readers
 * (the server decides; /api/ops/status answers 403 to anyone else). */
export function isOpsReader(me: { ops_reader?: boolean } | null | undefined): boolean {
  return !!me?.ops_reader;
}

/** The nav this reader sees. */
export function navFor(me: (Parameters<typeof hasTeamGrant>[0] & { ops_reader?: boolean }) | null | undefined) {
  return NAV.filter((n) => (!n.grant || hasTeamGrant(me)) && (!n.ops || isOpsReader(me)));
}

/** Phone tabs: My work, Late POs, Ask, Meetings, then More for the rest. */
export const TABS = ["/", "/late", "/ask", "/meetings"];

function isOn(path: string, href: string) {
  return href === "/" ? path === "/" : path === href || path.startsWith(`${href}/`);
}

/** "1:51 a.m." today, "5 Nov, 1:51 a.m." before. */
function when(iso: string, withDay = false, now = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const day = (x: Date) => x.toLocaleDateString("en-CA", { timeZone: TZ });
  const time = d.toLocaleTimeString("en-CA", { hour: "numeric", minute: "2-digit", timeZone: TZ });
  if (day(d) === day(now) && !withDay) return time;
  return `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: TZ }).replace("Sept", "Sep")}, ${time}`;
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
        title={`Updated ${when(f.as_of)}`}
        data-testid="home-freshness"
      >
        <RefreshCw className={cn("size-[14px] shrink-0", behind.length ? "text-faint" : "text-live")} strokeWidth={2} aria-hidden />
        <span className="min-w-0 text-[12px] leading-4 max-xl:hidden">
          <span className="block text-muted">Updated {when(f.as_of, true)}</span>
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
                {s.as_of ? when(s.as_of) : "never"}
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
          <span className="block text-fg">Ops: {breached.length} behind</span>
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

/** "12:36 PM ET" today, "Nov 9, 12:36 PM ET" before: the owner reads times in Eastern. */
export function eastern(iso?: string | null, now = new Date()): string {
  const d = iso ? new Date(iso) : null;
  if (!d || Number.isNaN(d.getTime())) return "";
  const tz = "America/New_York";
  const time = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: tz });
  const day = (x: Date) => x.toLocaleDateString("en-CA", { timeZone: tz });
  if (day(d) === day(now)) return `${time} ET`;
  return `${d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: tz })}, ${time} ET`;
}

/** The red alarm line across the top of every page, the only place alarms reach anyone (no
 * paging): open reliability incidents (a stopped refresh, a failed or
 * OOM-killed unit, Postgres, disk, memory, backups), or the checks themselves not running.
 * Ops readers only (the owner and the Team readers); nothing for anyone else (the route
 * answers 403) or when all is well. */
export function OpsAlarmBanner({ every = 60_000, className }: { every?: number; className?: string }) {
  const { state } = useAuth();
  const owner = state.status === "signed-in" && isOpsReader(state.me);
  const [alarms, setAlarms] = useState<OpsAlarms | null>(null);
  useEffect(() => {
    if (!owner) return;
    let live = true;
    const ctl = new AbortController();
    const load = () => getOpsAlarms(ctl.signal).then((a) => live && setAlarms(a), () => live && setAlarms(null));
    load();
    const id = setInterval(load, every);
    return () => {
      live = false;
      ctl.abort();
      clearInterval(id);
    };
  }, [owner, every]);
  if (!owner || !alarms || !alarms.configured) return null;
  const open = alarms.open ?? [];
  if (open.length === 0 && alarms.running) return null;
  const text = open.length > 0
    ? `${open.length} reliability problem${open.length === 1 ? "" : "s"}: ${open.slice(0, 3).map((i) => i.line).join(" · ")}${open.length > 3 ? ` · ${open.length - 3} more` : ""}`
    : `Reliability checks are not running${alarms.checked_at ? ` (last ${eastern(alarms.checked_at)})` : ""}; a new problem would not show here.`;
  return (
    <Link
      href="/ops"
      role="alert"
      title={text}
      className={cn("flex items-center gap-1.5 border-b border-late-line bg-late-soft px-5 py-1 text-[12px] leading-4 text-late hover:underline", className)}
      data-testid="ops-alarm-banner"
    >
      <TriangleAlert className="size-3 shrink-0" strokeWidth={2} aria-hidden />
      <span className="min-w-0 truncate">{text}</span>
    </Link>
  );
}

function NavItem({ href, label, count, onNavigate }: { href: string; label: string; count?: number | null; onNavigate?: () => void }) {
  const path = usePathname();
  const on = isOn(path, href);
  return (
    <Link
      href={href}
      onClick={onNavigate}
      aria-current={on ? "page" : undefined}
      title={label}
      className={cn(
        "group flex h-7 items-center gap-2.5 rounded-r-[2px] border-l-2 px-2 text-[13px] leading-none max-xl:justify-center max-xl:px-0",
        on ? "border-accent bg-nav-on font-medium text-fg" : "border-transparent text-fg/90 hover:bg-hover",
      )}
    >
      <PlaceIcon href={href} className={on ? undefined : "opacity-85 group-hover:opacity-100"} />
      <span className="flex-1 truncate max-xl:sr-only">{label}</span>
      {typeof count === "number" && count > 0 && (
        <span className="num text-[12px] text-muted max-xl:hidden" aria-label={`${count} late`}>
          {count}
        </span>
      )}
    </Link>
  );
}

/** The suppliers with late POs, most first: each opens Today showing only that supplier's. */
function LateBySupplier() {
  const [late, setLate] = useState<LatePo[] | null>(null);
  const path = usePathname();
  const params = useSearchParams();
  useEffect(() => {
    let live = true;
    loadLate().then((l) => live && setLate(l), () => undefined);
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
  const nav = navFor(state.status === "signed-in" ? state.me : null);
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
        <div className="space-y-0.5">
          {nav.map((n) => (
            <NavItem key={n.href} href={n.href} label={n.label} count={n.late ? late : undefined} />
          ))}
        </div>
        <Suspense>
          <LateBySupplier />
        </Suspense>
        {saved.length > 0 && (
          <SidebarSection title="Saved">
            <div className="space-y-px">
              {saved.slice(0, 6).map((s) => (
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
          Feedback
        </Menu.Item>
        {onViewAs && (
          <Menu.Item onSelect={onViewAs} className="flex cursor-pointer items-center rounded-sm px-2 py-1.5 text-row outline-none data-[highlighted]:bg-hover" data-testid="view-as-open">
            View as someone
          </Menu.Item>
        )}
        <Menu.Item
          onSelect={async () => {
            await signOut();
            router.replace("/signin");
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
  const { dev } = useBanner();
  const desktop = useDesktopShell();
  const [picking, setPicking] = useState(false);
  if (state.status !== "signed-in") return null;
  const name = state.me.display_name || "Account";
  return (
    <>
    {state.me.can_view_as && <ViewAsPicker open={picking} onOpenChange={setPicking} />}
    <Menu.Root>
      <Menu.Trigger aria-label={`Account: ${name}`} className="flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left hover:bg-hover max-xl:justify-center max-xl:px-0 cursor-pointer">
        <Avatar name={name} className="size-5 text-[9px]" />
        <span className="min-w-0 flex-1 max-xl:hidden">
          <span className="block truncate text-[13px] font-medium leading-4">{name}</span>
          {dev && desktop && <DevBanner compact className="truncate text-[11px] leading-4" />}
        </span>
        <MoreHorizontal className="size-4 shrink-0 text-faint max-xl:hidden" aria-hidden />
      </Menu.Trigger>
      <AccountMenuContent name={name} email={state.me.email} onViewAs={state.me.can_view_as ? () => setPicking(true) : undefined} />
    </Menu.Root>
    </>
  );
}

/** Phone: the tab bar along the bottom, as an iPhone app has it. */
function TabBar() {
  const path = usePathname();
  const home = useHome();
  const late = lateOf(home);
  const { state } = useAuth();
  const nav = navFor(state.status === "signed-in" ? state.me : null);
  const tabs = TABS.map((href) => NAV.find((n) => n.href === href)!);
  const rest = nav.filter((n) => !TABS.includes(n.href));
  const moreOn = rest.some((n) => isOn(path, n.href));
  return (
    <nav
      aria-label="Main"
      className="fixed inset-x-0 bottom-0 z-30 border-t border-border bg-bg-subtle pb-[env(safe-area-inset-bottom)] lg:hidden"
      data-testid="tab-bar"
    >
      <div className="mx-auto grid h-[52px] max-w-md grid-cols-5">
        {tabs.map(({ href, label, icon: Icon, late: showLate }) => {
          const on = isOn(path, href);
          return (
            <Link key={href} href={href} aria-current={on ? "page" : undefined} className={cn("relative flex flex-col items-center justify-center gap-0.5 text-[10px] font-medium", on ? "text-accent" : "text-muted")}>
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
            <Menu.Content side="top" align="end" sideOffset={8} className="z-50 w-48 rounded-xl bg-raised p-1.5 shadow-pop">
              {rest.map(({ href, label, icon: Icon }) => (
                <Menu.Item key={href} asChild>
                  <Link href={href} className="flex items-center gap-2.5 rounded-md px-2 py-2 text-row outline-none data-[highlighted]:bg-hover">
                    <Icon className="size-4 text-muted" aria-hidden />
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
        <Menu.Trigger aria-label={`Account: ${name}`} className="flex size-9 items-center justify-center rounded-full cursor-pointer">
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
    <button type="button" onClick={open} aria-label="Search or ask" className="flex size-9 items-center justify-center rounded-full text-accent hover:bg-hover lg:hidden cursor-pointer">
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
export const TOOLBAR_PAGES: string[] = ["/late", "/meetings", "/team", "/ask", "/po", "/ao", "/so", "/customer", "/supplier", "/object", "/source", "/activity", "/feedback", "/watching", "/my-work", "/rd-projects", "/autopilot", "/ops", "/trust", "/work-log"];

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
  const { state, refresh } = useAuth();
  return (
    <CommandPaletteProvider>
      <FeedbackProvider>
      <div className="flex h-dvh overflow-hidden bg-bg">
        <a
          href="#main"
          className="sr-only z-50 rounded-sm bg-accent px-3 py-2 text-accent-fg focus:not-sr-only focus:fixed focus:left-3 focus:top-3"
        >
          Skip to content
        </a>
        <Sidebar />
        <main id="main" className="flex min-w-0 flex-1 flex-col overflow-y-auto pb-[calc(52px+env(safe-area-inset-bottom))] lg:pb-0" data-app-main>
          <MobileTitleBar />
          <ViewAsBar />
          <OpsAlarmBanner />
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
