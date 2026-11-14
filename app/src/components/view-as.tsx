"use client";

import { Eye } from "lucide-react";
import { useSyncExternalStore } from "react";
import { markViewAs } from "@/lib/api";
import { onViewingAs, setViewingAs, viewingAs, type ViewingAs } from "@/lib/view-as";

export function useViewingAs(): ViewingAs | null {
  return useSyncExternalStore(onViewingAs, viewingAs, () => null);
}

/**
 * Words for the reader the screens are about: "you"/"your" for the owner's own view, the
 * person's name while viewing as them ("Nothing is waiting on Vince", never "on you").
 */
export function useReaderWords(): { you: string; your: string; You: string; Your: string } {
  const v = useViewingAs();
  if (!v) return { you: "you", your: "your", You: "You", Your: "Your" };
  const own = /s$/i.test(v.name) ? `${v.name}'` : `${v.name}'s`;
  return { you: v.name, your: own, You: v.name, Your: own };
}

/** Start or stop viewing (each audited), then reload so every screen reads again as the new reader. */
export async function switchTo(v: ViewingAs | null) {
  const was = viewingAs();
  // the marks go out with no view-as header: stop after leaving the view, start before entering it
  setViewingAs(null);
  if (was && was.key !== v?.key) await markViewAs("stop", was.key);
  if (v) {
    await markViewAs("start", v.key);
    setViewingAs(v);
  }
  window.location.assign("/");
}

/**
 * The line across the top of every page while the owner views as someone: whose screens
 * these are, that nothing can be sent, approved or logged as them, and the way back.
 */
export function ViewAsBar() {
  const v = useViewingAs();
  if (!v) return null;
  return (
    <div
      role="status"
      className="flex items-center gap-2 border-b border-accent bg-accent-soft px-5 py-1 text-[12px] leading-4 text-fg"
      data-testid="view-as-bar"
    >
      <Eye className="size-3.5 shrink-0 text-accent" strokeWidth={2} aria-hidden />
      <span className="min-w-0 flex-1 truncate">
        <span className="font-semibold">Viewing as {v.name}</span>
        <span className="text-muted"> · My work, alerts and drafts as {v.name} sees them. Read only: nothing is sent, approved or logged as {v.name}, and each view is audited.</span>
      </span>
      <button type="button" onClick={() => switchTo(null)} className="shrink-0 font-medium text-accent hover:underline cursor-pointer" data-testid="view-as-stop">
        Stop viewing
      </button>
    </div>
  );
}
