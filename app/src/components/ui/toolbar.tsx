"use client";

import Link from "next/link";
import * as React from "react";
import { cn } from "@/lib/cn";
import { MobileAccount, MobileSearch } from "../shell";

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
}: {
  /** The page's own heading (h1) when nothing else on the page names it. */
  heading?: boolean;
  title: React.ReactNode;
  parent?: { label: string; href: string };
  actions?: React.ReactNode;
  className?: string;
  testId?: string;
}) {
  return (
    <header
      className={cn("sticky top-0 z-20 flex h-[52px] shrink-0 items-center gap-3 border-b border-border bg-[color-mix(in_srgb,var(--bg)_90%,transparent)] px-4 backdrop-blur-xl lg:px-5", className)}
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
