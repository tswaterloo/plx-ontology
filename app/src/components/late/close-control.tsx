"use client";

// Close a purchase order inside the Workbench: for everyone, with an optional reason, and undoable.
// Nothing is written to Business Central, the Portal or Sage; the words say so.

import { Loader2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { closePo, getPoClosure, reopenPo, type PoKey } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDate } from "@/lib/format";
import type { PoClosure } from "@/lib/types";

const REASONS = ["Received", "Cancelled", "Wrong data"] as const;

/** "Closed by Sam on Nov 3 (received)". */
export function closedWords(c: PoClosure): string {
  const when = c.at ? ` on ${formatDate(c.at, { short: true })}` : "";
  return `Closed by ${c.by ?? "someone"}${when}${c.reason ? ` (${c.reason})` : ""}`;
}

/**
 * The close control for one order. `closed` is the state to start from when the caller knows it (the
 * order's page asks); `onChanged` runs after a close or an undo so a list can read itself again.
 * `popover`: anchor the close form as a dropdown under the button (safe inside a fixed-height toolbar).
 */
export function CloseControl({
  order,
  onChanged,
  compact = false,
  popover = false,
}: {
  order: PoKey;
  onChanged?: (c: PoClosure) => void;
  compact?: boolean;
  popover?: boolean;
}) {
  const [state, setState] = useState<PoClosure | null>(null);
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const key = order.handle ?? order.object_id ?? "";
  const wrapRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (compact) return; // a list row asks only when the person opens it
    const c = new AbortController();
    getPoClosure(order, c.signal).then(setState, () => undefined);
    return () => c.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, compact]);

  useEffect(() => {
    if (!open) return;
    inputRef.current?.focus();
    if (!popover) return;
    const onPointerDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, popover]);

  const run = useCallback(
    async (undo: boolean) => {
      setBusy(true);
      setError(null);
      try {
        const got = undo ? await reopenPo(order) : await closePo(order, reason.trim() || null);
        setState(got);
        setOpen(false);
        setReason("");
        onChanged?.(got);
      } catch (e) {
        setError(plainError(e));
      } finally {
        setBusy(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [key, reason, onChanged],
  );

  if (state?.closed) {
    if (popover) {
      return (
        <div className="relative flex items-center gap-2 text-[12.5px]" data-testid="po-closed">
          <span className="max-w-[200px] truncate text-muted" title={closedWords(state)}>
            {closedWords(state)}
          </span>
          {(state.new_since ?? []).length > 0 && (
            <span className="sr-only" data-testid="po-closed-new">
              {(state.new_since ?? []).join("; ")}. It stays closed until you open it again.
            </span>
          )}
          <button
            type="button"
            disabled={busy}
            onClick={() => void run(true)}
            className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-bg px-2 text-[12px] font-medium text-fg transition-colors hover:border-border-strong hover:bg-hover disabled:opacity-50 cursor-pointer"
          >
            {busy && <Loader2 className="size-3 animate-spin" aria-hidden />}
            {busy ? "Reopening" : "Open it again"}
          </button>
          {error && (
            <span role="alert" className="text-[12px] text-late">
              {error}
            </span>
          )}
        </div>
      );
    }
    return (
      <div className="text-[13px] leading-5" data-testid="po-closed">
        <p className="text-fg">{closedWords(state)}</p>
        {(state.new_since ?? []).length > 0 && (
          <p className="mt-0.5 text-late" data-testid="po-closed-new">
            {(state.new_since ?? []).join("; ")}. It stays closed until you open it again.
          </p>
        )}
        <button
          type="button"
          disabled={busy}
          onClick={() => void run(true)}
          className="mt-1 inline-flex items-center gap-1 text-muted underline underline-offset-2 hover:text-fg disabled:opacity-50 cursor-pointer"
        >
          {busy && <Loader2 className="size-3 animate-spin" aria-hidden />}
          {busy ? "Reopening" : "Open it again"}
        </button>
        {error && (
          <p role="alert" className="mt-1 text-late">
            {error}
          </p>
        )}
      </div>
    );
  }

  const formBody = (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5">
        {REASONS.map((r) => (
          <button
            key={r}
            type="button"
            onClick={() => setReason((cur) => (cur === r ? "" : r))}
            aria-pressed={reason === r}
            className={cn(
              "inline-flex h-6 items-center rounded-md border px-2 text-[11.5px] transition-colors cursor-pointer",
              reason === r ? "border-accent/60 bg-accent-soft font-medium text-fg" : "border-border bg-bg text-muted hover:border-border-strong hover:text-fg",
            )}
          >
            {r}
          </button>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label className="sr-only" htmlFor={`why-${key}`}>
          Why
        </label>
        <input
          ref={inputRef}
          id={`why-${key}`}
          list={`why-list-${key}`}
          value={reason}
          maxLength={300}
          onChange={(e) => setReason(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !busy) {
              e.preventDefault();
              void run(false);
            } else if (e.key === "Escape") {
              e.preventDefault();
              setOpen(false);
            }
          }}
          placeholder="Why (optional)"
          className="h-8 min-w-[11rem] flex-1 rounded-md border border-border-strong bg-bg px-2.5 text-[13px] text-fg outline-none transition-colors hover:border-border-strong focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-ring/50 dark:bg-surface"
        />
        <datalist id={`why-list-${key}`}>
          {REASONS.map((r) => (
            <option key={r} value={r} />
          ))}
        </datalist>
        <button
          type="button"
          disabled={busy}
          onClick={() => void run(false)}
          className="inline-flex h-8 items-center gap-1.5 rounded-md bg-accent px-3 text-[12.5px] font-medium text-accent-fg transition-[opacity,transform] hover:brightness-110 active:scale-[0.98] disabled:pointer-events-none disabled:opacity-60 cursor-pointer"
          data-testid="po-close-confirm"
        >
          {busy && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
          {busy ? "Closing" : "Close it"}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => setOpen(false)}
          className="inline-flex h-8 items-center rounded-md px-2 text-[12.5px] text-muted transition-colors hover:bg-hover hover:text-fg disabled:opacity-50 cursor-pointer"
        >
          Cancel
        </button>
      </div>
      <p className="text-[12px] leading-4 text-muted">
        Closes it for everyone here. Nothing changes in Business Central or your other systems. You can open it again.
      </p>
      {error && (
        <p role="alert" className="text-[12px] text-late">
          {error}
        </p>
      )}
    </div>
  );

  return (
    <div ref={wrapRef} className={cn("text-[13px] leading-5", popover && "relative")} data-testid="po-close">
      {!open || popover ? (
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className="inline-flex h-8 items-center rounded-md border border-border bg-bg px-2.5 text-[12.5px] font-medium text-muted transition-[background-color,border-color,color,transform] duration-150 hover:border-border-strong hover:bg-hover hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 active:scale-[0.98] cursor-pointer"
          data-testid="po-close-open"
        >
          Close this order
        </button>
      ) : null}
      {open &&
        (popover ? (
          <div
            role="dialog"
            aria-label="Close this order"
            className="absolute right-0 top-full z-40 mt-1.5 w-[min(360px,calc(100vw-24px))] rounded-lg border border-border-strong bg-raised p-3.5 shadow-pop"
            data-testid="po-close-popover"
          >
            {formBody}
          </div>
        ) : (
          formBody
        ))}
      {!open && error && (
        <p role="alert" className="mt-1 text-late">
          {error}
        </p>
      )}
    </div>
  );
}
