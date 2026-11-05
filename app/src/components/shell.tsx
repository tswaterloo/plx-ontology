"use client";

import * as Dialog from "@radix-ui/react-dialog";
import * as Menu from "@radix-ui/react-dropdown-menu";
import { Bell, FlaskConical, Home, ListChecks, LogOut, Menu as MenuIcon, MessageSquare, Search, Timer, X } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { getHome } from "@/lib/api";
import { CommandPaletteProvider, useCommandPalette } from "./command-palette";
import { plainServerNote } from "@/lib/format";
import { cn } from "@/lib/cn";
import { useSaved } from "@/lib/saved";
import { RequireAuth, useAuth } from "./auth";
import { ThemeToggle } from "./theme";
import { Pending } from "./ui/skeleton";
import { ErrorNotice } from "./ui/notice";

export function Logo({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex items-baseline gap-1.5 font-serif text-[18px] leading-6 tracking-[-0.01em] text-fg", className)}>
      <span className="font-semibold">PLX</span>
      <span className="font-normal text-muted">Workbench</span>
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
 * One short line in DEV mode: "Demo sign-in", with the full-read label beside it when the
 * reader holds the grant (said once per page, never on every card). The data label (e.g.
 * FIXTURE) is the line's tooltip and screen-reader text.
 */
export function DevBanner() {
  const { dev, me, access } = useBanner();
  if (!dev) return null;
  const label = plainServerNote(me?.data_label);
  return (
    <div
      role="note"
      title={["Demo sign-in, not Microsoft: not for staff use", label].filter(Boolean).join(". ")}
      className="flex items-center justify-center gap-1.5 truncate border-b border-border bg-bg-subtle px-3 py-1 text-center text-label text-muted"
      data-testid="dev-banner"
    >
      <FlaskConical className="size-3.5 shrink-0" aria-hidden />
      <span>Demo sign-in</span>
      {label && <span className="sr-only">: {label}</span>}
      {access && (
        <span className="font-normal" data-testid="access-banner">
          <span aria-hidden>· </span>
          {access}
        </span>
      )}
    </div>
  );
}

/** Shown once per page in Microsoft sign-in while the reader holds a declared full-read grant (DEV mode says it in its own line). */
export function AccessBanner() {
  const { dev, access } = useBanner();
  if (!access || dev) return null;
  return (
    <div
      role="note"
      className="flex items-center justify-center gap-2 border-b border-border bg-bg-subtle px-3 py-1 text-center text-label text-muted"
      data-testid="access-banner"
    >
      <span>{access}</span>
    </div>
  );
}

/** The late count for the sidebar pill: read once per page load, never blocking the page. */
let lateCount: Promise<number | null> | null = null;
function useLateCount(): number | null {
  const [n, setN] = useState<number | null>(null);
  useEffect(() => {
    lateCount ??= getHome()
      .then((h) => (h.needs_attention?.status === "read" ? (h.needs_attention.late_total ?? (h.needs_attention.late_pos ?? []).length) : null))
      .catch(() => {
        lateCount = null;
        return null;
      });
    let live = true;
    lateCount.then((v) => live && setN(v));
    return () => {
      live = false;
    };
  }, []);
  return n;
}

function Nav({ onNavigate }: { onNavigate?: () => void }) {
  const path = usePathname();
  const saved = useSaved();
  const late = useLateCount();
  const item = (href: string, label: string, Icon: typeof Home, count?: number | null) => {
    const on = href === "/" ? path === "/" : path === href || path.startsWith(`${href}/`);
    return (
      <Link
        href={href}
        onClick={onNavigate}
        aria-current={on ? "page" : undefined}
        className={cn(
          "flex min-h-8 items-center gap-2.5 rounded px-2.5 py-1.5 text-row transition-colors",
          on ? "bg-border font-medium text-fg" : "text-muted hover:bg-border/60 hover:text-fg",
        )}
      >
        <Icon className="size-4 shrink-0" strokeWidth={1.5} aria-hidden />
        <span className="flex-1">{label}</span>
        {typeof count === "number" && count > 0 && (
          <span className="text-meta font-medium tabular-nums text-late" aria-label={`${count} late`}>
            {count}
          </span>
        )}
      </Link>
    );
  };
  return (
    <nav aria-label="Main" className="flex h-full flex-col gap-0.5">
      {item("/", "Today", Home)}
      {item("/late", "Late orders", Timer, late)}
      {item("/briefing", "Briefing", Bell)}
      {item("/ask", "Ask", MessageSquare)}
      {item("/actions", "Actions", ListChecks)}
      <div className="mt-7 px-2.5 pb-1.5 text-eyebrow uppercase text-faint">Saved</div>
      {saved.length === 0 ? (
        <p className="px-2.5 text-meta text-faint">Answers and pages you save appear here.</p>
      ) : (
        <ul className="space-y-0.5 overflow-auto">
          {saved.slice(0, 8).map((s) => (
            <li key={s.key}>
              <Link
                href={s.href}
                onClick={onNavigate}
                className="block truncate rounded px-2.5 py-1 text-meta text-muted hover:bg-border/60 hover:text-fg"
              >
                {s.label}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </nav>
  );
}

/** The top bar's search: looks like a field, opens the command bar (records, places, ask). */
function GlobalSearch() {
  const { open } = useCommandPalette();
  return (
    <button
      type="button"
      onClick={open}
      className="group hidden h-8 w-full max-w-[440px] items-center gap-2.5 rounded border border-border-strong bg-surface px-3 text-left text-row text-faint transition-colors hover:border-control hover:text-muted sm:flex cursor-pointer"
      data-testid="global-search"
      aria-label="Search records or ask (⌘K)"
    >
      <Search className="size-3.5 shrink-0" strokeWidth={1.75} aria-hidden />
      <span className="flex-1 truncate">Find a PO, supplier or customer, or ask…</span>
      <kbd className="rounded-sm border border-border px-1 font-mono text-[11px] leading-4 text-faint" aria-hidden>
        ⌘K
      </kbd>
    </button>
  );
}

function UserMenu() {
  const { state, signOut } = useAuth();
  const router = useRouter();
  if (state.status !== "signed-in") return null;
  const u = { name: state.me.display_name, email: state.me.email };
  const initials = u.name
    .split(/\s+/)
    .map((p) => p[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();
  return (
    <Menu.Root>
      <Menu.Trigger
        aria-label={`Account: ${u.name}`}
        className="flex size-7 items-center justify-center rounded-full border border-border-strong text-[11px] font-semibold tracking-wide text-fg hover:border-control cursor-pointer"
      >
        {initials || "?"}
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Content
          align="end"
          sideOffset={8}
          className="z-50 w-64 rounded-xl border border-border bg-raised p-1.5 shadow-pop animate-fade-in"
        >
          <div className="px-2.5 py-2">
            <p className="truncate text-row font-medium">{u.name}</p>
            <p className="truncate text-meta text-muted">{u.email}</p>
          </div>
          <div className="flex items-center justify-between px-2.5 py-2">
            <span className="text-meta text-muted">Theme</span>
            <ThemeToggle />
          </div>
          <Menu.Separator className="my-1 h-px bg-border" />
          <Menu.Item
            onSelect={async () => {
              await signOut();
              router.replace("/signin");
            }}
            className="flex cursor-pointer items-center gap-2 rounded px-2.5 py-2 text-row data-[highlighted]:bg-surface-2"
          >
            <LogOut className="size-4" aria-hidden /> Sign out
          </Menu.Item>
        </Menu.Content>
      </Menu.Portal>
    </Menu.Root>
  );
}

function ShellSkeleton() {
  return (
    <div className="mx-auto w-full max-w-[1200px] px-4 pt-8 sm:px-8">
      <Pending label="Checking your sign-in" />
    </div>
  );
}

function MobileSearch() {
  const { open } = useCommandPalette();
  return (
    <button type="button" onClick={open} aria-label="Search or ask" className="flex size-10 items-center justify-center rounded text-muted hover:bg-surface-2 hover:text-fg sm:hidden cursor-pointer">
      <Search className="size-5" strokeWidth={1.75} aria-hidden />
    </button>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const { state, refresh } = useAuth();
  return (
    <CommandPaletteProvider>
    <div className="flex min-h-dvh flex-col">
      <a
        href="#main"
        className="sr-only z-50 rounded-lg bg-accent px-3 py-2 text-accent-fg focus:not-sr-only focus:fixed focus:left-3 focus:top-3"
      >
        Skip to content
      </a>
      <DevBanner />
      <AccessBanner />
      <div className="flex min-h-0 flex-1">
        <aside className="hidden w-[216px] shrink-0 bg-bg-subtle lg:block">
          <div className="sticky top-0 flex h-dvh flex-col px-3 py-3">
            <Link href="/" className="mb-6 flex h-8 items-center px-2.5">
              <Logo />
            </Link>
            <Nav />
          </div>
        </aside>
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-30 flex h-12 items-center gap-2 border-b border-border bg-bg px-3 sm:px-6">
            <Dialog.Root open={open} onOpenChange={setOpen}>
              <Dialog.Trigger
                aria-label="Open menu"
                className="rounded p-2 text-muted hover:bg-surface-2 hover:text-fg lg:hidden cursor-pointer"
              >
                <MenuIcon className="size-5" aria-hidden />
              </Dialog.Trigger>
              <Dialog.Portal>
                <Dialog.Overlay className="fixed inset-0 z-40 bg-[rgb(28_26_23/0.32)] animate-fade-in" />
                <Dialog.Content className="fixed inset-y-0 left-0 z-50 flex w-[82vw] max-w-xs flex-col bg-bg-subtle px-3 py-4 shadow-pop animate-fade-in">
                  <div className="mb-5 flex items-center justify-between px-2">
                    <Dialog.Title asChild>
                      <span>
                        <Logo />
                      </span>
                    </Dialog.Title>
                    <Dialog.Close aria-label="Close menu" className="rounded p-2 text-muted hover:bg-surface-2 cursor-pointer">
                      <X className="size-5" aria-hidden />
                    </Dialog.Close>
                  </div>
                  <Dialog.Description className="sr-only">Main navigation</Dialog.Description>
                  <Nav onNavigate={() => setOpen(false)} />
                </Dialog.Content>
              </Dialog.Portal>
            </Dialog.Root>
            <Link href="/" className="lg:hidden">
              <Logo className="text-[17px]" />
            </Link>
            <div className="flex flex-1 justify-center">
              <GlobalSearch />
            </div>
            <div className="flex items-center gap-2">
              <MobileSearch />
              <UserMenu />
            </div>
          </header>
          <main id="main" className="flex min-w-0 flex-1 flex-col">
            {state.status === "error" ? (
              <div className="mx-auto w-full max-w-xl p-6">
                <ErrorNotice message={state.message} onRetry={refresh} />
              </div>
            ) : (
              <RequireAuth fallback={<ShellSkeleton />}>{children}</RequireAuth>
            )}
          </main>
        </div>
      </div>
    </div>
    </CommandPaletteProvider>
  );
}
