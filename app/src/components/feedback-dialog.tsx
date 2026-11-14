"use client";

import { MessageSquareText } from "lucide-react";
import { createContext, lazy, Suspense, useContext, useEffect, useMemo, useState } from "react";
import { useShortcut } from "@/lib/platform";
import { useReturnFocus } from "@/lib/return-focus";

/** The sheet itself loads when it is first opened, so it stays out of every page's first load. */
const FeedbackSheet = lazy(() => import("./feedback-sheet"));

/** The record a page shows, read from its path (the same on every OS: app paths only). */
export function recordOfPage(path: string, params?: URLSearchParams | null): { object_type: string; object_id: string; label: string } | null {
  const seg = path.split("/").filter(Boolean).map((s) => {
    try {
      return decodeURIComponent(s);
    } catch {
      return s;
    }
  });
  const named: Record<string, string> = { po: "PurchaseOrder", ao: "AssemblyOrder", so: "SalesOrder", customer: "Customer", supplier: "Supplier" };
  if (seg[0] === "object" && seg.length >= 3) return { object_type: seg[1], object_id: seg.slice(2).join("/"), label: `${seg[1]} ${seg.slice(2).join("/")}` };
  if (named[seg[0]] && seg[1]) return { object_type: named[seg[0]], object_id: seg[1], label: `${named[seg[0]].replace(/([a-z])([A-Z])/g, "$1 $2")} ${seg[1]}` };
  const handle = seg[0] === "source" ? params?.get("handle") : null;
  if (handle) return { object_type: "Source", object_id: handle.slice(0, 200), label: "This source" };
  return null;
}

const Ctx = createContext<{ open: () => void }>({ open: () => undefined });

export function useFeedbackDialog() {
  return useContext(Ctx);
}

/** True when the key press is typed into a field (the shortcut leaves typing alone). */
function typing(e: KeyboardEvent): boolean {
  const t = e.target as HTMLElement | null;
  return !!t && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName));
}

/** The shortcut: Ctrl+Shift+F (⌘⇧F on a Mac), or "f" when nothing is being typed. */
export function isFeedbackShortcut(e: KeyboardEvent): boolean {
  const key = e.key.toLowerCase();
  if ((e.metaKey || e.ctrlKey) && e.shiftKey && !e.altKey && key === "f") return true;
  return key === "f" && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey && !typing(e);
}

/** The global Feedback entry: the sidebar item and the shortcut open one sheet, anywhere. */
export function FeedbackProvider({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.defaultPrevented || !isFeedbackShortcut(e)) return;
      if (document.querySelector("[role=dialog]")) return; // another sheet or the palette is open
      e.preventDefault();
      setOpen(true);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const value = useMemo(() => ({ open: () => setOpen(true) }), []);
  useReturnFocus(open);
  return (
    <Ctx.Provider value={value}>
      {children}
      {open && (
        <Suspense>
          <FeedbackSheet onClose={() => setOpen(false)} />
        </Suspense>
      )}
    </Ctx.Provider>
  );
}

/** The sidebar's Feedback entry, with its shortcut. */
export function FeedbackButton() {
  const { open } = useFeedbackDialog();
  const { mod } = useShortcut();
  return (
    <button
      type="button"
      onClick={open}
      title={`Give feedback (F, or ${mod === "⌘" ? "⌘⇧F" : "Ctrl+Shift+F"})`}
      className="flex h-8 w-full items-center gap-2.5 rounded-md px-2 text-left text-[13px] text-fg/90 hover:bg-hover max-xl:justify-center max-xl:px-0 cursor-pointer"
      data-testid="feedback-open"
    >
      <span className="flex size-5 shrink-0 items-center justify-center" aria-hidden>
        <MessageSquareText className="size-[15px] text-muted" strokeWidth={2} />
      </span>
      <span className="flex-1 truncate max-xl:sr-only">Give feedback</span>
      <kbd className="font-sans text-[11px] leading-4 text-faint max-xl:hidden" aria-hidden>
        F
      </kbd>
    </button>
  );
}
