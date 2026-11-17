"use client";

import { cn } from "@/lib/cn";

/** The end of a cut list: one control that opens the rest in place and folds it back. A list
 *  is never ended with "and N more" in prose; what was never loaded is said by its caller. */
export function ShowMore({ hidden, open, onToggle, className }: { hidden: number; open: boolean; onToggle: () => void; className?: string }) {
  if (hidden <= 0) return null;
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      className={cn("cursor-pointer text-[12px] font-medium text-muted hover:text-fg", className)}
      data-testid="show-more"
    >
      {open ? "Show fewer" : `Show ${hidden} more`}
    </button>
  );
}
