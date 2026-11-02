import { AlertCircle, Info } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/cn";

/** "Reference 8e081121" out of a message that carries "(incident 8e081121)" or "incident 8e081121". */
export function splitReference(message: string): { text: string; reference: string | null } {
  const m = /\s*\(?(?:quote )?incident ([0-9a-f]{6,})\)?\.?/i.exec(message);
  if (!m) return { text: message, reference: null };
  const text = (message.slice(0, m.index) + message.slice(m.index + m[0].length)).replace(/[;,]\s*if it keeps happening,?\s*(tell the Workbench team)?\s*\.?$/i, ".").replace(/\s+\./g, ".").trim();
  return { text, reference: m[1] };
}

/** Plain-language error block with an optional retry; an incident id moves to a "Reference" line. */
export function ErrorNotice({
  message,
  onRetry,
  className,
  tone = "error",
}: {
  message: string;
  onRetry?: () => void;
  className?: string;
  /** "notice": a server state (busy, switched off), a calm banner. "info": data age and access notes. */
  tone?: "error" | "notice" | "info";
}) {
  const Icon = tone === "error" ? AlertCircle : Info;
  const { text, reference } = splitReference(message);
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      data-testid={tone === "notice" ? "server-notice" : undefined}
      className={cn(
        "flex items-start gap-3 rounded-xl border p-4 text-row text-fg",
        tone === "notice" ? "border-risk-line bg-risk-soft" : tone === "info" ? "border-border bg-surface-2" : "border-late-line bg-late-soft",
        className,
      )}
    >
      <Icon className={cn("mt-0.5 size-4 shrink-0", tone === "notice" ? "text-risk" : tone === "info" ? "text-muted" : "text-late")} aria-hidden />
      <div className="flex-1">
        <p>{text}</p>
        {reference && <p className="mt-0.5 text-meta text-muted">Reference {reference}</p>}
      </div>
      {onRetry && (
        <button type="button" onClick={onRetry} className="font-medium text-accent hover:underline cursor-pointer">
          Try again
        </button>
      )}
    </div>
  );
}
