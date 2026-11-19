import { cn } from "@/lib/cn";

/**
 * What each page is for, in one plain sentence under its title (the owner, 11-17: "the pages
 * don't even make sense"). Keyed by the page's path; a sub-page (a tab) has its own line.
 * Memory and Templates say it in their own opening paragraph; a record page is named by the
 * record it shows.
 */
export const PAGE_INTROS = {
  "/": "What needs you today: late orders to chase, replies to send and alerts to decide.",
  "/ask": "Ask about any order, supplier, customer or lot. Answers come only from the records and mail you may open.",
  "/?show=alerts": "Alerts from the rules you set and the records you follow.",
  "/?show=late": "Every purchase order past its due date, with the date the supplier gave, if any.",
  "/autopilot": "What is prepared for you to answer, and what you want prepared. Each person turns their own on.",
  "/autopilot-log": "What the autopilot did and what each person did with it, with undo.",
  "/meetings": "Your upcoming meetings, the weekly review and the follow-ups you owe from recorded meetings.",
  "/team": "What people are working on and what waits on whom, as far as your access allows.",
  "/records": "Find any customer, order, supplier or product by its name or number.",
  "/work-log": "Everything you did in the Workbench, newest first, and the time it saved.",
  "/activity": "Each message and task you started here, and whether it went out.",
  "/help": "How the Workbench helps in your role, and what to try in your first week.",
  "/ops": "What is broken in the Workbench now, what to do about it, and what is paused on purpose.",
  "/trust": "Whether the data keeps up with the systems it reads, and how accurate answers measured.",
  "/feedback": "Feedback sent with Give feedback, and what became of it.",
} as const satisfies Record<string, string>;

/** The page's one sentence. `path` must be a key of PAGE_INTROS (a typo is a type error). */
export function PageIntro({ path, className }: { path: keyof typeof PAGE_INTROS; className?: string }) {
  const line = PAGE_INTROS[path];
  if (!line) return null;
  return (
    <p className={cn("max-w-[75ch] px-4 pt-3 text-[13px] leading-5 text-muted lg:px-5", className)} data-testid="page-intro">
      {line}
    </p>
  );
}
