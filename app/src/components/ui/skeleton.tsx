"use client";

import { useEffect, useState } from "react";
import { cn } from "@/lib/cn";

/** After this long a read still under way says so, so a slow server never looks stuck. */
export const SLOW_MS = 8_000;
export const SLOW_WORDS = "Taking longer than usual. Still reading.";

/** True once ``ms`` have passed since the caller mounted. */
export function useSlow(ms = SLOW_MS): boolean {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setSlow(true), ms);
    return () => clearTimeout(t);
  }, [ms]);
  return slow;
}

/** The line a long read shows under its loading state (nothing until ``SLOW_MS``). */
export function SlowNote({ className }: { className?: string }) {
  const slow = useSlow();
  if (!slow) return null;
  return (
    <p role="status" className={cn("px-3 py-2 text-meta text-muted", className)} data-testid="slow-note">
      {SLOW_WORDS}
    </p>
  );
}

/** A still, faint bar for one line of text whose words are not known yet. */
export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden className={cn("skeleton h-4 w-full", className)} />;
}

/** The one way a part of a page says it is loading: what is being read, in words. */
/** A wait in words. ``page``: the whole page is waiting, so the words sit centred in the
 * pane (a bare line at the content column's edge reads as misplaced). */
/** ``after``: say nothing for the first ``after`` ms (a wait that is usually over by then
 * never flashes its words, nor sits in the page as a second loading line). */
export function Pending({ label, className, testId, page, after = 0 }: { label: string; className?: string; testId?: string; page?: boolean; after?: number }) {
  const slow = useSlow();
  const shown = useSlow(after) || after <= 0;
  if (!shown) return null;
  return (
    <p
      role="status"
      aria-label={label}
      className={cn("py-2 text-meta text-muted", page && "flex min-h-[40vh] flex-col items-center justify-center text-center", className)}
      data-testid={testId}
      data-pending=""
    >
      <span>{label}</span>
      {slow && <span className="mt-1 block text-faint" data-testid="slow-note">{SLOW_WORDS}</span>}
    </p>
  );
}

export function SkeletonLines({ label = "Loading" }: { lines?: number; label?: string }) {
  return <Pending label={label} />;
}

/** A figures line while it loads: nothing (the header says what is being read). */
export function SkeletonStats(_: { count?: number }) {
  return null;
}

export function SkeletonRows({ label = "Loading" }: { rows?: number; label?: string }) {
  return <Pending label={label} />;
}
