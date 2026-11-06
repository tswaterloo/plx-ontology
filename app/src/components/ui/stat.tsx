import Link from "next/link";
import * as React from "react";
import { cn } from "@/lib/cn";
import { type Status } from "@/lib/status";
import { StatusGlyph } from "./glyphs";

/** One figure in a summary line: a small label, the figure (in its status colour when it is a problem), one line of context. */
export function Stat({
  label,
  value,
  caption,
  status,
  href,
  onClick,
  className,
  testId,
}: {
  label: string;
  value: React.ReactNode;
  caption?: React.ReactNode;
  status?: Status;
  href?: string;
  onClick?: () => void;
  className?: string;
  testId?: string;
}) {
  const flagged = status && status !== "neutral";
  // a figure slot holding words ("none found") is said quietly, not set as a number
  const words = typeof value === "string" && !/\d/.test(value);
  const body = (
    <>
      <span className="flex items-center gap-1.5 text-meta text-muted">
        {flagged && <StatusGlyph status={status} size={12} />}
        {label}
      </span>
      <span className={cn("mt-0.5 block break-words tabular-nums", words ? "text-row leading-[26px] text-muted" : "text-[22px] leading-7 font-semibold tracking-[-0.02em] text-fg", (href || onClick) && "group-hover:underline")}>{value}</span>
      {caption && <span className="block truncate text-meta text-muted">{caption}</span>}
    </>
  );
  const cls = cn("group relative block min-w-0", className);
  if (href)
    return (
      <Link href={href} className={cls} data-testid={testId}>
        {body}
      </Link>
    );
  if (onClick)
    return (
      <button type="button" onClick={onClick} className={cn(cls, "text-left cursor-pointer")} data-testid={testId}>
        {body}
      </button>
    );
  return (
    <div className={cls} data-testid={testId}>
      {body}
    </div>
  );
}

/** Figures side by side, left-aligned, no boxes; they wrap on a narrow screen. */
export function StatRow({ children, className }: { children: React.ReactNode; className?: string; stack?: boolean }) {
  return <div className={cn("flex flex-wrap gap-x-10 gap-y-4 py-1", className)}>{children}</div>;
}
