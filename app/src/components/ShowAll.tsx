"use client";

import { useState, type ReactNode } from "react";

/**
 * A list that shows its first `limit` rows and says so: "20 of 143 shown", with a button that shows
 * all of them (guardrail 1: no silent limit). `say` is the reason the list starts short.
 */
export function ShowAll<T>({
  items,
  limit,
  say = "",
  noun = "items",
  children,
}: {
  items: readonly T[];
  limit: number;
  say?: string;
  noun?: string;
  children: (item: T, index: number) => ReactNode;
}) {
  const [all, setAll] = useState(false);
  const total = items.length;
  const cut = !all && total > limit;
  const shown = cut ? items.slice(0, limit) : items;
  return (
    <>
      {shown.map((item, i) => children(item, i))}
      {cut ? (
        <p className="text-sm text-muted">
          {limit.toLocaleString("en-US")} of {total.toLocaleString("en-US")} {noun} shown{say ? ` (${say})` : ""}.{" "}
          <button type="button" className="underline hover:text-fg cursor-pointer" onClick={() => setAll(true)}>
            Show all {total.toLocaleString("en-US")}
          </button>
        </p>
      ) : null}
    </>
  );
}
