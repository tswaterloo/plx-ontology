import Link from "next/link";
import * as React from "react";
import { cn } from "@/lib/cn";
import { STATUS_CLASS, type Status } from "@/lib/status";

/** A number tile: label, value, caption; a status adds a 3 px left rule and colours the value. */
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
  const c = status ? STATUS_CLASS[status] : null;
  // a long value ("24 Jun 2026") steps down on a phone so it stays on one line
  const long = typeof value === "string" && value.length > 8;
  const body = (
    <>
      <span className="block text-label uppercase text-muted">{label}</span>
      <span className={cn("mt-1 block break-words text-stat tabular-nums", long && "max-sm:text-[20px] max-sm:leading-[26px]", c ? c.fg : "text-fg")}>{value}</span>
      {caption && <span className="mt-0.5 block truncate text-meta text-muted">{caption}</span>}
    </>
  );
  const cls = cn(
    "relative block min-h-[84px] min-w-0 overflow-hidden rounded-xl border border-border bg-surface px-4 py-3 sm:min-h-[96px]",
    c && `before:absolute before:inset-y-0 before:left-0 before:w-[3px] ${c.rule}`,
    (href || onClick) && "transition-colors hover:bg-surface-2",
    className,
  );
  if (href)
    return (
      <Link href={href} className={cls} data-testid={testId}>
        {body}
      </Link>
    );
  if (onClick)
    return (
      <button type="button" onClick={onClick} className={cn(cls, "w-full text-left cursor-pointer")} data-testid={testId}>
        {body}
      </button>
    );
  return (
    <div className={cls} data-testid={testId}>
      {body}
    </div>
  );
}

/** Tiles in a row: 2 across below 1024 px, 4 across from 1024. */
export function StatRow({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("grid grid-cols-2 gap-3 lg:grid-cols-4", className)}>{children}</div>;
}
