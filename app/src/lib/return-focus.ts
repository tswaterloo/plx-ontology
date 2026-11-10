import { useEffect, useRef } from "react";

const outside = (el: Element | null): el is HTMLElement => el instanceof HTMLElement && el !== document.body && !el.closest("[role=dialog]");

/**
 * A dialog opened from anywhere (a shortcut, a button that is not its Radix trigger) gives
 * focus back where it was when it closes; without this, focus falls to the page body and a
 * keyboard reader starts again from the top. Not when the dialog took the reader to another page.
 */
export function useReturnFocus(open: boolean) {
  // the last element focused outside any dialog: a dialog's autofocus moves focus before
  // this component's effect runs, so "where focus was" is kept as it happens
  const last = useRef<HTMLElement | null>(null);
  const from = useRef<{ el: HTMLElement; at: string } | null>(null);
  useEffect(() => {
    if (outside(document.activeElement)) last.current = document.activeElement;
    const onFocus = (e: FocusEvent) => {
      if (outside(e.target as Element)) last.current = e.target as HTMLElement;
    };
    // focus left for nowhere (a click on the page): there is nothing to give it back to
    const onBlur = (e: FocusEvent) => {
      if (!e.relatedTarget) last.current = null;
    };
    document.addEventListener("focusin", onFocus);
    document.addEventListener("focusout", onBlur);
    return () => {
      document.removeEventListener("focusin", onFocus);
      document.removeEventListener("focusout", onBlur);
    };
  }, []);
  useEffect(() => {
    if (open) {
      from.current = last.current ? { el: last.current, at: location.href } : null;
      return;
    }
    const was = from.current;
    from.current = null;
    if (!was || was.at !== location.href) return;
    const el = was.el;
    // after the dialog's own close handling, and only if nothing else took focus
    const t = setTimeout(() => {
      const now = document.activeElement;
      if (el.isConnected && (!now || now === document.body)) el.focus();
    }, 0);
    return () => clearTimeout(t);
  }, [open]);
}
