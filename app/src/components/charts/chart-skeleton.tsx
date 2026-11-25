import { cn } from "@/lib/cn";

// bar heights of the placeholder: a shape that reads as "a chart is coming" without claiming any data
const BARS = [
  "h-[58%] [animation-delay:0ms]",
  "h-[82%] [animation-delay:90ms]",
  "h-[46%] [animation-delay:180ms]",
  "h-[70%] [animation-delay:270ms]",
  "h-[38%] [animation-delay:360ms]",
  "h-[64%] [animation-delay:450ms]",
  "h-[52%] [animation-delay:540ms]",
  "h-[76%] [animation-delay:630ms]",
];

/** A chart that is still being read or built: faint bars the size of the chart, with a gentle
 * shimmer, and what is happening in words under it (``step``: the stream's own words when it gives
 * one). Holds the chart's height so the tile does not jump when the chart arrives. */
export function ChartSkeleton({ step, className }: { step?: string; className?: string }) {
  return (
    <div className={cn("space-y-2", className)} data-testid="chart-skeleton" aria-busy>
      <div aria-hidden className="flex h-48 items-end gap-3 border-b border-border px-2">
        {BARS.map((cls, i) => <div key={i} className={cn("skeleton skeleton-shimmer flex-1", cls)} />)}
      </div>
      {step && <p role="status" className="text-[13px] text-muted">{step}</p>}
    </div>
  );
}
