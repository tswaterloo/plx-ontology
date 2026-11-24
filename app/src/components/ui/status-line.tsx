import { cn } from "@/lib/cn";
import { statusText, type Value } from "@/lib/value";

/** The one line that says anything not `ok` about a served value (contract C1): how old,
 *  switched off, not recorded, reading failed with its source. `ok` renders nothing. */
export function StatusLine({ value, className, now }: { value: Pick<Value, "status" | "as_of" | "why">; className?: string; now?: Date }) {
  const text = statusText(value, now);
  if (!text) return null;
  return (
    <p role="status" data-status={value.status} className={cn("text-[12px] text-muted", value.status === "failed" && "text-danger", className)}>
      {text}
    </p>
  );
}
