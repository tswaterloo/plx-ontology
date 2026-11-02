"use client";

import * as Dialog from "@radix-ui/react-dialog";
import * as Menu from "@radix-ui/react-dropdown-menu";
import { Bell, FlaskConical, Home, LogOut, Menu as MenuIcon, MessageSquare, Search, Timer, X } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { getHome } from "@/lib/api";
import { stashPending } from "@/lib/pending";
import { AskBox } from "./ask-box";
import { plainServerNote } from "@/lib/format";
import { cn } from "@/lib/cn";
import { useSaved } from "@/lib/saved";
import { RequireAuth, useAuth } from "./auth";
import { ThemeToggle } from "./theme";
import { Skeleton } from "./ui/skeleton";
import { ErrorNotice } from "./ui/notice";

export function Logo({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-2 font-semibold tracking-tight", className)}>
      <span className="flex size-7 items-center justify-center rounded-lg bg-accent text-[13px] font-bold text-accent-fg" aria-hidden>
        P
      </span>
      <span>PLX Workbench</span>
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
      className="flex items-center justify-center gap-1.5 truncate border-b border-border bg-surface-2 px-3 py-1 text-center text-label text-muted"
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
      className="flex items-center justify-center gap-2 border-b border-border bg-surface-2 px-3 py-1 text-center text-label text-muted"
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
          "flex min-h-9 items-center gap-2.5 rounded-lg px-3 py-2 text-row transition-colors",
          on ? "bg-surface-2 font-medium text-fg" : "text-muted hover:bg-surface-2 hover:text-fg",
        )}
      >
        <Icon className="size-4" aria-hidden />
        <span className="flex-1">{label}</span>
        {typeof count === "number" && count > 0 && (
          <span className="rounded-md bg-neutral-soft px-1.5 text-label tabular-nums text-late" aria-label={`${count} late`}>
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
      <div className="mt-6 px-3 pb-1 text-eyebrow uppercase text-muted">Saved</div>
      {saved.length === 0 ? (
        <p className="px-3 text-meta text-muted">Save an answer or a page to find it here.</p>
      ) : (
        <ul className="space-y-0.5 overflow-auto">
          {saved.slice(0, 8).map((s) => (
            <li key={s.key}>
              <Link
                href={s.href}
                onClick={onNavigate}
                className="block truncate rounded-lg px-3 py-1.5 text-row text-muted hover:bg-surface-2 hover:text-fg"
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

const SEARCH_ID = "global-search";

/** The top bar's search: records as you type, Enter asks. ⌘K, Ctrl K and "/" focus it. */
function GlobalSearch() {
  const router = useRouter();
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const el = document.getElementById(SEARCH_ID) as HTMLTextAreaElement | null;
      if (!el) return;
      const typing = e.target instanceof HTMLElement && (e.target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName));
      if (((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") || (e.key === "/" && !typing)) {
        e.preventDefault();
        el.focus();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return (
    <div className="relative hidden w-full max-w-[480px] sm:block" data-testid="global-search">
      <AskBox
        size="sm"
        inputId={SEARCH_ID}
        label="Search records or ask"
        placeholder="Search POs, suppliers, customers, or ask a question"
        onSubmit={(q, pins) => {
          stashPending(q, pins);
          router.push(`/ask?q=${encodeURIComponent(q)}`);
        }}
      />
      <kbd className="pointer-events-none absolute top-1/2 right-11 hidden -translate-y-1/2 rounded border border-border px-1.5 text-label text-muted lg:block" aria-hidden>
        ⌘K
      </kbd>
    </div>
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
        className="flex size-8 items-center justify-center rounded-full bg-surface-2 text-meta font-semibold text-fg ring-1 ring-border hover:ring-border-strong cursor-pointer"
      >
        {initials || "?"}
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Content
          align="end"
          sideOffset={8}
          className="z-50 w-64 rounded-xl border border-border bg-surface p-1.5 shadow-pop animate-fade-in"
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
            className="flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-row data-[highlighted]:bg-surface-2 data-[highlighted]:outline-2 data-[highlighted]:outline-ring"
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
    <div className="mx-auto w-full max-w-3xl space-y-4 p-6" role="status" aria-label="Loading">
      <Skeleton className="h-10 w-2/3" />
      <Skeleton className="h-28 w-full rounded-3xl" />
      <Skeleton className="h-40 w-full rounded-2xl" />
    </div>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const { state, refresh } = useAuth();
  return (
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
        <aside className="hidden w-60 shrink-0 border-r border-border bg-bg-subtle lg:block">
          <div className="sticky top-0 flex h-dvh flex-col px-3 py-4">
            <Link href="/" className="mb-5 px-2">
              <Logo />
            </Link>
            <Nav />
          </div>
        </aside>
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-border bg-bg/80 px-3 backdrop-blur sm:px-5">
            <Dialog.Root open={open} onOpenChange={setOpen}>
              <Dialog.Trigger
                aria-label="Open menu"
                className="rounded-lg p-2 text-muted hover:bg-surface-2 hover:text-fg lg:hidden cursor-pointer"
              >
                <MenuIcon className="size-5" aria-hidden />
              </Dialog.Trigger>
              <Dialog.Portal>
                <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40 animate-fade-in" />
                <Dialog.Content className="fixed inset-y-0 left-0 z-50 flex w-[82vw] max-w-xs flex-col bg-bg px-3 py-4 shadow-pop animate-fade-in">
                  <div className="mb-5 flex items-center justify-between px-2">
                    <Dialog.Title asChild>
                      <span>
                        <Logo />
                      </span>
                    </Dialog.Title>
                    <Dialog.Close aria-label="Close menu" className="rounded-lg p-2 text-muted hover:bg-surface-2 cursor-pointer">
                      <X className="size-5" aria-hidden />
                    </Dialog.Close>
                  </div>
                  <Dialog.Description className="sr-only">Main navigation</Dialog.Description>
                  <Nav onNavigate={() => setOpen(false)} />
                </Dialog.Content>
              </Dialog.Portal>
            </Dialog.Root>
            <Link href="/" className="lg:hidden">
              <Logo className="text-row" />
            </Link>
            <div className="flex flex-1 justify-center">
              <GlobalSearch />
            </div>
            <div className="flex items-center gap-2">
              <Link href="/ask" aria-label="Search or ask" className="flex size-10 items-center justify-center rounded-lg text-muted hover:bg-surface-2 hover:text-fg sm:hidden">
                <Search className="size-5" aria-hidden />
              </Link>
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
  );
}
