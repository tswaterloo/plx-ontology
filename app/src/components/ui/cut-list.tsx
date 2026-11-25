"use client";

import { useState, type ReactNode } from "react";
import type { Listed } from "@/lib/value";

/**
 * A list the server shortened (contract C1: `{items, total, next}`): its rows, then "12 of 81
 * shown" and one control that reads the rest through `next` (`onMore`), appended in place. A
 * list the client holds whole and shows short is `ShowAll`'s; this one never pretends the rows
 * it has are all there are.
 */
export function CutList<T>({
  list,
  noun = "items",
  onMore,
  children,
}: {
  list: Listed<T>;
  noun?: string;
  onMore: (next: string | number) => Promise<Listed<T>>;
  children: (item: T, index: number) => ReactNode;
}) {
  const [got, setGot] = useState<Listed<T>>(list);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const shown = got.items.length;
  const more = () => {
    if (got.next === null || busy) return;
    setBusy(true);
    setError(null);
    onMore(got.next)
      .then((page) => setGot((g) => ({ items: [...g.items, ...page.items], total: page.total, next: page.next })))
      .catch(() => setError("The rest could not be read; try again."))
      .finally(() => setBusy(false));
  };
  return (
    <>
      {got.items.map((item, i) => children(item, i))}
      {got.total > shown ? (
        <p className="text-sm text-muted" data-testid="cut-list-note">
          {shown.toLocaleString("en-US")} of {got.total.toLocaleString("en-US")} {noun} shown.{" "}
          {got.next !== null ? (
            <button type="button" className="underline hover:text-fg cursor-pointer" onClick={more} disabled={busy}>
              {busy ? "Reading" : `Show the other ${(got.total - shown).toLocaleString("en-US")}`}
            </button>
          ) : null}
          {error ? <span className="ml-2 text-late">{error}</span> : null}
        </p>
      ) : null}
    </>
  );
}
