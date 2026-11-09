"use client";

import { ArrowUpRight, ChevronRight } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { getExplore } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { objectHref, safeHref, typeLabel } from "@/lib/format";
import type { Explore, ExploreNode } from "@/lib/types";
import { SectionTitle } from "./record/parts";

type Step = { type: string; id: string; label: string };

export function nodeHref(n: ExploreNode): string {
  return safeHref(n.open_url) ?? objectHref(n.type, n.object_id);
}

/** The line that tells firm links from suggested ones, drawn and worded (never colour alone). */
function Legend() {
  return (
    <p className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-muted" data-testid="explorer-legend">
      <span className="inline-flex items-center gap-1.5">
        <svg width="22" height="8" aria-hidden><line x1="1" y1="4" x2="21" y2="4" stroke="currentColor" strokeWidth="2" /></svg>
        Firm: confirmed link
      </span>
      <span className="inline-flex items-center gap-1.5">
        <svg width="22" height="8" aria-hidden><line x1="1" y1="4" x2="21" y2="4" stroke="currentColor" strokeWidth="2" strokeDasharray="3 3" /></svg>
        Suggested: not confirmed
      </span>
    </p>
  );
}

function NodeButton({ n, onWalk }: { n: ExploreNode; onWalk: (n: ExploreNode) => void }) {
  const firm = n.tier === "firm";
  return (
    <li className="flex items-stretch gap-1">
      <button
        type="button"
        onClick={() => onWalk(n)}
        data-tier={n.tier}
        aria-label={`${n.label}, ${typeLabel(n.type)}, ${firm ? "firm link" : "suggested link"}. Show what it links to`}
        className={cn(
          "min-w-0 flex-1 cursor-pointer rounded-lg border px-2.5 py-1.5 text-left text-[13px] leading-5 hover:bg-hover",
          firm ? "border-solid border-border-strong" : "border-dashed border-border text-muted",
        )}
      >
        <span className="block truncate">{n.label}</span>
        <span className="block text-[12px] leading-4 text-muted">
          {typeLabel(n.type)}
          {!firm && " · suggested"}
        </span>
      </button>
      <Link href={nodeHref(n)} className="flex shrink-0 items-center rounded-lg px-2 text-faint hover:bg-hover hover:text-fg" aria-label={`Open ${n.label}`}>
        <ArrowUpRight className="size-4" aria-hidden />
      </Link>
    </li>
  );
}

/**
 * Link explorer: a record's neighbours one hop out, by kind of link, firm and suggested
 * apart. Choosing a neighbour reads its own neighbours (the second hop, lazily); every read
 * is as this reader, so a record they may not see is not shown, and a trimmed walk is said
 * in words with no count.
 */
export function LinkExplorer({ type, id, label }: { type: string; id: string; label: string }) {
  const [open, setOpen] = useState(false);
  const [trail, setTrail] = useState<Step[]>([{ type, id, label }]);
  const [data, setData] = useState<Explore | null>(null);
  const [error, setError] = useState<string | null>(null);
  const here = trail[trail.length - 1];

  // a different record page starts the explorer over
  useEffect(() => {
    setTrail([{ type, id, label }]);
    setData(null);
    setOpen(false);
  }, [type, id, label]);

  useEffect(() => {
    if (!open) return;
    const c = new AbortController();
    setData(null);
    setError(null);
    getExplore(here.type, here.id, c.signal)
      .then((d) => !c.signal.aborted && setData(d))
      .catch((e) => {
        if (c.signal.aborted) return;
        setError(e?.status === 404 ? "You can't see this record, so what it links to isn't shown." : plainError(e));
      });
    return () => c.abort();
  }, [open, here.type, here.id]);

  const walk = (n: ExploreNode) => setTrail((t) => [...t, { type: n.type, id: n.object_id, label: n.label }]);

  if (!open)
    return (
      <section data-testid="explorer">
        <SectionTitle>Links</SectionTitle>
        <button type="button" onClick={() => setOpen(true)} className="cursor-pointer rounded-lg border border-border px-3 py-1.5 text-[13px] hover:bg-hover" data-testid="explorer-open">
          Explore what this links to
        </button>
      </section>
    );

  return (
    <section data-testid="explorer" aria-live="polite">
      <SectionTitle aside={<button type="button" onClick={() => setOpen(false)} className="cursor-pointer text-[12px] text-muted hover:text-fg">Close</button>}>Links</SectionTitle>
      <nav aria-label="Path walked" className="mb-2 flex flex-wrap items-center gap-1 text-[12px] text-muted" data-testid="explorer-trail">
        {trail.map((s, i) => (
          <span key={`${s.id}-${i}`} className="inline-flex items-center gap-1">
            {i > 0 && <ChevronRight className="size-3" aria-hidden />}
            {i < trail.length - 1 ? (
              <button type="button" onClick={() => setTrail((t) => t.slice(0, i + 1))} className="cursor-pointer underline-offset-2 hover:underline">{s.label}</button>
            ) : (
              <span className="text-fg" aria-current="step">{s.label}</span>
            )}
          </span>
        ))}
      </nav>
      <Legend />
      {error && <p className="mt-2 text-[13px] text-muted" role="status" data-testid="explorer-error">{error}</p>}
      {!error && !data && <p className="mt-2 text-[13px] text-muted" role="status">Reading links</p>}
      {data && (
        <div className="mt-2 space-y-3">
          {(data.groups ?? []).map((g) => (
            <div key={g.key} data-testid="explorer-group">
              <h3 className="mb-1 text-[12px] font-medium text-muted">{g.label}</h3>
              <ul className="grid gap-1 sm:grid-cols-2">
                {(g.nodes ?? []).map((n) => (
                  <NodeButton key={n.object_id} n={n} onWalk={walk} />
                ))}
              </ul>
              {g.more && <p className="mt-1 text-[12px] text-muted">More of this kind exist. Open a record to see them.</p>}
            </div>
          ))}
          {data.message && (
            <p className="text-[13px] text-muted" role="status" data-testid="explorer-note">
              {data.message}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
