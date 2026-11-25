"use client";

// Close a purchase order inside the Workbench: for everyone, with an optional reason, and undoable.
// Nothing is written to Business Central, the Portal or Sage; the words say so.

import { useCallback, useEffect, useState } from "react";
import { closePo, getPoClosure, reopenPo, type PoKey } from "@/lib/api";
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
 */
export function CloseControl({ order, onChanged, compact = false }: { order: PoKey; onChanged?: (c: PoClosure) => void; compact?: boolean }) {
  const [state, setState] = useState<PoClosure | null>(null);
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const key = order.handle ?? order.object_id ?? "";

  useEffect(() => {
    if (compact) return; // a list row asks only when the person opens it
    const c = new AbortController();
    getPoClosure(order, c.signal).then(setState, () => undefined);
    return () => c.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, compact]);

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

  if (state?.closed)
    return (
      <div className="text-[13px] leading-5" data-testid="po-closed">
        <p className="text-fg">{closedWords(state)}</p>
        {(state.new_since ?? []).length > 0 && (
          <p className="mt-0.5 text-late" data-testid="po-closed-new">
            {(state.new_since ?? []).join("; ")}. It stays closed until you open it again.
          </p>
        )}
        <button type="button" disabled={busy} onClick={() => void run(true)} className="mt-1 text-muted underline hover:text-fg cursor-pointer">
          Open it again
        </button>
        {error && <p className="mt-1 text-late">{error}</p>}
      </div>
    );

  return (
    <div className="text-[13px] leading-5" data-testid="po-close">
      {!open ? (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="inline-flex h-8 items-center rounded-md border border-border bg-bg px-3 text-[12.5px] font-medium text-muted hover:border-border-strong hover:bg-hover hover:text-fg cursor-pointer"
          data-testid="po-close-open"
        >
          Close this order
        </button>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor={`why-${key}`}>
            Why
          </label>
          <input
            id={`why-${key}`}
            list={`why-list-${key}`}
            value={reason}
            maxLength={300}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Why (optional)"
            className="h-8 w-44 rounded-lg border border-border bg-bg px-2.5 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
          />
          <datalist id={`why-list-${key}`}>
            {REASONS.map((r) => (
              <option key={r} value={r} />
            ))}
          </datalist>
          <button type="button" disabled={busy} onClick={() => void run(false)} className="h-8 rounded-lg bg-accent px-3 text-[13px] font-medium text-white cursor-pointer disabled:opacity-60" data-testid="po-close-confirm">
            Close it
          </button>
          <button type="button" onClick={() => setOpen(false)} className="text-muted hover:text-fg cursor-pointer">
            Cancel
          </button>
          <p className="w-full text-[12px] text-muted">Closes it for everyone here. Nothing changes in Business Central or your other systems. You can open it again.</p>
        </div>
      )}
      {error && <p className="mt-1 text-late">{error}</p>}
    </div>
  );
}
