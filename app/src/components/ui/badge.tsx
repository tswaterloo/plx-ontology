import * as React from "react";
import { cn } from "@/lib/cn";
import type { Tone } from "@/lib/types";

const TONES: Record<Tone, string> = {
  neutral: "bg-surface-2 text-fg border-border",
  good: "bg-good-soft text-good border-good/30",
  warn: "bg-warn-soft text-warn border-warn/30",
  bad: "bg-bad-soft text-bad border-bad/30",
  info: "bg-info-soft text-info border-info/30",
};

export function Badge({
  tone = "neutral",
  className,
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & { tone?: Tone }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium",
        TONES[tone] ?? TONES.neutral,
        className,
      )}
      {...props}
    />
  );
}
