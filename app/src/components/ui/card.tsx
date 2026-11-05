import * as React from "react";
import { cn } from "@/lib/cn";

/**
 * A section of a page: an ink rule above, its heading, its content. No box: grouping comes
 * from the rule and the space around it. `Panel` is the boxed surface for things that sit
 * apart (a draft, a sign-in form, a document).
 */
export function Card({ className, ...props }: React.ComponentProps<"div">) {
  return <div data-card="" className={cn("border-t border-rule", className)} {...props} />;
}

export function CardHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex min-h-11 items-center justify-between gap-3 py-2.5 text-meta text-muted", className)} {...props} />;
}

export function CardTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return <h2 className={cn("text-section text-fg", className)} {...props} />;
}

export function CardBody({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("pb-2", className)} {...props} />;
}

/** A boxed surface: hairline border, 4 px radius, no shadow. */
export function Panel({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-lg border border-border bg-surface", className)} {...props} />;
}
