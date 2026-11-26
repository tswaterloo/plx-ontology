import { Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";
import { eastern } from "@/lib/format";

/** A quiet inline indicator shown while a page revalidates cached data in the background.
 *  Renders nothing when `at` is null (the answer is current). */
export function StoredAge({ at, className }: { at: number | null; className?: string }) {
  if (at === null) return null;
  const when = eastern(new Date(at).toISOString());
  return (
    <p
      role="status"
      data-status="stale"
      data-testid="stored-age"
      className={cn("inline-flex items-center gap-1.5 text-[11.5px] leading-4 text-faint tabular-nums", className)}
    >
      <Loader2 className="size-3 shrink-0 animate-spin text-faint" aria-hidden />
      <span>{when ? `Updated ${when} · Refreshing` : "Updating"}</span>
    </p>
  );
}
