"use client";

// The record page's vocabulary, shared by every record type: the same section titles, figures,
// properties, order line and list rows as Today's detail pane, drawn at page size.

import { ArrowUpRight } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { cn } from "@/lib/cn";
import { businessToday, formatDate } from "@/lib/format";
import type { Shown, Status } from "@/lib/status";
import type { Source } from "@/lib/types";
import { EvidenceDrawer } from "../source-chip";
import { daysIn, DoneRing, lateFigure, OpenRing, StatusGlyph } from "../ui/glyphs";
import { STATUS_INK } from "../ui/status-badge";

export function SectionTitle({ children, aside, as: Tag = "h2", className }: { children: React.ReactNode; aside?: React.ReactNode; as?: "h2" | "h3"; className?: string }) {
  return (
    <div className={cn("mb-3 flex min-h-4 items-baseline justify-between gap-3", className)}>
      <Tag className="text-[12px] font-medium leading-4 text-muted">{children}</Tag>
      {aside}
    </div>
  );
}

/** A quiet text button for a section's corner: "All 12", "Fewer". */
export function MoreButton({ children, onClick, testId, expanded }: { children: React.ReactNode; onClick: () => void; testId?: string; expanded?: boolean }) {
  return (
    <button type="button" onClick={onClick} aria-expanded={expanded} className="text-[12px] font-medium text-muted hover:text-fg cursor-pointer" data-testid={testId}>
      {children}
    </button>
  );
}

/** A status as its shape: late with days sweeps the ring; the words beside it stay in ink. */
export function Glyph({ status, days, size = 14 }: { status: Status; days?: number | null; size?: number }) {
  if (status === "late") return null;
  if (status === "risk") return <OpenRing size={size} />;
  if (status === "ok") return <DoneRing size={size} />;
  if (status === "neutral") return <OpenRing size={size} tone="neutral" />;
  return <StatusGlyph status={status} size={size} />;
}

export { daysIn };

/** A status line's item: glyph, then the words in ink. */
export function Fact({ shown, className }: { shown: Shown; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 font-medium", shown.status === "neutral" ? "text-muted font-normal" : shown.status === "late" && daysIn(shown.label) != null ? lateFigure(daysIn(shown.label)) : STATUS_INK[shown.status], className)} data-status={shown.status}>
      <Glyph status={shown.status} />
      {shown.label}
    </span>
  );
}

/** One figure: a small label, the figure large, its unit beside it; words in a figure's place are said quietly. */
export function Figure({
  label,
  value,
  unit,
  status,
  days,
  source,
  children,
  testId,
}: {
  label: string;
  value: string;
  unit?: string | null;
  status?: Status;
  days?: number | null;
  source?: Source;
  children?: React.ReactNode;
  testId?: string;
}) {
  const [open, setOpen] = useState(false);
  const words = !/\d/.test(value);
  const flagged = status && status !== "neutral";
  const body = (
    <>
      <span className="flex items-center gap-1.5 text-[12px] leading-4 text-muted">
        {flagged && <Glyph status={status} days={days} size={12} />}
        {label}
      </span>
      <span className="mt-1 flex items-baseline gap-1.5 whitespace-nowrap">
        <span className={cn(words ? "text-[13px] leading-7 text-muted" : "text-[22px] leading-7 font-semibold tracking-[-0.02em] num", source && "group-hover:underline decoration-1 underline-offset-4")}>{value}</span>
        {unit && <span className="truncate text-[13px] text-muted">{unit}</span>}
      </span>
    </>
  );
  return (
    <div className="min-w-0" data-testid={testId}>
      {source ? (
        <>
          <button type="button" onClick={() => setOpen(true)} className="group block max-w-full text-left cursor-pointer" title="Open the source">
            {body}
          </button>
          {open && <EvidenceDrawer source={source} open={open} onOpenChange={setOpen} />}
        </>
      ) : (
        <div>{body}</div>
      )}
      {children}
    </div>
  );
}

/** A thin progress bar with its percentage, under a figure. */
export function Progress({ pct }: { pct: number }) {
  return (
    <div className="mt-2 flex items-center gap-2">
      <span className="relative h-1 w-24 overflow-hidden rounded-full bg-[color-mix(in_srgb,var(--fg)_10%,transparent)]">
        <span className="absolute inset-y-0 left-0 rounded-full bg-fg/70" style={{ width: `${Math.max(3, Math.min(100, pct))}%` }} />
      </span>
      <span className="text-[12px] num text-muted">{pct}%</span>
    </div>
  );
}

/** A label and its value in the inspector. */
export function Property({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[96px_minmax(0,1fr)] items-baseline gap-3 py-1">
      <dt className="text-[12px] text-muted">{label}</dt>
      <dd className="min-w-0 text-[13px] leading-5 break-words">{children}</dd>
    </div>
  );
}

/** A link row in the inspector: the name, an arrow on hover. */
export function LinkRow({ href, children, aside }: { href: string; children: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <Link href={href} className="group flex h-8 items-center justify-between gap-2 rounded-lg px-2 text-[13px] hover:bg-hover">
      <span className="min-w-0 truncate">{children}</span>
      <span className="flex shrink-0 items-center gap-2">
        {aside}
        <ArrowUpRight className="size-3.5 text-faint group-hover:text-fg" aria-hidden />
      </span>
    </Link>
  );
}

export const DAY = 86_400_000;

/** A day as a time: an ISO date, or BC's "Mar 28, 2026". */
export function dayOf(v: string | null | undefined): number | null {
  if (!v) return null;
  const d = /^\d{4}-\d{2}-\d{2}/.test(v) ? new Date(`${v.slice(0, 10)}T12:00:00Z`) : new Date(`${v} 12:00 UTC`);
  return Number.isNaN(d.getTime()) ? null : d.getTime();
}

const short = (t: number) => formatDate(new Date(t).toISOString().slice(0, 10), { short: true });

/**
 * The order's life on one line: ordered, needed, today. The days past the needed-by date are
 * hatched; with no needed-by date the line after the order is left open.
 */
export function OrderLine({ ordered, needed, daysLate, end: endLabel = "Today" }: { ordered: number | null; needed: number | null; daysLate?: number | null; end?: string }) {
  const now = Date.now();
  if (ordered == null && needed == null) return null;
  const start = ordered ?? needed!;
  const end = Math.max(now, needed ?? now);
  const span = Math.max(1, end - start);
  const at = (t: number) => ((t - start) / span) * 100;
  const late = needed != null && now > needed;
  const lateDays = late ? (daysLate ?? Math.round((now - needed!) / DAY)) : 0;
  const neededAt = needed != null ? at(needed) : null;
  return (
    <div className="pt-1" data-testid="order-line">
      <div className="relative h-[18px]">
        {late && neededAt != null && (
          <span className={cn("absolute top-0 -translate-x-1/2 text-[12px] leading-4 font-medium whitespace-nowrap", lateFigure(lateDays))} style={{ left: `${Math.min(88, Math.max(neededAt + (100 - neededAt) / 2, 12))}%` }}>
            {lateDays} days late
          </span>
        )}
      </div>
      <div className="relative mt-1 h-[6px] rounded-full bg-[color-mix(in_srgb,var(--fg)_10%,transparent)]">
        {neededAt != null ? (
          <>
            <span className="absolute inset-y-0 left-0 rounded-l-full bg-[color-mix(in_srgb,var(--fg)_22%,transparent)]" style={{ width: `${neededAt}%` }} />
            {late && (
              <span
                className="absolute inset-y-0 right-0 rounded-r-full"
                style={{ left: `${neededAt}%`, background: "repeating-linear-gradient(135deg, var(--dot-late) 0 2px, color-mix(in srgb, var(--dot-late) 28%, transparent) 2px 5px)" }}
              />
            )}
          </>
        ) : (
          <span className="absolute inset-0 rounded-full" style={{ background: "repeating-linear-gradient(90deg, color-mix(in srgb, var(--fg) 22%, transparent) 0 4px, transparent 4px 8px)" }} />
        )}
        {[ordered != null ? at(ordered) : null, neededAt, 100].map((p, i) =>
          p == null ? null : (
            <span
              key={i}
              className={cn("absolute top-1/2 size-[10px] -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-bg", i === 2 ? (late ? "bg-dot-late" : "bg-fg") : "bg-[color-mix(in_srgb,var(--fg)_55%,var(--bg))]")}
              style={{ left: `${p}%` }}
            />
          ),
        )}
      </div>
      <div className="relative mt-2 h-8 text-[12px] leading-4">
        {ordered != null && (
          <span className="absolute left-0 top-0">
            <span className="block text-muted">Ordered</span>
            <span className="block font-medium">{short(ordered)}</span>
          </span>
        )}
        {needed != null && neededAt != null && (
          <span className="absolute top-0 -translate-x-1/2 text-center" style={{ left: `${Math.min(70, Math.max(neededAt, 30))}%` }}>
            <span className="block text-muted">Needed by</span>
            <span className="block font-medium">{short(needed)}</span>
          </span>
        )}
        {needed == null && (
          <span className="absolute top-0 left-1/2 -translate-x-1/2 text-center">
            <span className="block text-muted">Needed by</span>
            <span className="block font-medium">Not on record</span>
          </span>
        )}
        <span className="absolute right-0 top-0 text-right">
          <span className="block text-muted">{endLabel}</span>
          <span className="block font-medium">{formatDate(businessToday(new Date(now)), { short: true })}</span>
        </span>
      </div>
    </div>
  );
}

/** Lines of placeholder while a part of the page is read. */
export function Bars({ n = 3, className }: { n?: number; className?: string }) {
  return (
    <div className={cn("space-y-3 pt-1", className)} aria-hidden>
      {Array.from({ length: n }, (_, i) => (
        <div key={i} className={cn("skeleton h-3.5", ["w-[72%]", "w-[58%]", "w-[66%]", "w-[44%]", "w-[52%]"][i % 5])} />
      ))}
    </div>
  );
}
