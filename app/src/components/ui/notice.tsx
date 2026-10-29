import { AlertCircle, Info } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/cn";

/** Plain-language error block with an optional retry. */
export function ErrorNotice({
  message,
  onRetry,
  className,
  tone = "error",
}: {
  message: string;
  onRetry?: () => void;
  className?: string;
  /** "notice": a server state (busy, switched off), shown as a calm banner, not an error. */
  tone?: "error" | "notice";
}) {
  const Icon = tone === "notice" ? Info : AlertCircle;
  return (
    <div
      role={tone === "notice" ? "status" : "alert"}
      data-testid={tone === "notice" ? "server-notice" : undefined}
      className={cn(
        "flex items-start gap-3 rounded-xl border p-4 text-sm text-fg",
        tone === "notice" ? "border-warn/40 bg-warn-soft" : "border-bad/30 bg-bad-soft",
        className,
      )}
    >
      <Icon className={cn("mt-0.5 size-4 shrink-0", tone === "notice" ? "text-warn" : "text-bad")} aria-hidden />
      <p className="flex-1">{message}</p>
      {onRetry && (
        <button type="button" onClick={onRetry} className="font-medium text-accent hover:underline cursor-pointer">
          Try again
        </button>
      )}
    </div>
  );
}
