"use client";

import { createContext, lazy, Suspense, useContext, useEffect, useMemo, useState } from "react";
import { whenIdle } from "@/lib/idle";
import { useReturnFocus } from "@/lib/return-focus";

/**
 * The command bar: one field that finds any record by number or name, goes anywhere in the
 * app and asks a question, all from the keyboard. ⌘K or Ctrl K opens it from anywhere, "/"
 * when no field has focus. ↑ ↓ move, ↵ opens, Esc closes.
 */

const PaletteCtx = createContext<{ open: () => void }>({ open: () => undefined });

/** Opens the command bar from anywhere under the provider (the top bar's search, a hint). */
export function useCommandPalette() {
  return useContext(PaletteCtx);
}

export function CommandPaletteProvider({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const typing = e.target instanceof HTMLElement && (e.target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName));
      if (((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") || (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey)) {
        e.preventDefault();
        setOpen((v) => !v || e.key === "/");
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const value = useMemo(() => ({ open: () => setOpen(true) }), []);
  // the dialog loads when the bar first opens (or when the browser is idle), never in the first load
  const [used, setUsed] = useState(false);
  if (open && !used) setUsed(true);
  useEffect(() => whenIdle(loadPalette), []);
  useReturnFocus(open);
  return (
    <PaletteCtx.Provider value={value}>
      {children}
      {used && (
        <Suspense fallback={null}>
          <CommandPalette open={open} onOpenChange={setOpen} />
        </Suspense>
      )}
    </PaletteCtx.Provider>
  );
}

const loadPalette = () => import("./command-palette-dialog");
const CommandPalette = lazy(loadPalette);
