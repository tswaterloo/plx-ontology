import * as React from "react";
import { cn } from "@/lib/cn";

/** A section of a page: a hairline above, its heading, its content. `Panel` is the boxed surface (a draft, the sign-in form, a document). */
export function Card({ className, ...props }: React.ComponentProps<"div">) {
  return <div data-card="" className={cn("border-t border-rule", className)} {...props} />;
}

export function CardHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex min-h-10 items-center justify-between gap-3 py-2 text-meta text-muted", className)} {...props} />;
}

export function CardTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return <h2 className={cn("text-section text-fg", className)} {...props} />;
}

export function CardBody({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("pb-2", className)} {...props} />;
}

/** A boxed surface: hairline border, small radius, no shadow. */
export function Panel({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-lg border border-border bg-surface", className)} {...props} />;
}
