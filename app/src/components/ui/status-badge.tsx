import * as React from "react";
import { cn } from "@/lib/cn";
import { type Shown, type Status } from "@/lib/status";
import { StatusGlyph } from "./glyphs";

/** A status in at most three words: its shape, then the words in ink. No fill, no coloured text. */
export function StatusBadge({
  status,
  label,
  shown,
  dot: _dot = false,
  className,
  ...props
}: Omit<React.HTMLAttributes<HTMLSpanElement>, "children"> & { status?: Status; label?: string; shown?: Shown | null; dot?: boolean }) {
  const s = shown?.status ?? status ?? "neutral";
  const text = shown?.label ?? label;
  if (!text) return null;
  return (
    <span data-status={s} className={cn("inline-flex h-5 shrink-0 items-center gap-1.5 whitespace-nowrap text-[12px] font-medium text-fg", className)} {...props}>
      <StatusGlyph status={s} size={13} />
      {text}
    </span>
  );
}

/** The quiet form for rows and lists: the shape and the words; a neutral state is said in grey with no shape. */
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
  return (
    <span data-status={s} className={cn("inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap text-meta", s === "neutral" ? "text-muted" : "text-fg", className)} {...props}>
      {s !== "neutral" && <StatusGlyph status={s} size={12} />}
      {text}
    </span>
  );
}
