import { Children } from "react";
import Link from "next/link";
import * as React from "react";
import { cn } from "@/lib/cn";
import { STATUS_CLASS, type Status } from "@/lib/status";

/**
 * One cell of a figures strip: a small label, the figure in the serif (in its status colour
 * when it is a problem), one line of context. No box, no icon, no stripe.
 */
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
  const c = status && status !== "neutral" && status !== "ok" ? STATUS_CLASS[status] : null;
  // a long value ("24 Jun 2026", "$1,840.00 CAD") steps down so it stays on one line
  const long = typeof value === "string" && value.length > 8;
  const longer = typeof value === "string" && value.length > 11;
  // a figure slot holding words ("none found") is said quietly, not set as a number
  const words = typeof value === "string" && !/\d/.test(value);
  const body = (
    <>
      <span className="block text-eyebrow uppercase text-muted group-hover:text-fg">{label}</span>
      <span className={cn("mt-1.5 block break-words text-stat", long && "max-sm:text-[20px] max-sm:leading-[26px]", longer && "text-[20px] leading-[26px] sm:text-[22px] sm:leading-7", words ? "text-[20px] leading-8 text-muted italic" : c ? c.fg : "text-fg")}>{value}</span>
      {caption && <span className="mt-1 block truncate text-meta text-muted">{caption}</span>}
    </>
  );
  const cls = cn("group relative block min-w-0 px-4 py-3.5 first:pl-0 max-lg:px-0", (href || onClick) && "transition-colors", className);
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

const LG_COLS: Record<number, string> = { 1: "lg:grid-cols-1", 2: "lg:grid-cols-2", 3: "lg:grid-cols-3", 4: "lg:grid-cols-4" };

/**
 * A figures strip: cells side by side between two rules, divided by thin vertical rules from
 * 1024 px; below that, two to a row with hairlines between the rows.
 */
export function StatRow({ children, className, stack = false }: { children: React.ReactNode; className?: string; stack?: boolean }) {
  const n = Math.min(4, Math.max(1, Children.toArray(children).filter(Boolean).length));
  return (
    <div
      className={cn(
        "grid grid-cols-2 gap-x-6 border-y border-border-strong max-lg:divide-y max-lg:divide-border",
        "max-lg:[&>*:nth-child(2)]:!border-t-0 [&>*:last-child:nth-child(odd)]:col-span-2 lg:[&>*:last-child:nth-child(odd)]:col-span-1",
        // stack: from 1024 px the figures run down a column (a margin ledger beside a drawing)
        stack ? "lg:grid-cols-1 lg:divide-y lg:divide-border lg:[&>*]:!px-0" : cn("lg:gap-x-0 lg:divide-x lg:divide-border-strong", LG_COLS[n]),
        className,
      )}
    >
      {children}
    </div>
  );
}
