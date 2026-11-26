import { Activity, ChartColumn, BookMarked, BookOpen, Bot, BellRing, Briefcase, Calculator, ClipboardCheck, Building2, ClipboardList, Truck, Database, FileText, FlaskConical, Gauge, Layers, ListChecks, Mail, Package, CalendarClock, Clock3, History, Inbox, MessageCircle, MessageSquareText, Users, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/cn";

/** Not on record: a closed ring with a bar (static; a dashed ring read as a loading spinner). */
export function OpenRing({ size = 14, tone = "risk", className }: { size?: number; tone?: "risk" | "neutral"; className?: string }) {
  return (
    <svg viewBox="0 0 14 14" width={size} height={size} className={cn("shrink-0", className)} aria-hidden>
      {/* a closed ring with a bar: "not there", never a dashed ring that reads as loading */}
      <circle cx="7" cy="7" r="5.25" fill="none" stroke={tone === "risk" ? "var(--dot-risk)" : "var(--dot-neutral)"} strokeWidth="1.5" />
      <path d="M4.6 7h4.8" stroke={tone === "risk" ? "var(--dot-risk)" : "var(--dot-neutral)"} strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

/** Confirmed: the ring closed with a tick. */
export function DoneRing({ size = 14, className }: { size?: number; className?: string }) {
  return (
    <svg viewBox="0 0 14 14" width={size} height={size} className={cn("shrink-0", className)} aria-hidden>
      <circle cx="7" cy="7" r="6" fill="var(--dot-ok)" />
      <path d="M4.4 7.2l1.8 1.8 3.4-3.6" fill="none" stroke="#fff" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/**
 * A place in the app: a restrained monochrome line icon (Linear/Claude style), with no tile
 * behind it. Active state elevates to text-fg via className; inactive sits quietly in text-muted.
 */
export const PLACES: Record<string, { icon: LucideIcon; hue: string }> = {
  "/": { icon: Inbox, hue: "text-muted" },
  "/late": { icon: Clock3, hue: "text-muted" },
  "/watching": { icon: BellRing, hue: "text-muted" },
  "/ask": { icon: MessageCircle, hue: "text-muted" },
  "/insights": { icon: ChartColumn, hue: "text-muted" },
  "/artifacts": { icon: Layers, hue: "text-muted" },
  "/assist": { icon: Bot, hue: "text-muted" },
  "/autopilot": { icon: Bot, hue: "text-muted" },
  "/meetings": { icon: CalendarClock, hue: "text-muted" },
  "/team": { icon: Users, hue: "text-muted" },
  "/activity": { icon: History, hue: "text-muted" },
  "/work-log": { icon: History, hue: "text-muted" },
  "/templates": { icon: FileText, hue: "text-muted" },
  "/ops": { icon: Activity, hue: "text-muted" },
  "/data-health": { icon: Gauge, hue: "text-muted" },
  "/feedback": { icon: MessageSquareText, hue: "text-muted" },
  "/memory": { icon: BookMarked, hue: "text-muted" },
  "/records": { icon: Database, hue: "text-muted" },
  "/orders": { icon: ClipboardList, hue: "text-muted" },
  "/customers": { icon: Building2, hue: "text-muted" },
  "/suppliers": { icon: Truck, hue: "text-muted" },
  "/products": { icon: Package, hue: "text-muted" },
  "/more": { icon: Database, hue: "text-muted" },
  "/work": { icon: Briefcase, hue: "text-muted" },
  "/admin": { icon: Activity, hue: "text-muted" },
};

export function PlaceIcon({ href, className }: { href: string; className?: string }) {
  const p = PLACES[href];
  if (!p) return null;
  const Icon = p.icon;
  return <Icon className={cn("size-4 shrink-0 text-muted", className)} strokeWidth={1.75} aria-hidden />;
}

/**
 * A source system: a small line icon in the system's hue, then its name in the readable ink of
 * that hue, medium weight. The same system is the same colour everywhere; no tile, badge or fill.
 */
const SYSTEM_CODE: Record<string, [string, string, LucideIcon]> = {
  BC: ["BC", "bc", BookOpen],
  Portal: ["Portal", "portal", Package],
  Email: ["Mail", "mail", Mail],
  Teams: ["Teams", "teams", MessageCircle],
  File: ["File", "file", FileText],
  MC: ["MC", "mc", ListChecks],
  Sage: ["Sage", "sage", Calculator],
  FileMaker: ["FM", "fm", Database],
};

/** ``bare``: the system's icon only, for a line that names the system in full beside it. */
export function SystemCode({ system, size = 18, className, bare = false }: { system: string | null | undefined; size?: number; className?: string; bare?: boolean }) {
  const [label, key, Icon] = SYSTEM_CODE[system ?? ""] ?? [(system ?? "?").slice(0, 6), "file", FileText];
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-[5px] leading-none font-medium",
        size <= 16 ? "h-4 text-[11px]" : size <= 20 ? "h-5 text-[11px]" : "h-8 text-[12px]",
        className,
      )}
      style={{ color: `var(--sys-${key}-ink)` }}
      data-system={system ?? undefined}
      aria-hidden
    >
      <Icon className="size-[13px] shrink-0" strokeWidth={1.75} />
      {!bare && label}
    </span>
  );
}

/** Late days as text: amber up to 30 days, red beyond (both hold AA on every pane). */
export const lateInk = (days: number | null | undefined) => (typeof days === "number" && days > 30 ? "text-late" : "text-risk");

/** A late figure ("134d", "134 days late"): Geist Mono in the severity colour, no icon. */
export const lateFigure = (days: number | null | undefined) => cn("font-mark", lateInk(days));

/** A late row's 3 px left edge in the severity colour (amber up to 30 days, red beyond). */
export const lateEdge = (days: number | null | undefined) => cn("border-l-[3px]", typeof days === "number" && days > 30 ? "border-late" : "border-risk");

/** "134 days late" or "134d" -> 134. */
export function daysIn(label: string | null | undefined): number | null {
  const m = /(\d+)\s*(?:d\b|days?)/.exec(label ?? "");
  return m ? Number(m[1]) : null;
}

/**
 * A status as a shape, for places with no figure to draw: late is a half-swept wedge,
 * at risk a dashed open ring, confirmed a ticked ring, a data problem a dotted ring, new a
 * filled ring, neutral a plain ring. The words beside it stay in ink.
 */
export function StatusGlyph({ status, size = 13, className }: { status: "late" | "risk" | "ok" | "suspect" | "new" | "neutral"; size?: number; className?: string }) {
  switch (status) {
    case "late":
      return null; // lateness is said by its coloured figure, not a shape
    case "risk":
      return <OpenRing size={size} className={className} />;
    case "ok":
      return <DoneRing size={size} className={className} />;
    case "suspect":
      return (
        <svg viewBox="0 0 14 14" width={size} height={size} className={cn("shrink-0", className)} aria-hidden>
          <circle cx="7" cy="7" r="5.6" fill="none" stroke="var(--suspect)" strokeWidth="1.6" strokeDasharray="0.1 2.6" strokeLinecap="round" />
        </svg>
      );
    case "new":
      return (
        <svg viewBox="0 0 14 14" width={size} height={size} className={cn("shrink-0", className)} aria-hidden>
          <circle cx="7" cy="7" r="5.6" fill="none" stroke="var(--accent)" strokeWidth="1.5" />
          <circle cx="7" cy="7" r="2.6" fill="var(--accent)" />
        </svg>
      );
    default:
      return (
        <svg viewBox="0 0 14 14" width={size} height={size} className={cn("shrink-0", className)} aria-hidden>
          <circle cx="7" cy="7" r="5.6" fill="none" stroke="var(--dot-neutral)" strokeWidth="1.5" />
        </svg>
      );
  }
}
