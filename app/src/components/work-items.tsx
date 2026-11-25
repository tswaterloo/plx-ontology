import Link from "next/link";
import type { Answer } from "@/lib/types";

export type WorkGroups = NonNullable<Answer["work_items"]>;

/**
 * The items an answer lists (the model's items block), grouped by what the person must do.
 * Each item: where it comes from as a small label (a link when the message or record can be
 * opened), who and when, then one or two lines of what is needed. Plain rows, no cards.
 */
export function WorkItems({ groups }: { groups: WorkGroups }) {
  const shown = groups.filter((g) => (g.items ?? []).length > 0);
  if (shown.length === 0) return null;
  return (
    <div className="space-y-5" data-testid="work-items">
      {shown.map((g, gi) => (
        <section key={gi} aria-label={g.title || "Items"}>
          {g.title && (
            <h3 className="mb-1 flex items-baseline gap-2 text-[13px] font-semibold">
              {g.title}
              <span className="num text-[12px] font-normal text-muted">{(g.items ?? []).length}</span>
            </h3>
          )}
          <ul className="divide-y divide-border/60 border-t border-border/60">
            {(g.items ?? []).map((it, i) => (
              <li key={i} className="py-2" data-testid="work-item">
                <div className="flex flex-wrap items-baseline gap-x-2 text-[12px] text-muted">
                  {it.source && <SourceLabel label={it.source} href={it.open_url ?? null} />}
                  {it.who && <span className="text-fg">{it.who}</span>}
                  {it.when && <span className="num">{it.when}</span>}
                </div>
                <p className="mt-0.5 text-[14px] leading-snug">{it.text}</p>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function SourceLabel({ label, href }: { label: string; href: string | null }) {
  const base = "rounded-sm border border-border px-1.5 py-px text-[11px] font-medium leading-4";
  if (!href) return <span className={`${base} text-muted`}>{label}</span>;
  return (
    <Link prefetch={false} href={href} className={`${base} text-accent hover:bg-hover`} aria-label={`Open the ${label} item`}>
      {label}
    </Link>
  );
}
