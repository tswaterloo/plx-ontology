"use client";

import { useEffect, useState } from "react";
import { cn } from "@/lib/cn";

/** After this long a read still under way says so, so a slow server never looks stuck. */
export const SLOW_MS = 8_000;
export const SLOW_WORDS = "Taking longer than usual. Still reading.";
/** After this long a wait stops: the region says what did not arrive and offers Retry
 * (a read that lands later still replaces it). No loading state waits forever. */
export const LIMIT_MS = 60_000;
/** A record page's read stops waiting after this (core pages measured up to 110 s, 11-17). */
export const RECORD_LIMIT_MS = 150_000;

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

/** "Reading your inbox" -> "reading your inbox": the wait's words inside a sentence. */
function inSentence(label: string): string {
  return /^[A-Z][a-z]/.test(label) ? label[0].toLowerCase() + label.slice(1) : label;
}

/** What a wait that ran past its limit shows: what did not arrive, and Retry (by default the
 * page is read again, which repeats every read on it). */
export function TimedOut({ label, onRetry, className, ms = LIMIT_MS }: { label: string; onRetry?: () => void; className?: string; ms?: number }) {
  const secs = Math.round(ms / 1000);
  return (
    <div role="alert" className={cn("py-2 text-meta text-muted", className)} data-testid="pending-timeout">
      <span className="text-fg">Stopped waiting: {inSentence(label)} took over {secs >= 120 ? `${Math.round(secs / 60)} minutes` : secs === 60 ? "a minute" : `${secs} seconds`}.</span>{" "}
      <button type="button" onClick={onRetry ?? (() => window.location.reload())} className="font-medium text-fg underline underline-offset-2 cursor-pointer" data-testid="pending-retry">
        Retry
      </button>
    </div>
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
/** ``limit``: after this long the wait stops and says so, with Retry (``onRetry``, else the page
 * is read again). */
export function Pending({ label, className, testId, page, after = 0, limit = LIMIT_MS, onRetry }: { label: string; className?: string; testId?: string; page?: boolean; after?: number; limit?: number; onRetry?: () => void }) {
  const slow = useSlow();
  const shown = useSlow(after) || after <= 0;
  const over = useSlow(limit);
  if (over) return <TimedOut label={label} onRetry={onRetry} ms={limit} className={cn(page && "flex min-h-[40vh] flex-col items-center justify-center text-center", className)} />;
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
