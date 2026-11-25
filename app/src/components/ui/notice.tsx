import * as React from "react";
import { cn } from "@/lib/cn";
import { ACCESS_NOTES, FINAL_ERRORS } from "@/lib/errors";
import { humanizeIsoTimestamps } from "@/lib/format";
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
  tone: asked = "error",
}: {
  message: string;
  onRetry?: () => void;
  className?: string;
  /** "notice": a server state (busy, switched off), a calm banner. "info": data age and access notes. */
  tone?: "error" | "notice" | "info";
}) {
  const { text: rawText, reference } = splitReference(message);
  const text = humanizeIsoTimestamps(rawText);
  // a missing record or a refused access is the same on a second try: no "Try again" for those
  const retry = FINAL_ERRORS.has(text) ? undefined : onRetry;
  // viewing as someone, a page outside My work, alerts and drafts says so as a note, not an error
  const tone = ACCESS_NOTES.has(text) ? "info" : asked;
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      data-testid={tone === "notice" ? "server-notice" : undefined}
      className={cn(
        "flex items-start gap-3 text-[12.5px] leading-5 text-fg",
        tone === "notice"
          ? "rounded-md border border-border border-l-2 border-l-risk/75 bg-surface/60 px-3 py-2"
          : tone === "info"
            ? "py-1 text-muted"
            : "rounded-md border border-border border-l-2 border-l-late/75 bg-surface/60 px-3 py-2",
        className,
      )}
    >
      <div className="flex-1">
        <p>{text}</p>
        {reference && <p className="mt-0.5 text-meta text-muted">Reference {reference}</p>}
      </div>
      {retry && (
        <button type="button" onClick={retry} className="shrink-0 font-medium text-fg underline underline-offset-2 hover:no-underline cursor-pointer">
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
export function StaleNote({ since, readAt, className, onReturn = false }: { since?: string | null; readAt?: string | null; className?: string; onReturn?: boolean }) {
  const line = staleLine(since, readAt, Date.now(), onReturn);
  if (!line) return null;
  return (
    <p className={cn("text-meta text-muted", className)} data-testid="stale-page" role="status">
      {line}
    </p>
  );
}

type Report = (message: string | null, from: symbol) => void;
const OneProblemSlot = React.createContext<Report | null>(null);

/**
 * One problem banner for a whole card: every part inside `provide(...)` that uses
 * `useProblem` reports to it, and so does the card itself through `set`. The newest failure
 * replaces the one shown, so two failed clicks never stack two boxes; a part that clears its
 * own problem clears the banner only while it is still that part's. Put `banner` where the
 * card shows it.
 */
export function useProblemSlot(): {
  provide: (children: React.ReactNode) => React.ReactNode;
  banner: React.ReactNode;
  set: (message: string | null) => void;
} {
  const [shown, setShown] = React.useState<{ message: string; from: symbol } | null>(null);
  const [own] = React.useState(() => Symbol("card"));
  const report = React.useCallback<Report>((message, from) => {
    setShown((cur) => (message ? { message, from } : cur?.from === from ? null : cur));
  }, []);
  const set = React.useCallback((m: string | null) => report(m, own), [report, own]);
  const provide = (children: React.ReactNode) => <OneProblemSlot.Provider value={report}>{children}</OneProblemSlot.Provider>;
  return { provide, banner: shown ? <ErrorNotice message={shown.message} /> : null, set };
}

/**
 * A part's problem: `[message, set, banner]`. Inside a card's `useProblemSlot` the message
 * goes to the card's one banner and `banner` is null; anywhere else `banner` is this part's
 * own notice.
 */
export function useProblem(): [string | null, (message: string | null) => void, React.ReactNode] {
  const slot = React.useContext(OneProblemSlot);
  const [me] = React.useState(() => Symbol("problem"));
  const [message, setMessage] = React.useState<string | null>(null);
  const set = React.useCallback(
    (m: string | null) => {
      setMessage(m);
      slot?.(m, me);
    },
    [slot, me],
  );
  React.useEffect(() => () => slot?.(null, me), [slot, me]);
  return [message, set, !slot && message ? <ErrorNotice message={message} /> : null];
}
