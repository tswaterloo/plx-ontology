"use client";

import { useEffect, useState } from "react";
import { cn } from "@/lib/cn";
import { useWaiting } from "@/lib/ready";

/** After this long a read still under way says so, so a slow server never looks stuck. */
export const SLOW_MS = 8_000;
export const SLOW_WORDS = "Taking longer than usual. Still reading.";
/** After this long a wait also says how long it has gone on and offers "Read again". It never
 * stops waiting and never gives up (Guardrail 1); only a dead connection ends a read (lib/api
 * IDLE_STALL_MS). */
export const LIMIT_MS = 60_000;
/** The same for a record page, whose cold reads are the slowest. */
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

/** Whole seconds since mount, ticking every 5 s once past SLOW_MS (0 until then, or when off). */
export function useElapsed(on: boolean): number {
  const [secs, setSecs] = useState(0);
  useEffect(() => {
    if (!on) return;
    const start = Date.now();
    const t = setInterval(() => {
      const s = Math.round((Date.now() - start) / 1000);
      setSecs(s * 1000 >= SLOW_MS ? s : 0);
    }, 5_000);
    return () => clearInterval(t);
  }, [on]);
  return secs;
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

/** What a long wait adds under its placeholder: that it is still going, for how long, and a way
 * to ask again. The read keeps going; a late answer replaces everything. */
export function StillReading({ label, onRetry, className, since }: { label: string; onRetry?: () => void; className?: string; since?: number }) {
  const [now, setNow] = useState(() => Date.now());
  const [start] = useState(() => since ?? Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 5_000);
    return () => clearInterval(t);
  }, []);
  const secs = Math.max(0, Math.round((now - start) / 1000));
  const took = secs >= 120 ? `${Math.round(secs / 60)} minutes` : secs >= 60 ? "over a minute" : `${secs} seconds`;
  return (
    <div role="status" className={cn("py-2 text-meta text-muted", className)} data-testid="pending-still">
      <span className="text-fg">Still reading: {inSentence(label)}. {took} so far.</span>
      {onRetry && (
        <>{" "}<button type="button" onClick={onRetry} className="font-medium text-fg underline underline-offset-2 cursor-pointer" data-testid="pending-retry">Read again</button></>
      )}
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
export function Pending({ label, className, testId, page, after = 0, limit = LIMIT_MS, onRetry, bars = 3 }: { label: string; className?: string; testId?: string; page?: boolean; after?: number; limit?: number | null; onRetry?: () => void; bars?: number }) {
  const slow = useSlow();
  const shown = useSlow(after) || after <= 0;
  // ``limit={null}``: the wait never stops (a read that has no deadline, Guardrail 1); it says
  // it is still reading, with the time so far
  const timed = useSlow(limit ?? 2_000_000_000);
  const over = limit != null && timed;
  const waited = useElapsed(limit == null);
  useWaiting(!over);   // a placeholder (shown or not yet shown) means the page is not ready
  if (!shown) return null;
  const widths = ["w-[78%]", "w-[62%]", "w-[48%]", "w-[68%]"];
  return (
    <div
      role="status"
      aria-label={label}
      className={cn("py-3 text-meta text-muted", page && "flex min-h-[40vh] flex-col items-center justify-center text-center", className)}
      data-testid={testId}
      data-pending=""
    >
      {bars > 0 && (
        <div className={cn("mb-3 space-y-2.5", page ? "w-full max-w-sm" : "max-w-lg")} aria-hidden>
          {Array.from({ length: bars }, (_, i) => (
            <div key={i} className={cn("skeleton h-3.5", widths[i % widths.length], page && "mx-auto")} />
          ))}
        </div>
      )}
      <span className="text-[12px] text-muted">{label}</span>
      {slow && !over && <span className="mt-1 block text-faint" data-testid="slow-note">{SLOW_WORDS}{waited > 0 ? ` ${waited} s so far.` : ""}</span>}
      {over && <StillReading label={label} onRetry={onRetry} since={Date.now() - limit} />}
    </div>
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
