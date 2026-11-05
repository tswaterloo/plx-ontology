import * as React from "react";
import { cn } from "@/lib/cn";
import { STATUS_CLASS, type Shown, type Status } from "@/lib/status";

/** A status in at most three words: 20 px, 2 px radius, soft fill, no border; a data problem gets a dotted line. */
export function StatusBadge({
  status,
  label,
  shown,
  dot = false,
  className,
  ...props
}: Omit<React.HTMLAttributes<HTMLSpanElement>, "children"> & { status?: Status; label?: string; shown?: Shown | null; dot?: boolean }) {
  const s = shown?.status ?? status ?? "neutral";
  const text = shown?.label ?? label;
  if (!text) return null;
  const c = STATUS_CLASS[s];
  return (
    <span
      data-status={s}
      className={cn(
        "inline-flex h-5 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-sm px-1.5 text-label",
        c.soft,
        c.fg,
        s === "suspect" && cn("border border-dotted", c.line),
        className,
      )}
      {...props}
    >
      {dot && <span className={cn("size-1.5", c.dot)} aria-hidden />}
      {text}
    </span>
  );
}

/** The quiet form for rows and lists: a small square in the status colour and the word, no fill. */
export function StatusWord({
  status,
  label,
  shown,
  className,
  ...props
}: Omit<React.HTMLAttributes<HTMLSpanElement>, "children"> & { status?: Status; label?: string; shown?: Shown | null }) {
  const s = shown?.status ?? status ?? "neutral";
  const text = shown?.label ?? label;
  if (!text) return null;
  const c = STATUS_CLASS[s];
  // a neutral state is said, not signalled: plain faint words, no square
  return (
    <span data-status={s} className={cn("inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap text-label", s === "neutral" ? "text-faint" : cn("font-medium", c.fg), className)} {...props}>
      {s !== "neutral" && <span className={cn("size-1.5 shrink-0", c.dot)} aria-hidden />}
      {text}
    </span>
  );
}
