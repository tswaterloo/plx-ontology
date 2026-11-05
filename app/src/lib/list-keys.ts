"use client";

import { useEffect, type RefObject } from "react";

/**
 * j / k move through a list's rows from the keyboard (↓ / ↑ too once a row has focus), Enter
 * opens the focused row's link (the browser's own), Esc leaves the list. Ignored while typing
 * in a field and with ⌘, Ctrl or Alt held. The row keeps a visible ring; nothing animates.
 */
export function useListKeys(container: RefObject<HTMLElement | null>, selector = "a[data-row-link]") {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target;
      if (t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      const root = container.current;
      if (!root) return;
      const rows = [...root.querySelectorAll<HTMLElement>(selector)].filter((el) => el.offsetParent !== null);
      if (rows.length === 0) return;
      const at = rows.indexOf(document.activeElement as HTMLElement);
      const inList = at !== -1;
      let next: number | null = null;
      if (e.key === "j" || (inList && e.key === "ArrowDown")) next = inList ? Math.min(rows.length - 1, at + 1) : 0;
      else if (e.key === "k" || (inList && e.key === "ArrowUp")) next = inList ? Math.max(0, at - 1) : 0;
      else if (e.key === "Escape" && inList) {
        (document.activeElement as HTMLElement).blur();
        return;
      }
      if (next === null) return;
      e.preventDefault();
      rows[next].focus({ preventScroll: true });
      rows[next].scrollIntoView({ block: "nearest" });
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [container, selector]);
}
