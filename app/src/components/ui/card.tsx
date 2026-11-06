import * as React from "react";
import { cn } from "@/lib/cn";

/** A section of a page: its small heading, then its content; sections are set apart by space, not rules. `Panel` is the lifted sheet (a draft, the sign-in form, a document). */
export function Card({ className, ...props }: React.ComponentProps<"div">) {
  return <div data-card="" className={cn(className)} {...props} />;
}

export function CardHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("mb-2 flex min-h-6 items-center justify-between gap-3 text-meta text-muted", className)} {...props} />;
}

export function CardTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return <h2 className={cn("text-[12px] leading-4 font-medium text-muted", className)} {...props} />;
}

export function CardBody({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn(className)} {...props} />;
}

/** A lifted sheet: the app's one surface for something you read or fill in as a whole. */
export function Panel({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-xl bg-bg shadow-lift", className)} {...props} />;
}
