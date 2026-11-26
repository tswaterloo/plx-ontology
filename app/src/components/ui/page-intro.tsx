import { cn } from "@/lib/cn";

/**
 * What each page is for, in one plain sentence under its title (the owner, 11-17: "the pages
 * don't even make sense"). Keyed by the page's path; a sub-page (a tab) has its own line.
 * Memory and Templates say it in their own opening paragraph; a record page is named by the
 * record it shows.
 */
export const PAGE_INTROS = {
  "/": "",
  "/ask": "Ask about anything you can open, or ask for a chart, a document or a quote.",
  "/?show=alerts": "Alerts from the rules you set and the records you follow.",
  "/?show=late": "Every purchase order past its due date, with the date the supplier gave, if any.",
  "/assist": "",
  "/meetings": "",
  "/team": "What people are working on and what waits on whom.",
  "/insights": "",
  "/artifacts": "",
  "/records": "",
  "/orders": "Purchase orders, late first, and sales orders.",
  "/customers": "Every customer. The ones you opened lately and saved come first.",
  "/suppliers": "Every supplier. The ones you opened lately and saved come first.",
  "/products": "Every product. The ones you opened lately and saved come first.",
  "/more": "Invoices, shipments, receipts, batches and every other list.",
  "/work-log": "What you did here, newest first, and the time it saved.",
  "/activity": "Each message and task you started here, and whether it went out.",
  "/help": "How the Workbench helps in your role.",
  "/ops": "",
  "/data-health": "Is the data complete, fresh and correct, system by system, with the problems first.",
  "/feedback": "Feedback you sent, and what became of it.",
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
