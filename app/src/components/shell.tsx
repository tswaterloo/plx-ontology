"use client";

import * as Dialog from "@radix-ui/react-dialog";
import * as Menu from "@radix-ui/react-dropdown-menu";
import { Bookmark, FlaskConical, Home, LogOut, Menu as MenuIcon, MessageSquarePlus, X } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
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

export function DevBanner() {
  const { state } = useAuth();
  const me = state.status === "signed-in" ? state.me : null;
  const mode = me?.auth ?? state.config?.mode;
  const dev = me ? me.dev_mode || me.auth !== "entra" : !!mode && mode !== "entra";
  if (!dev) return null;
  const label = plainServerNote(me?.data_label);
  return (
    <div
      role="note"
      className="flex items-center justify-center gap-2 bg-warn-soft px-3 py-1.5 text-center text-xs font-medium text-warn"
      data-testid="dev-banner"
    >
      <FlaskConical className="size-3.5 shrink-0" aria-hidden />
      <span>
        DEV mode — demo sign-in, not Microsoft. Not for staff use.
        {label && <span className="font-normal"> {label}</span>}
      </span>
    </div>
  );
}

function Nav({ onNavigate }: { onNavigate?: () => void }) {
  const path = usePathname();
  const saved = useSaved();
  const item = (href: string, label: string, Icon: typeof Home) => (
    <Link
      href={href}
      onClick={onNavigate}
      aria-current={path === href ? "page" : undefined}
      className={cn(
        "flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors",
        path === href ? "bg-surface-2 font-medium text-fg" : "text-muted hover:bg-surface-2 hover:text-fg",
      )}
    >
      <Icon className="size-4" aria-hidden />
      {label}
    </Link>
  );
  return (
    <nav aria-label="Main" className="flex h-full flex-col gap-1">
      <Link
        href="/ask"
        onClick={onNavigate}
        className="mb-3 flex items-center gap-2 rounded-xl border border-border bg-surface px-3 py-2.5 text-sm font-medium shadow-card transition-colors hover:bg-surface-2"
      >
        <MessageSquarePlus className="size-4" aria-hidden /> New question
      </Link>
      {item("/", "Home", Home)}
      <div className="mt-5 flex items-center gap-2 px-3 pb-1 text-xs font-medium uppercase tracking-wide text-muted">
        <Bookmark className="size-3" aria-hidden /> Saved
      </div>
      {saved.length === 0 ? (
        <p className="px-3 text-xs text-muted">Save an answer or a page to find it here.</p>
      ) : (
        <ul className="space-y-0.5 overflow-auto">
          {saved.slice(0, 12).map((s) => (
            <li key={s.key}>
              <Link
                href={s.href}
                onClick={onNavigate}
                className="block truncate rounded-lg px-3 py-1.5 text-sm text-muted hover:bg-surface-2 hover:text-fg"
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
        className="flex size-8 items-center justify-center rounded-full bg-surface-2 text-xs font-semibold text-fg ring-1 ring-border hover:ring-border-strong cursor-pointer"
      >
        {initials || "?"}
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Content
          align="end"
          sideOffset={8}
          className="z-50 w-64 rounded-xl border border-border bg-surface p-1.5 shadow-card animate-fade-in"
        >
          <div className="px-2.5 py-2">
            <p className="truncate text-sm font-medium">{u.name}</p>
            <p className="truncate text-xs text-muted">{u.email}</p>
          </div>
          <div className="flex items-center justify-between px-2.5 py-2">
            <span className="text-xs text-muted">Theme</span>
            <ThemeToggle />
          </div>
          <Menu.Separator className="my-1 h-px bg-border" />
          <Menu.Item
            onSelect={async () => {
              await signOut();
              router.replace("/signin");
            }}
            className="flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-sm outline-none data-[highlighted]:bg-surface-2"
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
      <div className="flex min-h-0 flex-1">
        <aside className="hidden w-64 shrink-0 border-r border-border bg-bg-subtle lg:block">
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
                <Dialog.Content className="fixed inset-y-0 left-0 z-50 flex w-[82vw] max-w-xs flex-col bg-bg px-3 py-4 shadow-card animate-fade-in">
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
              <Logo className="text-sm" />
            </Link>
            <div className="ml-auto flex items-center gap-2">
              <ThemeToggle className="hidden sm:inline-flex" />
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
