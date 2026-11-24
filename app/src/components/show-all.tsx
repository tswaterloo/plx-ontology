"use client";

import { Fragment, useState, type ReactNode } from "react";

/**
 * The first `first` items, then a "Show all N" control that opens the rest (and "Show fewer"
 * to close them): a list is never cut without saying so and without a way to the rest.
 * `as`: "li" puts the control in its own list item (inside a ul/ol), else an inline button.
 */
export function ShowAll<T>({
  items,
  first,
  children,
  as,
  noun = "",
}: {
  items: readonly T[];
  first: number;
  children: (item: T, i: number) => ReactNode;
  as?: "li";
  noun?: string;
}) {
  const [all, setAll] = useState(false);
  const shown = all ? items : items.slice(0, first);
  const more = items.length - first;
  const control = more > 0 && (
    <button
      type="button"
      className="cursor-pointer text-[12px] text-accent underline-offset-2 hover:underline"
      aria-expanded={all}
      onClick={() => setAll((a) => !a)}
    >
      {all ? "Show fewer" : `Show all ${items.length}${noun ? ` ${noun}` : ""}`}
    </button>
  );
  return (
    <>
      {shown.map((item, i) => (
        <Fragment key={i}>{children(item, i)}</Fragment>
      ))}
      {control &&
        (as === "li" ? <li className="list-none">{control}</li> : control)}
    </>
  );
}
