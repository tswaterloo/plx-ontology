"use client";

import * as Menu from "@radix-ui/react-dropdown-menu";
import { Bookmark, CalendarCheck2, Clock3, History, Inbox, MessageCircle, MoreHorizontal, RefreshCw, Search, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { getHome, getLateList } from "@/lib/api";
import { CommandPaletteProvider, useCommandPalette } from "./command-palette";
import { plainServerNote, SYSTEM_LABEL } from "@/lib/format";
import { cn } from "@/lib/cn";
import { stashPending } from "@/lib/pending";
import { useShortcut } from "@/lib/platform";
import { useSaved } from "@/lib/saved";
import type { HomeResponse, LatePo } from "@/lib/types";
import { PlaceTile, SystemTile } from "./ui/glyphs";
import { Monogram } from "./ui/monogram";
import { RequireAuth, useAuth } from "./auth";
import { ThemeToggle } from "./theme";
import { Pending } from "./ui/skeleton";
import { ErrorNotice } from "./ui/notice";

const TZ = "America/Toronto";

/** The bench: an oak top on two legs, the legs in the text colour. */
export function BenchMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" aria-hidden className={cn("size-[18px] shrink-0", className)}>
      <g transform="translate(0 -1.7)">
        <path className="fill-oak" d="M3.5 9h25a1.5 1.5 0 0 1 1.5 1.5v3a1.5 1.5 0 0 1-1.5 1.5h-25A1.5 1.5 0 0 1 2 13.5v-3A1.5 1.5 0 0 1 3.5 9z" />
        <path fill="currentColor" d="M7.6 15h4.2l-2.6 10.6a1 1 0 0 1-1 .8H5.3a.8.8 0 0 1-.8-1z M20.2 15h4.2l3.1 10.4a.8.8 0 0 1-.8 1h-2.9a1 1 0 0 1-1-.8z" />
      </g>
    </svg>
  );
}

/** The app icon in small: the white bench on its forest tile. */
export function AppIcon({ className }: { className?: string }) {
  return (
    <span className={cn("flex size-[22px] shrink-0 items-center justify-center rounded-[6px] bg-gradient-to-b from-[#2e8a5f] to-[#1b5e40] text-white shadow-[inset_0_-1px_0_rgb(0_0_0/0.15)]", className)} aria-hidden>
      <svg viewBox="0 0 32 32" className="size-[15px]">
        <g transform="translate(0 -1.7)">
          <path fill="#fff" d="M3.5 9h25a1.5 1.5 0 0 1 1.5 1.5v3a1.5 1.5 0 0 1-1.5 1.5h-25A1.5 1.5 0 0 1 2 13.5v-3A1.5 1.5 0 0 1 3.5 9z" />
          <path fill="currentColor" d="M7.6 15h4.2l-2.6 10.6a1 1 0 0 1-1 .8H5.3a.8.8 0 0 1-.8-1z M20.2 15h4.2l3.1 10.4a.8.8 0 0 1-.8 1h-2.9a1 1 0 0 1-1-.8z" />
        </g>
      </svg>
    </span>
  );
}

export function Logo({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-2 text-[14px] leading-5 font-semibold tracking-[-0.01em] text-fg", className)}>
      <AppIcon />
      <span>
        PLX <span className="font-normal text-muted">Workbench</span>
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

const NAV: { href: string; label: string; icon: LucideIcon; late?: boolean }[] = [
  { href: "/", label: "Today", icon: Inbox },
  { href: "/late", label: "Late POs", icon: Clock3, late: true },
  { href: "/briefing", label: "Briefing", icon: CalendarCheck2 },
  { href: "/ask", label: "Ask", icon: MessageCircle },
  { href: "/actions", label: "Action log", icon: History },
];

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
        <RefreshCw className="size-[14px] shrink-0 text-faint" strokeWidth={2} aria-hidden />
        <span className="min-w-0 text-[12px] leading-4 max-xl:hidden">
          <span className="block text-muted">Updated {when(f.as_of, true)}</span>
          {behind.length > 0 && <span className="block truncate text-faint">{behind.map((s) => name(s.system)).join(" and ")} behind</span>}
        </span>
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Content side="top" align="start" sideOffset={6} className="z-50 w-72 rounded-xl bg-raised p-1.5 shadow-pop" data-testid="sync-detail">
          {sources.map((s) => (
            <div key={s.system} className="flex items-center gap-2.5 rounded-md px-2 py-1.5 text-row">
              <SystemTile system={s.system} size={18} />
              <span className="flex-1 truncate">{name(s.system)}</span>
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
        "group flex h-8 items-center gap-2.5 rounded-md px-2 text-[13px] leading-none max-xl:justify-center max-xl:px-0",
        on ? "bg-[color-mix(in_srgb,var(--fg)_7%,transparent)] font-medium text-fg" : "text-fg/90 hover:bg-hover",
      )}
    >
      <PlaceTile href={href} />
      <span className="flex-1 truncate max-xl:sr-only">{label}</span>
      {typeof count === "number" && count > 0 && (
        <span className="text-[12px] tabular-nums text-muted max-xl:hidden" aria-label={`${count} late`}>
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
              className={cn("flex h-8 items-center gap-2.5 rounded-md px-2 text-[13px]", on ? "bg-[color-mix(in_srgb,var(--fg)_7%,transparent)] font-medium" : "text-fg/90 hover:bg-hover")}
            >
              <Monogram name={name} size={20} />
              <span className="flex-1 truncate">{name}</span>
              <span className="text-[12px] tabular-nums text-muted">{n}</span>
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
  const recent = (home?.recent_questions ?? []).filter(
    (r, i, all) => all.findIndex((x) => x.question.trim().toLowerCase() === r.question.trim().toLowerCase()) === i,
  );
  return (
    <aside className="hidden w-[60px] shrink-0 flex-col border-r border-border bg-sidebar lg:flex xl:w-[240px]" aria-label="Sidebar">
      <div className="flex h-[52px] shrink-0 items-center justify-center xl:justify-start xl:px-5">
        <Link href="/" aria-label="PLX Workbench, Today">
          <Logo className="max-xl:[&>span:last-child]:hidden" />
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
          {NAV.map((n) => (
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
        <SyncStatus home={home} />
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
      className={cn("flex size-6 shrink-0 items-center justify-center rounded-full bg-gradient-to-b from-[#7d8794] to-[#5c6672] text-[10px] font-semibold text-white", className)}
      aria-hidden
    >
      {initialsOf(name) || "?"}
    </span>
  );
}

function AccountMenuContent({ name, email }: { name: string; email?: string | null }) {
  const { signOut } = useAuth();
  const router = useRouter();
  return (
    <Menu.Portal>
      <Menu.Content side="top" align="start" sideOffset={6} collisionPadding={8} className="z-50 w-64 rounded-xl bg-raised p-1.5 shadow-pop">
        <div className="px-2 py-1.5">
          <p className="truncate text-row font-medium">{name}</p>
          {email && <p className="truncate text-meta text-muted">{email}</p>}
        </div>
        <AccessBanner className="px-2 py-1.5" />
        <div className="flex items-center justify-between px-2 py-1.5">
          <span className="text-meta text-muted">Appearance</span>
          <ThemeToggle />
        </div>
        <Menu.Separator className="mx-2 my-1 h-px bg-border" />
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
  if (state.status !== "signed-in") return null;
  const name = state.me.display_name || "Account";
  return (
    <Menu.Root>
      <Menu.Trigger aria-label={`Account: ${name}`} className="flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left hover:bg-hover max-xl:justify-center max-xl:px-0 cursor-pointer">
        <Avatar name={name} className="size-5 text-[9px]" />
        <span className="min-w-0 flex-1 max-xl:hidden">
          <span className="block truncate text-[13px] font-medium leading-4">{name}</span>
          {dev && desktop && <DevBanner compact className="truncate text-[11px] leading-4" />}
        </span>
        <MoreHorizontal className="size-4 shrink-0 text-faint max-xl:hidden" aria-hidden />
      </Menu.Trigger>
      <AccountMenuContent name={name} email={state.me.email} />
    </Menu.Root>
  );
}

/** Phone: the tab bar along the bottom, as an iPhone app has it. */
function TabBar() {
  const path = usePathname();
  const home = useHome();
  const late = lateOf(home);
  const tabs = NAV.filter((n) => n.href !== "/actions");
  return (
    <nav
      aria-label="Main"
      className="fixed inset-x-0 bottom-0 z-30 border-t border-border bg-[color-mix(in_srgb,var(--bg-subtle)_88%,transparent)] pb-[env(safe-area-inset-bottom)] backdrop-blur-xl lg:hidden"
      data-testid="tab-bar"
    >
      <div className="mx-auto grid h-[52px] max-w-md grid-cols-4">
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
      </div>
    </nav>
  );
}

/** Phone: the account button for a page's title bar. */
export function MobileAccount() {
  const { state } = useAuth();
  const desktop = useDesktopShell();
  if (state.status !== "signed-in") return null;
  const name = state.me.display_name || "Account";
  return (
    <div className="flex items-center gap-1 lg:hidden">
      {desktop === false && <DevBanner compact className="text-[11px] leading-4" />}
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
    <header className="sticky top-0 z-30 flex h-12 shrink-0 items-center gap-2 border-b border-border bg-[color-mix(in_srgb,var(--bg-subtle)_88%,transparent)] px-3 backdrop-blur-xl lg:hidden">
      <Link href="/" aria-label="Today">
        <Logo />
      </Link>
      <div className="flex-1" />
      <MobileSearch />
      <MobileAccount />
    </header>
  );
}

/** Pages that draw their own Toolbar; the shell adds no phone title bar over them.*/
const TOOLBAR_PAGES: string[] = ["/late", "/briefing", "/ask", "/po", "/ao", "/so", "/customer", "/supplier", "/object", "/source", "/actions"];

function ShellSkeleton() {
  return (
    <div className="mx-auto w-full max-w-[1200px] px-4 pt-8 sm:px-8">
      <Pending label="Signing in" />
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
    </CommandPaletteProvider>
  );
}
