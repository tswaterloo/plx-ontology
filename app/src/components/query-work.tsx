"use client";

import { useState } from "react";
import { plainError } from "@/lib/errors";
import type { FilterEdit, QueryWork } from "@/lib/types";

const link = "text-[12px] text-accent underline-offset-2 hover:underline cursor-pointer disabled:cursor-default disabled:opacity-50";

/** Pure: the edits as the server takes them, in the statement's own filter order. */
export function editList(edits: Record<number, string | null>): FilterEdit[] {
  return Object.keys(edits)
    .map(Number)
    .sort((a, b) => a - b)
    .map((index) => ({ index, text: edits[index] }));
}

/**
 * Show the work of one statement: the views it reads and each of its filters, read from the statement itself.
 * A filter can be changed or taken out; `read` reads the statement again with every change so far (as the reader,
 * nothing saved) and `onReset` goes back to the statement as it was. The filters listed are always the original
 * statement's: a change never renumbers them.
 */
export function ShowWork({ work, read, onReset, testId }: { work: QueryWork; read: (edits: FilterEdit[]) => Promise<void>; onReset: () => void; testId: string }) {
  const [base] = useState(work);
  const [edits, setEdits] = useState<Record<number, string | null>>({});
  const [open, setOpen] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const changed = Object.keys(edits).length > 0;

  const apply = async (next: Record<number, string | null>) => {
    setBusy(true);
    setError(null);
    try {
      if (Object.keys(next).length === 0) onReset();
      else await read(editList(next));
      setEdits(next);
      setOpen(null);
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const set = (index: number, text: string | null) => {
    const original = base.filters.find((f) => f.index === index)?.text;
    const next = { ...edits };
    if (text !== null && text.trim() === original) delete next[index];
    else next[index] = text === null ? null : text.trim();
    void apply(next);
  };

  return (
    <div className="space-y-1 text-[12px]" data-testid={testId}>
      {base.views.length > 0 && (
        <p className="text-muted">
          Reads {base.views.join(", ")}
        </p>
      )}
      {base.filters.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-muted">Filters</span>
          {base.filters.map((f) => {
            const now = f.index in edits ? edits[f.index] : f.text;
            if (open === f.index) {
              return (
                <form
                  key={f.index}
                  className="flex items-center gap-1"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (draft.trim()) set(f.index, draft);
                  }}
                >
                  <input
                    aria-label={`Filter ${f.index + 1}`}
                    className="h-7 min-w-[16rem] rounded-md border border-border-strong bg-bg px-2 font-mono text-[12px] text-fg dark:bg-surface"
                    value={draft}
                    autoFocus
                    disabled={busy}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => e.key === "Escape" && setOpen(null)}
                  />
                  <button type="submit" className={link} disabled={busy || !draft.trim()}>Apply</button>
                  <button type="button" className={link} disabled={busy} onClick={() => set(f.index, null)}>Take out</button>
                  <button type="button" className={link} disabled={busy} onClick={() => setOpen(null)}>Cancel</button>
                </form>
              );
            }
            return (
              <button
                key={f.index}
                type="button"
                title="Change or take out this filter"
                disabled={busy}
                className={`rounded border px-1.5 py-0.5 text-left font-mono cursor-pointer ${f.index in edits ? "border-accent" : "border-border"} ${now === null ? "text-muted line-through" : ""}`}
                onClick={() => {
                  setDraft(now ?? f.text);
                  setOpen(f.index);
                }}
              >
                {now ?? f.text}
              </button>
            );
          })}
          {base.more_filters ? (
            <span className="text-muted">
              {base.more_filters} further {base.more_filters === 1 ? "filter is" : "filters are"} in the whole query (Show query)
            </span>
          ) : null}
          {changed && (
            <button type="button" className={link} disabled={busy} onClick={() => void apply({})}>
              Reset filters
            </button>
          )}
        </div>
      )}
      {busy && <p className="text-muted">Reading again</p>}
      {error && <p className="text-late" role="alert">{error}</p>}
    </div>
  );
}

/** The statement itself, put on screen only when asked for (Insights reads in words first). */
export function QueryToggle({ sql, testId }: { sql: string; testId: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="text-[12px]" data-testid={testId}>
      <button type="button" className={link} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {open ? "Hide query" : "Show query"}
      </button>
      {open && <pre className="mt-1 overflow-x-auto whitespace-pre-wrap rounded-md border border-border bg-surface p-2.5 font-mono text-fg">{sql}</pre>}
    </div>
  );
}
