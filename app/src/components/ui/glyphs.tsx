import { Bot, BellRing, CalendarClock, Clock3, History, Inbox, MessageCircle, MessageSquareText, Users, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/cn";

/**
 * How late, drawn: a grey ring with a wedge that fills clockwise with days past the
 * needed-by date (full at 180 days), like a clock hand sweeping. The number beside it says
 * the days; the wedge lets a column of them be compared at a glance.
 */
export function AgeRing({ days, size = 14, className }: { days: number; size?: number; className?: string }) {
  const f = Math.max(0.06, Math.min(1, days / 180));
  const r = 4.1;
  const a = f * 2 * Math.PI;
  const x = 7 + r * Math.sin(a);
  const y = 7 - r * Math.cos(a);
  const wedge = f >= 0.999 ? `M7 7m-${r} 0a${r} ${r} 0 1 0 ${2 * r} 0a${r} ${r} 0 1 0 -${2 * r} 0` : `M7 7V${7 - r}A${r} ${r} 0 ${f > 0.5 ? 1 : 0} 1 ${x.toFixed(2)} ${y.toFixed(2)}Z`;
  return (
    <svg viewBox="0 0 14 14" width={size} height={size} className={cn("shrink-0", className)} aria-hidden>
      <circle cx="7" cy="7" r="6.1" fill="none" stroke="var(--border-strong)" strokeWidth="1.3" />
      <path d={wedge} fill="var(--dot-late)" />
    </svg>
  );
}

/** No date on record: the ring left open, dashed. */
export function OpenRing({ size = 14, tone = "risk", className }: { size?: number; tone?: "risk" | "neutral"; className?: string }) {
  return (
    <svg viewBox="0 0 14 14" width={size} height={size} className={cn("shrink-0", className)} aria-hidden>
      <circle cx="7" cy="7" r="5.25" fill="none" stroke={tone === "risk" ? "var(--dot-risk)" : "var(--dot-neutral)"} strokeWidth="1.5" strokeDasharray="2.2 2.2" />
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

/** A place in the app: a white glyph on its own colour tile, as the app icon is drawn. */
export const PLACES: Record<string, { icon: LucideIcon; tile: string }> = {
  "/": { icon: Inbox, tile: "bg-[linear-gradient(180deg,#4f86ee,#2f6fe4)]" },
  "/late": { icon: Clock3, tile: "bg-[linear-gradient(180deg,#ec7058,#df4a33)]" },
  "/watching": { icon: BellRing, tile: "bg-[linear-gradient(180deg,#f0ae3f,#e3951a)]" },
  "/ask": { icon: MessageCircle, tile: "bg-[linear-gradient(180deg,#2fb0a2,#13917f)]" },
  "/autopilot": { icon: Bot, tile: "bg-[linear-gradient(180deg,#7486e0,#5465c9)]" },
  "/meetings": { icon: CalendarClock, tile: "bg-[linear-gradient(180deg,#5aa8e8,#3a8bd6)]" },
  "/team": { icon: Users, tile: "bg-[linear-gradient(180deg,#6cc070,#43a047)]" },
  "/activity": { icon: History, tile: "bg-[linear-gradient(180deg,#90939b,#72757e)]" },
  "/feedback": { icon: MessageSquareText, tile: "bg-[linear-gradient(180deg,#9b7be6,#7d5bd6)]" },
};

/** 20 px: drawn with classes only, since the sidebar is drawn on the server. */
export function PlaceTile({ href, className }: { href: string; className?: string }) {
  const p = PLACES[href];
  if (!p) return null;
  const Icon = p.icon;
  return (
    <span className={cn("flex size-5 shrink-0 items-center justify-center rounded-[5.5px] text-white shadow-[inset_0_-1px_0_rgb(0_0_0/0.12)]", p.tile, className)} aria-hidden>
      <Icon className="size-[13px]" strokeWidth={2.3} />
    </span>
  );
}

/** A source system as a small lettered tile: the same system is the same colour everywhere. */
const SYSTEM_TILE: Record<string, [string, string]> = {
  BC: ["BC", "#1f8a70"],
  Portal: ["P", "#2f6fe4"],
  Email: ["M", "#2a9bd8"],
  Teams: ["T", "#5b5fc7"],
  File: ["F", "#7a7d86"],
  MC: ["MC", "#c2572b"],
  Sage: ["S", "#3d8b2f"],
  FileMaker: ["FM", "#8a5cc2"],
};

export function SystemTile({ system, size = 18, className }: { system: string | null | undefined; size?: number; className?: string }) {
  const [label, colour] = SYSTEM_TILE[system ?? ""] ?? [(system ?? "?").slice(0, 2), "#7a7d86"];
  return (
    <span
      className={cn("flex shrink-0 items-center justify-center font-semibold text-white", className)}
      style={{ width: size, height: size, borderRadius: Math.round(size * 0.28), background: colour, fontSize: label.length > 1 ? size * 0.42 : size * 0.52, letterSpacing: "-0.02em" }}
      aria-hidden
    >
      {label}
    </span>
  );
}

/**
 * A status as a shape, for places with no figure to draw: late is a half-swept wedge,
 * at risk a dashed open ring, confirmed a ticked ring, a data problem a dotted ring, new a
 * filled ring, neutral a plain ring. The words beside it stay in ink.
 */
export function StatusGlyph({ status, size = 13, className }: { status: "late" | "risk" | "ok" | "suspect" | "new" | "neutral"; size?: number; className?: string }) {
  switch (status) {
    case "late":
      return <AgeRing days={90} size={size} className={className} />;
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
