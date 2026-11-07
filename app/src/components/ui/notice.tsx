import * as React from "react";
import { cn } from "@/lib/cn";
import { staleLine } from "@/lib/home";

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
  const { text, reference } = splitReference(message);
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      data-testid={tone === "notice" ? "server-notice" : undefined}
      className={cn(
        "flex items-start gap-3 text-row text-fg",
        tone === "notice" ? "rounded-lg bg-risk-soft px-3.5 py-2.5" : tone === "info" ? "py-1 text-muted" : "rounded-lg bg-late-soft px-3.5 py-2.5",
        className,
      )}
    >
      <div className="flex-1">
        <p>{text}</p>
        {reference && <p className="mt-0.5 text-meta text-muted">Reference {reference}</p>}
      </div>
      {onRetry && (
        <button type="button" onClick={onRetry} className="shrink-0 font-medium text-fg underline underline-offset-2 hover:no-underline cursor-pointer">
          Try again
        </button>
      )}
    </div>
  );
}

export const BUSY_RETRY = "The server is busy. Retrying in a few seconds.";

/**
 * A load that failed: a busy server is a calm notice that tries again by itself (once,
 * after `after` ms); anything else is the plain error with its retry.
 */
export function LoadProblem({ message, busy, onRetry, after = 20_000, className }: { message: string; busy?: boolean; onRetry: () => void; after?: number; className?: string }) {
  React.useEffect(() => {
    if (!busy) return;
    const t = setTimeout(onRetry, after);
    return () => clearTimeout(t);
  }, [busy, onRetry, after]);
  return <ErrorNotice message={busy ? BUSY_RETRY : message} onRetry={onRetry} tone={busy ? "notice" : "error"} className={className} />;
}

/** The page's age, when the server answered from its last good read: "As of 9:08 PM ET · refreshing". */
export function StaleNote({ since, className }: { since?: string | null; className?: string }) {
  const line = staleLine(since);
  if (!line) return null;
  return (
    <p className={cn("text-meta text-muted", className)} data-testid="stale-page" role="status">
      {line}
    </p>
  );
}
