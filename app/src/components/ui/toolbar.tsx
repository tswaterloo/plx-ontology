"use client";

import Link from "next/link";
import * as React from "react";
import { Pin, PinOff } from "lucide-react";
import { cn } from "@/lib/cn";
import { pinsFrom, togglePin } from "@/lib/pins";
import { useSaved } from "@/lib/saved";
import { MobileAccount, MobileSearch } from "../shell";
import { useAuth } from "../auth";

/** Pin this page to My work and the sidebar (or unpin it). `label`: what the pin says. */
export function PinButton({ label }: { label: string }) {
  const items = useSaved();
  const { state } = useAuth();
  const me = state.status === "signed-in" ? state.me : null;
  // the page as it is now, query included (a tab or filter is part of what is pinned)
  const [href, setHref] = React.useState("");
  React.useEffect(() => {
    const now = window.location.pathname + window.location.search;
    if (now !== href) setHref(now);
  });
  if (!me || !href) return null;
  const pinned = pinsFrom(items, me).pins.some((p) => p.href === href);
  return (
    <button
      type="button"
      onClick={() => togglePin({ href, label }, items, me)}
      aria-pressed={pinned}
      title={pinned ? "Unpin from My work" : "Pin to My work"}
      aria-label={pinned ? `Unpin ${label} from My work` : `Pin ${label} to My work`}
      className="inline-flex size-8 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg cursor-pointer max-lg:hidden"
      data-testid="pin-page"
    >
      {pinned ? <PinOff className="size-[15px]" aria-hidden /> : <Pin className="size-[15px]" aria-hidden />}
    </button>
  );
}

/**
 * A page's toolbar: the 52 px line every pane shares. The title (with the place it sits in
 * before it, when there is one) on the left, the page's actions on the right; on a phone the
 * search and account buttons join them. Sticks to the top of the pane as it scrolls.
 */
export function Toolbar({
  title,
  parent,
  actions,
  className,
  testId,
  heading,
  pin,
}: {
  /** The page's own heading (h1) when nothing else on the page names it. */
  heading?: boolean;
  title: React.ReactNode;
  /** What a pin of this page says; a plain-text title is used when this is not given. False: no pin button. */
  pin?: string | false;
  parent?: { label: string; href: string };
  actions?: React.ReactNode;
  className?: string;
  testId?: string;
}) {
  return (
    <header
      className={cn("sticky top-0 z-20 flex h-[52px] shrink-0 items-center gap-3 border-b border-border bg-bg px-4 lg:px-5", className)}
      data-testid={testId}
    >
      <div className="flex min-w-0 flex-1 items-center gap-1.5 text-[13px]">
        {parent && (
          <>
            <Link href={parent.href} className="shrink-0 text-muted hover:text-fg">
              {parent.label}
            </Link>
            <span className="text-faint" aria-hidden>
              ›
            </span>
          </>
        )}
        {React.createElement(
          heading ? "h1" : "span",
          { className: cn("min-w-0 truncate", parent ? "font-medium" : "text-[15px] font-semibold tracking-[-0.015em]") },
          title,
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-0.5">{actions}</div>}
      {pin !== false && (pin || (typeof title === "string" && title)) && <PinButton label={(pin || title) as string} />}
      <div className="flex items-center lg:hidden">
        <MobileSearch />
        <MobileAccount quiet={!!actions} />
      </div>
    </header>
  );
}

/** A toolbar button: quiet by default; `primary` for the one main action. */
export function ToolbarButton({ primary, className, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { primary?: boolean }) {
  return (
    <button
      type="button"
      className={cn(
        "inline-flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-[13px] cursor-pointer [&_svg]:size-[15px]",
        primary ? "ml-1 bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-3 font-medium text-fg hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)]" : "text-muted hover:bg-hover hover:text-fg",
        className,
      )}
      {...props}
    />
  );
}
