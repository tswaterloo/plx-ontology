import * as React from "react";
import { cn } from "@/lib/cn";
import { STATUS_CLASS, type Shown, type Status } from "@/lib/status";

/** A status in at most three words: 22 px, 6 px radius, soft fill, line border, optional dot. */
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
      className={cn("inline-flex h-[22px] shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md border px-2 text-label", c.soft, c.fg, c.line, className)}
      {...props}
    >
      {dot && <span className={cn("size-1.5 rounded-full", c.dot)} aria-hidden />}
      {text}
    </span>
  );
}
