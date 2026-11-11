import * as React from "react";
import { cn } from "@/lib/cn";
import type { Tone } from "@/lib/types";

const TONES: Record<Tone, string> = {
  neutral: "bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] text-fg border-transparent",
  good: "bg-ok-soft text-ok border-ok-line",
  warn: "bg-risk-soft text-risk border-risk-line",
  bad: "bg-late-soft text-late border-late-line",
  info: "bg-new-soft text-new border-new-line",
};

/** A neutral tag (or an older tone). Status badges use StatusBadge. */
export function Badge({
  tone = "neutral",
  className,
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & { tone?: Tone }) {
  return (
    <span
      className={cn(
        "inline-flex min-h-5 items-center gap-1 rounded-[2px] border px-1.5 text-label",
        TONES[tone] ?? TONES.neutral,
        className,
      )}
      {...props}
    />
  );
}
