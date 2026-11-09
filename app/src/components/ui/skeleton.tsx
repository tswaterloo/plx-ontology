import { cn } from "@/lib/cn";

/** A still, faint bar for one line of text whose words are not known yet. */
export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden className={cn("skeleton h-4 w-full", className)} />;
}

/** The one way a part of a page says it is loading: what is being read, in words. */
export function Pending({ label, className, testId }: { label: string; className?: string; testId?: string }) {
  return (
    <p role="status" aria-label={label} className={cn("py-2 text-meta text-muted", className)} data-testid={testId} data-pending="">
      <span>{label}</span>
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
