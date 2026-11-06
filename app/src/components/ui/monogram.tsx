import { FileText } from "lucide-react";
import { cn } from "@/lib/cn";

// A counterpart's tile: its initials on a soft tint of its own hue, so the same supplier
// reads the same colour in every list. Eight hues, picked by a stable hash of the name.
const HUES = [212, 150, 268, 26, 182, 330, 238, 82];

function hueOf(name: string): number {
  let h = 0;
  for (const c of name.toLowerCase()) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return HUES[h % HUES.length];
}

export function initials(name: string): string {
  const words = name
    .replace(/\(.*?\)/g, " ")
    .replace(/[^\p{L}\p{N}\s]/gu, " ")
    .split(/\s+/)
    .filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

export function Monogram({ name, size = 28, className }: { name: string | null | undefined; size?: number; className?: string }) {
  const style = { width: size, height: size, fontSize: Math.round(size * (size < 24 ? 0.5 : 0.38)), borderRadius: Math.round(size * 0.28) } as React.CSSProperties;
  if (!name)
    return (
      <span className={cn("flex shrink-0 items-center justify-center bg-surface-2 text-muted", className)} style={style} aria-hidden>
        <FileText style={{ width: size * 0.5, height: size * 0.5 }} strokeWidth={1.8} />
      </span>
    );
  return (
    <span
      className={cn(
        "flex shrink-0 items-center justify-center font-semibold tracking-[0.01em]",
        "bg-[hsl(var(--h)_75%_93%)] text-[hsl(var(--h)_55%_32%)] dark:bg-[hsl(var(--h)_30%_24%)] dark:text-[hsl(var(--h)_75%_78%)]",
        className,
      )}
      style={{ ...style, ["--h" as string]: hueOf(name) }}
      aria-hidden
    >
      {size < 24 ? initials(name).slice(0, 1) : initials(name)}
    </span>
  );
}
